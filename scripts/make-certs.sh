#!/usr/bin/env bash
# Generate the TLS cert the HUD needs. Browsers only grant microphone access
# over https (or on localhost), so the HUD is unusable on a LAN IP without it.
#
#   ./scripts/make-certs.sh [/path/to/jarvis_ai]
#
# The cert MUST land in <jarvis_ai>/server/certs/ — server.py resolves
# `tls_cert` against its own directory, not the repo root, and silently skips
# TLS when the file isn't there. A cert in <jarvis_ai>/certs/ looks right and
# does nothing.
set -euo pipefail

JA="${1:-$HOME/jarvis_ai}"
[[ -f "$JA/server/server.py" ]] || { echo "not a jarvis_ai checkout: $JA" >&2; exit 1; }

CERT_DIR="$JA/server/certs"
mkdir -p "$CERT_DIR"

if [[ -f "$CERT_DIR/cert.pem" ]]; then
  echo "cert already exists at $CERT_DIR — delete it to regenerate"
  exit 0
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
IP="${IP:-127.0.0.1}"

openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
  -keyout "$CERT_DIR/key.pem" -out "$CERT_DIR/cert.pem" \
  -subj "/CN=jarvis.local" \
  -addext "subjectAltName=DNS:localhost,DNS:jarvis.local,IP:127.0.0.1,IP:${IP}" 2>/dev/null

chmod 600 "$CERT_DIR/key.pem"
echo "created $CERT_DIR/cert.pem  (SAN: localhost, 127.0.0.1, $IP)"
echo
echo "It is self-signed, so the browser warns once per device — accept it and"
echo "the microphone works from then on."
