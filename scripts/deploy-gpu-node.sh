#!/usr/bin/env bash
# Push this repo's gpu-node/ to the GPU machine and run the installer there.
#
#   ./scripts/deploy-gpu-node.sh [user@host] [remote-dir]
#
# Requires working SSH key auth to the node. Uses rsync when available and
# falls back to tar-over-ssh, so it works on a stock node with no extras.
set -euo pipefail

TARGET="${1:-${JARVIS_GPU_SSH:-neronain@100.84.136.110}}"
REMOTE_DIR="${2:-${JARVIS_GPU_DIR:-~/jarvis-gpu-node}}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log() { printf '\033[36m→\033[0m %s\n' "$*"; }

log "checking ssh to $TARGET"
ssh -o BatchMode=yes -o ConnectTimeout=10 "$TARGET" 'echo ok' >/dev/null \
  || { echo "cannot ssh to $TARGET with key auth — see docs/DEPLOYMENT.md" >&2; exit 1; }

log "creating $REMOTE_DIR"
ssh "$TARGET" "mkdir -p $REMOTE_DIR"

if command -v rsync >/dev/null 2>&1; then
  log "syncing gpu-node/ via rsync"
  rsync -az --delete \
    --exclude '.venv' --exclude '__pycache__' --exclude '.env' \
    --exclude 'voices/*.wav' \
    "$HERE/gpu-node/" "$TARGET:$REMOTE_DIR/"
else
  log "rsync not found — falling back to tar over ssh"
  tar -C "$HERE/gpu-node" \
      --exclude '.venv' --exclude '__pycache__' --exclude '.env' \
      --exclude 'voices/*.wav' -czf - . \
    | ssh "$TARGET" "tar -C $REMOTE_DIR -xzf -"
fi

log "running installer on the node"
ssh "$TARGET" "cd $REMOTE_DIR && bash install.sh --systemd"

cat <<MSG

Deployed to $TARGET:$REMOTE_DIR

Finish on the node:
  ssh $TARGET
  cd $REMOTE_DIR
  cp .env.example .env && \$EDITOR .env        # set the two tokens
  # copy a reference clip into voices/ and edit voices.yaml
  systemctl --user enable --now jarvis-stt jarvis-tts jarvis-stats
  loginctl enable-linger \$USER

Then from here:
  ./scripts/healthcheck.sh
  ./scripts/smoke-test.sh
MSG
