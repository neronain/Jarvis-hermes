#!/usr/bin/env bash
# Jarvis-hermes — one-command GPU node bootstrap.
#
# Run this ON the GPU machine. It clones the repo, installs both sidecars,
# generates the shared tokens, and starts the services.
#
#   curl -fsSL https://raw.githubusercontent.com/neronain/Jarvis-hermes/main/scripts/bootstrap.sh | bash
#
# Re-runnable: existing tokens, voices.yaml and reference clips are preserved.
#
# Testing hooks (not for normal use):
#   JARVIS_SKIP_GPU_CHECK=1   skip the nvidia-smi requirement
#   JARVIS_SKIP_INSTALL=1     skip the venv/torch/model install and service start
set -euo pipefail

REPO="${JARVIS_REPO:-https://github.com/neronain/Jarvis-hermes.git}"
DIR="${JARVIS_DIR:-$HOME/Jarvis-hermes}"
BRANCH="${JARVIS_BRANCH:-main}"

log()  { printf '\033[36m→\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '\033[33m⚠\033[0m %s\n' "$*"; }
die()  { printf '\033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

printf '\n\033[1m  Jarvis-hermes — GPU node bootstrap\033[0m\n\n'

# --- preflight -------------------------------------------------------------
command -v git >/dev/null || die "git not found — sudo apt install -y git"
command -v python3 >/dev/null || die "python3 not found"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' \
  || die "Python 3.10+ required (found $(python3 -V 2>&1))"
# `import venv` succeeds on stock Ubuntu even though `python -m venv` then
# fails: the missing piece is ensurepip, not venv. install.sh falls back to uv
# when neither is present, so this is a note rather than a hard stop.
python3 -c 'import ensurepip' 2>/dev/null \
  || command -v uv >/dev/null 2>&1 \
  || warn "no ensurepip and no uv — the installer will fetch uv into ~/.local/bin"

if command -v nvidia-smi >/dev/null 2>&1; then
  ok "GPU: $(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader | head -1)"
  VRAM=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -1)
  if [[ "${VRAM:-0}" -lt 10000 ]]; then
    warn "VRAM is ${VRAM} MiB — STT will be set to int8_float16 so both sidecars fit"
    LOW_VRAM=1
  fi
elif [[ "${JARVIS_SKIP_GPU_CHECK:-0}" == "1" ]]; then
  warn "skipping GPU check (JARVIS_SKIP_GPU_CHECK=1)"
else
  die "nvidia-smi not found — this script must run on a machine with an NVIDIA GPU"
fi

# --- fetch -----------------------------------------------------------------
if [[ -d "$DIR/.git" ]]; then
  log "updating $DIR"
  git -C "$DIR" fetch --quiet origin "$BRANCH"
  git -C "$DIR" checkout --quiet "$BRANCH"
  git -C "$DIR" pull --quiet --ff-only origin "$BRANCH"
else
  log "cloning into $DIR"
  git clone --quiet --branch "$BRANCH" "$REPO" "$DIR"
fi
cd "$DIR/gpu-node"

# --- tokens ----------------------------------------------------------------
# Generated here rather than asked for: a token the user has to invent is a
# token that ends up being "test123".
if [[ -f .env ]]; then
  ok ".env already exists — keeping your tokens"
else
  log "generating .env with fresh tokens"
  cp .env.example .env
  gen() { openssl rand -hex 32 2>/dev/null || head -c32 /dev/urandom | od -An -tx1 | tr -d ' \n'; }
  STT_TOK="$(gen)"; TTS_TOK="$(gen)"
  sed -i "s|^JARVIS_STT_TOKEN=.*|JARVIS_STT_TOKEN=${STT_TOK}|" .env
  sed -i "s|^JARVIS_TTS_TOKEN=.*|JARVIS_TTS_TOKEN=${TTS_TOK}|" .env
  [[ "${LOW_VRAM:-0}" == "1" ]] && \
    sed -i "s|^JARVIS_STT_COMPUTE=.*|JARVIS_STT_COMPUTE=int8_float16|" .env
  chmod 600 .env
fi

# --- install ---------------------------------------------------------------
if [[ "${JARVIS_SKIP_INSTALL:-0}" == "1" ]]; then
  warn "skipping installer (JARVIS_SKIP_INSTALL=1)"
else
  log "running installer (torch + models — this takes a while on first run)"
  bash install.sh --systemd
fi

# --- reference voice -------------------------------------------------------
shopt -s nullglob
CLIPS=(voices/*.wav voices/*.mp3 voices/*.m4a voices/*.flac)
shopt -u nullglob

if [[ ${#CLIPS[@]} -eq 0 ]]; then
  warn "no reference clip found in $DIR/gpu-node/voices/"
  echo
  echo "  The TTS sidecar cannot start without one. Add a 5-15 second Thai clip:"
  echo "    ffmpeg -i your-recording.m4a -ac 1 -ar 24000 -sample_fmt s16 \\"
  echo "      $DIR/gpu-node/voices/jarvis_ref.wav"
  echo "  then set ref_text in $DIR/gpu-node/voices.yaml to its exact transcript,"
  echo "  and run:  systemctl --user enable --now jarvis-tts"
  echo
  START_TTS=0
else
  ok "reference clip: ${CLIPS[0]}"
  START_TTS=1
fi

# --- start -----------------------------------------------------------------
if [[ "${JARVIS_SKIP_INSTALL:-0}" == "1" ]]; then
  warn "skipping service start (JARVIS_SKIP_INSTALL=1)"
else
  log "starting services"
  systemctl --user daemon-reload
  systemctl --user enable --now jarvis-stt jarvis-stats
  [[ "$START_TTS" == "1" ]] && systemctl --user enable --now jarvis-tts
  loginctl enable-linger "$USER" 2>/dev/null || warn "could not enable linger — services stop at logout"
fi

# --- report ----------------------------------------------------------------
# shellcheck disable=SC1091
set -a; source .env; set +a
IP=$(command -v tailscale >/dev/null && tailscale ip -4 2>/dev/null | head -1 || true)
IP="${IP:-$(hostname -I 2>/dev/null | awk '{print $1}')}"

echo
printf '\033[1m  Done. Give these to the voice host:\033[0m\n\n'
cat <<REPORT
  # add to ~/.hermes/.env on the host running Hermes
  JARVIS_STT_TOKEN=${JARVIS_STT_TOKEN}
  JARVIS_TTS_TOKEN=${JARVIS_TTS_TOKEN}

  # add to server.yaml
  stt.remote.url : http://${IP}:${JARVIS_STT_PORT:-8768}/stt
  voice.url      : http://${IP}:${JARVIS_TTS_PORT:-8769}
REPORT
echo
if [[ "${JARVIS_SKIP_INSTALL:-0}" == "1" ]]; then
  echo "  (skipped health wait)"
  exit 0
fi
log "waiting for models to load (first start pulls several GB)..."
for i in $(seq 1 60); do
  s=$(curl -s -m 2 "http://127.0.0.1:${JARVIS_STT_PORT:-8768}/health" 2>/dev/null | grep -o '"status":"[a-z]*"' || true)
  [[ "$s" == '"status":"ok"' ]] && { ok "STT ready"; break; }
  sleep 5
done
if [[ "$START_TTS" == "1" ]]; then
  for i in $(seq 1 60); do
    s=$(curl -s -m 2 "http://127.0.0.1:${JARVIS_TTS_PORT:-8769}/health" 2>/dev/null | grep -o '"status":"[a-z]*"' || true)
    [[ "$s" == '"status":"ok"' ]] && { ok "TTS ready"; break; }
    sleep 5
  done
fi

echo
echo "  Status:  systemctl --user status jarvis-stt jarvis-tts"
echo "  Logs:    journalctl --user -u jarvis-tts -f"
echo "  Health:  curl -s localhost:${JARVIS_TTS_PORT:-8769}/health"
echo
