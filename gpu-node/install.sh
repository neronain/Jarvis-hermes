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
TORCH_INDEX="${JARVIS_TORCH_INDEX:-https://download.pytorch.org/whl/cu124}"
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

if command -v nvidia-smi >/dev/null 2>&1; then
  ok "GPU: $(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader | head -1)"
else
  warn "nvidia-smi not found — the sidecars require CUDA and will fail to start"
fi

# --- venv ------------------------------------------------------------------
if [[ ! -d "$VENV" ]]; then
  log "creating venv at $VENV"
  "$PY" -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"
python -m pip install --quiet --upgrade pip wheel

# --- torch (CUDA build first, so f5-tts-th doesn't pull the CPU wheel) ------
if python -c 'import torch, sys; sys.exit(0 if torch.cuda.is_available() else 1)' 2>/dev/null; then
  ok "torch with CUDA already present ($(python -c 'import torch; print(torch.__version__)'))"
else
  log "installing torch from $TORCH_INDEX (this is the slow part)"
  pip install --index-url "$TORCH_INDEX" torch torchaudio \
    || die "torch install failed — check that $TORCH_INDEX matches this node's CUDA version"
fi

# --- sidecar deps ----------------------------------------------------------
log "installing sidecar dependencies"
pip install -r "$HERE/requirements.txt"

# --- config ----------------------------------------------------------------
if [[ ! -f "$HERE/voices.yaml" ]]; then
  cp "$HERE/voices.example.yaml" "$HERE/voices.yaml"
  warn "created voices.yaml from the example — add a reference clip before starting the TTS sidecar"
fi

# --- models ----------------------------------------------------------------
if [[ "$SKIP_MODELS" -eq 0 ]]; then
  log "pre-downloading faster-whisper $STT_MODEL"
  python - <<PYEOF
from faster_whisper import WhisperModel
WhisperModel("${STT_MODEL}", device="cpu", compute_type="int8")
print("whisper weights cached")
PYEOF

  log "pre-downloading F5-TTS-TH weights"
  python - <<'PYEOF'
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
