#!/usr/bin/env bash
# Jarvis GPU node installer — Linux + NVIDIA.
#
# Idempotent: safe to re-run after a pull. Creates a venv, installs the
# sidecars, downloads the models, and (optionally) installs systemd units.
#
#   ./install.sh                 # venv + deps + models
#   ./install.sh --systemd       # also install & enable user services
#   ./install.sh --skip-models   # deps only (models download on first run)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${JARVIS_VENV:-$HERE/.venv}"
PY="${JARVIS_PYTHON:-python3}"
TORCH_INDEX="${JARVIS_TORCH_INDEX:-}"   # empty = pick from the GPU's compute capability
WITH_SYSTEMD=0
SKIP_MODELS=0
STT_MODEL="${JARVIS_STT_MODEL:-large-v3}"

for arg in "$@"; do
  case "$arg" in
    --systemd)     WITH_SYSTEMD=1 ;;
    --skip-models) SKIP_MODELS=1 ;;
    -h|--help)     sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

log()  { printf '\033[36m→\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '\033[33m⚠\033[0m %s\n' "$*"; }
die()  { printf '\033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

# --- preflight -------------------------------------------------------------
log "checking prerequisites"
command -v "$PY" >/dev/null || die "$PY not found"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' \
  || die "Python 3.10+ required (found $("$PY" -V 2>&1))"

# Stock Ubuntu ships python3 without ensurepip, so `python -m venv` fails and
# fixing it needs apt + sudo. uv is a single user-space binary that creates the
# venv itself, so the installer never needs root. Prefer it when present, and
# fetch it rather than asking for a password we may not be able to supply.
PKG=""
if command -v uv >/dev/null 2>&1; then
  PKG=uv
elif "$PY" -c 'import ensurepip' 2>/dev/null; then
  PKG=venv
else
  log "python3-venv is unavailable — installing uv into ~/.local/bin"
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 || true
  export PATH="$HOME/.local/bin:$PATH"
  command -v uv >/dev/null 2>&1 \
    || die "could not install uv. Either install it manually, or run:
       sudo apt install -y python3-venv"
  PKG=uv
fi
ok "package manager: $PKG"

# Picking the wrong CUDA index is the most expensive mistake here: pip installs
# happily, nothing errors, and the first inference dies with "no kernel image is
# available for execution on the device" — or silently runs on CPU. Blackwell
# (sm_120 / sm_121) has no kernels in cu124 at all, so the index is derived from
# the card rather than hardcoded.
pick_torch_index() {
  local cap major
  cap=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -1)
  major=${cap%%.*}
  if [[ -z "$cap" ]]; then
    echo "https://download.pytorch.org/whl/cu124"          # no GPU visible; harmless default
  elif [[ "$major" -ge 12 ]]; then
    echo "https://download.pytorch.org/whl/cu128"          # Blackwell
  elif [[ "$major" -ge 9 ]]; then
    echo "https://download.pytorch.org/whl/cu126"          # Hopper/Ada refresh
  else
    echo "https://download.pytorch.org/whl/cu124"
  fi
}

if command -v nvidia-smi >/dev/null 2>&1; then
  ok "GPU: $(nvidia-smi --query-gpu=name,compute_cap --format=csv,noheader | head -1)"
  if [[ -z "$TORCH_INDEX" ]]; then
    TORCH_INDEX="$(pick_torch_index)"
    ok "torch index: ${TORCH_INDEX##*/} (from compute capability)"
  fi
else
  warn "nvidia-smi not found — the sidecars require CUDA and will fail to start"
  TORCH_INDEX="${TORCH_INDEX:-https://download.pytorch.org/whl/cu124}"
fi

# --- venv ------------------------------------------------------------------
if [[ ! -x "$VENV/bin/python" ]]; then
  log "creating venv at $VENV"
  if [[ "$PKG" == "uv" ]]; then
    uv venv "$VENV"
  else
    "$PY" -m venv "$VENV"
  fi
fi
VPY="$VENV/bin/python"

# One wrapper so the rest of the script doesn't branch on the package manager.
pip_install() {
  if [[ "$PKG" == "uv" ]]; then
    uv pip install --python "$VPY" "$@"
  else
    "$VPY" -m pip install "$@"
  fi
}

if [[ "$PKG" == "venv" ]]; then
  "$VPY" -m pip install --quiet --upgrade pip wheel
fi

# --- torch (CUDA build first, so f5-tts-th doesn't pull the CPU wheel) ------
# "torch.cuda.is_available()" is necessary but not sufficient on Blackwell: a
# cu124 build reports True and then fails at the first kernel launch. Check that
# this card's architecture is actually in the build.
torch_supports_this_gpu() {
  "$VPY" - <<'PYEOF' 2>/dev/null
import sys
try:
    import torch
    if not torch.cuda.is_available():
        sys.exit(1)
    cap = torch.cuda.get_device_capability(0)
    arch = f"sm_{cap[0]}{cap[1]}"
    sys.exit(0 if arch in torch.cuda.get_arch_list() else 1)
except Exception:
    sys.exit(1)
PYEOF
}

if torch_supports_this_gpu; then
  ok "torch with CUDA already present ($("$VPY" -c 'import torch; print(torch.__version__)'))"
else
  log "installing torch from $TORCH_INDEX (this is the slow part)"
  pip_install --upgrade --index-url "$TORCH_INDEX" torch torchaudio \
    || die "torch install failed — check that $TORCH_INDEX matches this node's CUDA version"
  torch_supports_this_gpu \
    || die "torch installed but has no kernels for this GPU. Override the index, e.g.
       JARVIS_TORCH_INDEX=https://download.pytorch.org/whl/cu130 ./install.sh"
fi

# --- sidecar deps ----------------------------------------------------------
log "installing sidecar dependencies"
pip_install -r "$HERE/requirements.txt"

# --- config ----------------------------------------------------------------
# systemd's EnvironmentFile= keeps everything after "=", trailing comment
# included, so `JARVIS_STT_COMPUTE=float16  # note` reaches ctranslate2 verbatim
# and it rejects the compute type. Catch it here rather than in a stack trace.
if [[ -f "$HERE/.env" ]] && grep -qE '^[A-Z_]+=[^#]*[^ ]+[[:space:]]+#' "$HERE/.env"; then
  warn "these .env lines have trailing comments, which systemd treats as part of the value:"
  grep -nE '^[A-Z_]+=[^#]*[^ ]+[[:space:]]+#' "$HERE/.env" | sed 's/^/    /'
  warn "move each comment onto its own line before starting the services"
fi

if [[ ! -f "$HERE/voices.yaml" ]]; then
  cp "$HERE/voices.example.yaml" "$HERE/voices.yaml"
  warn "created voices.yaml from the example — add a reference clip before starting the TTS sidecar"
fi

# --- models ----------------------------------------------------------------
if [[ "$SKIP_MODELS" -eq 0 ]]; then
  log "pre-downloading faster-whisper $STT_MODEL"
  "$VPY" - <<PYEOF
from faster_whisper import WhisperModel
WhisperModel("${STT_MODEL}", device="cpu", compute_type="int8")
print("whisper weights cached")
PYEOF

  log "pre-downloading F5-TTS-TH weights"
  "$VPY" - <<'PYEOF'
try:
    from f5_tts_th.tts import TTS
    TTS(model="v2")
    print("F5-TTS-TH weights cached")
except Exception as exc:  # first-run download can need HF auth or a proxy
    print(f"WARNING: could not pre-download F5-TTS-TH ({exc});"
          " it will download on first request instead")
PYEOF
fi

# --- systemd ---------------------------------------------------------------
if [[ "$WITH_SYSTEMD" -eq 1 ]]; then
  UNIT_DIR="$HOME/.config/systemd/user"
  mkdir -p "$UNIT_DIR"
  for unit in "$HERE"/systemd/*.service; do
    name="$(basename "$unit")"
    sed -e "s|@@WORKDIR@@|$HERE|g" -e "s|@@VENV@@|$VENV|g" "$unit" > "$UNIT_DIR/$name"
    ok "installed $name"
  done
  systemctl --user daemon-reload
  warn "set JARVIS_STT_TOKEN / JARVIS_TTS_TOKEN in $HERE/.env before enabling"
  echo "  systemctl --user enable --now jarvis-stt jarvis-tts jarvis-stats"
  echo "  loginctl enable-linger $USER   # keep services up after logout"
fi

ok "install complete"
echo
echo "Next:"
echo "  1. put a reference clip in $HERE/voices/ and update voices.yaml"
echo "  2. cp .env.example .env && edit the tokens"
echo "  3. ./run-stt.sh   and   ./run-tts.sh   (or use --systemd)"
