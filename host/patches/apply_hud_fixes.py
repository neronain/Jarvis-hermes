#!/usr/bin/env python3
"""Make the HUD's MODELS LOADOUT panel show what is actually running.

Upstream hardcodes that panel in HTML — "whisper base.en", "ElevenLabs Flash
v2.5", "claude-haiku-4-5" are literal strings, not readings. On this
deployment every one of them is wrong, and a status panel that lies is worse
than no panel.

This adds a ``/api/loadout`` endpoint that reports the real brain, STT and TTS
by reading the Hermes config and querying the sidecars, rewires the panel to
render it, drops the Fallback row (nothing configures one here), and relabels
the ElevenLabs quota row when ElevenLabs isn't the voice.

Also makes the local machine's name configurable — upstream calls it
"MAC MINI · HERMES" in code, which is wrong on any other host.

    python apply_hud_fixes.py /path/to/jarvis_ai

Idempotent; writes .orig backups on first run.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

MARKER = "# --- jarvis-hermes: live loadout ---"
HUD_MARKER = "<!-- jarvis-hermes: live loadout -->"

ENDPOINT = '''
''' + MARKER + '''
_LOADOUT_CACHE: dict = {"ts": 0.0, "data": None}


def _hermes_model() -> str:
    """The agent's configured model, read from Hermes' own config."""
    try:
        import yaml
        cfg = yaml.safe_load((Path.home() / ".hermes" / "config.yaml").read_text()) or {}
        return str((cfg.get("model") or {}).get("default") or "") or "hermes-agent"
    except Exception:
        return "hermes-agent"


def _probe(url: str, token: str = "") -> dict:
    try:
        headers = {"X-Jarvis-Token": token} if token else {}
        r = requests.get(url, headers=headers, timeout=2)
        return r.json() if r.ok else {}
    except Exception:
        return {}


@app.get("/api/loadout")
async def loadout() -> JSONResponse:
    """What is actually serving this pipeline right now.

    Cached for 30s: the panel polls on a timer and the sidecar probes are
    network calls, not free.
    """
    now = time.time()
    if _LOADOUT_CACHE["data"] is not None and now - _LOADOUT_CACHE["ts"] < 30:
        return JSONResponse(_LOADOUT_CACHE["data"])

    stt_cfg = CFG.get("stt") or {}
    voice_cfg = CFG.get("voice") or {}
    remote = stt_cfg.get("remote") or {}

    # STT: prefer the remote worker's own report, fall back to the local model.
    stt = f"{stt_cfg.get('model', '?')} (local CPU)"
    if remote.get("url"):
        health = _probe(
            remote["url"].replace("/stt", "/health"),
            os.environ.get(remote.get("token_env", ""), ""),
        )
        if health.get("status") == "ok":
            stt = f"{health.get('model', '?')} · {health.get('device', '?')} · {remote.get('name', 'remote')}"
        else:
            stt = f"{stt_cfg.get('model', '?')} (local — {remote.get('name', 'remote')} down)"

    # TTS: ask the sidecar, so a dead one shows as dead instead of as configured.
    provider = voice_cfg.get("provider", "elevenlabs")
    if provider == "f5_tts_th":
        health = _probe(
            (voice_cfg.get("url", "").rstrip("/")) + "/health",
            os.environ.get(voice_cfg.get("token_env", ""), ""),
        )
        tts = (
            f"{health.get('model', 'F5-TTS-TH')} · {health.get('device', '?')}"
            if health.get("status") == "ok"
            else "F5-TTS-TH (offline)"
        )
        voice_name = voice_cfg.get("voice_name", "")
    else:
        tts = f"ElevenLabs {voice_cfg.get('model', '')}".strip()
        voice_name = voice_cfg.get("voice_name", "")

    data = {
        "brain": _hermes_model(),
        "stt": stt,
        "tts": tts,
        "voice": voice_name,
        "provider": provider,
        # The HUD creates its AudioContext at this rate, and an AudioContext
        # cannot change rate once made. Reading it from the server is what
        # stops the two drifting into speech that plays at the wrong speed.
        "tts_rate": int(voice_cfg.get("sample_rate", 16000)),
    }
    _LOADOUT_CACHE.update({"ts": now, "data": data})
    return JSONResponse(data)


'''

HUD_ROWS_OLD = '''      <div class="kv"><span>Brain</span><b>hermes-agent</b></div>
      <div class="kv"><span>STT</span><b>whisper base.en</b></div>
      <div class="kv"><span>TTS</span><b>ElevenLabs Flash v2.5</b></div>
      <div class="kv"><span>Fallback</span><b>claude-haiku-4-5</b></div>
'''

HUD_ROWS_NEW = '''      ''' + HUD_MARKER + '''
      <div class="kv"><span>Brain</span><b id="ldBrain">—</b></div>
      <div class="kv"><span>STT</span><b id="ldStt">—</b></div>
      <div class="kv"><span>TTS</span><b id="ldTts">—</b></div>
      <div class="kv" id="ldVoiceRow" style="display:none"><span>Voice</span><b id="ldVoice">—</b></div>
'''

HUD_QUOTA_OLD = '''      <div class="kv"><span>11Labs quota</span><b id="uTts">—</b></div>'''
HUD_QUOTA_NEW = '''      <div class="kv"><span id="uTtsLabel">TTS chars today</span><b id="uTts">—</b></div>'''

HUD_JS_ANCHOR = "setInterval(refreshUsage,60000);"
HUD_JS_NEW = '''async function refreshLoadout(){
  try{
    const j=await(await fetch("/api/loadout")).json();
    $("ldBrain").textContent=j.brain||"—";
    $("ldStt").textContent=j.stt||"—";
    $("ldTts").textContent=j.tts||"—";
    if(j.voice){$("ldVoiceRow").style.display="flex";$("ldVoice").textContent=j.voice}
    // The quota bar is an ElevenLabs concept; a self-hosted voice has no quota.
    if(j.provider!=="elevenlabs"){
      $("uTtsLabel").textContent="TTS chars today";
      const bar=$("ttsBar"); if(bar&&bar.parentElement) bar.parentElement.style.display="none";
    }
    const dead=/offline|down/i.test(j.stt+" "+j.tts);
    $("ldStt").className=/down|offline/i.test(j.stt)?"err":"";
    $("ldTts").className=/offline/i.test(j.tts)?"err":"";
  }catch{}
}
refreshLoadout(); setInterval(refreshLoadout,30000);
''' + HUD_JS_ANCHOR

MACHINE_OLD = '''    mac: dict = {"name": "MAC MINI · HERMES", "online": True}'''
MACHINE_NEW = '''    # Upstream hardcodes "MAC MINI · HERMES"; this host may be neither.
    mac: dict = {
        "name": str((CFG.get("server") or {}).get("local_name")
                    or f"{socket.gethostname().upper()} · HERMES"),
        "online": True,
    }'''


def _patch(path: Path, pairs: list[tuple[str, str]], marker: str) -> str:
    src = path.read_text(encoding="utf-8")
    if marker in src:
        return "already patched"
    for old, _ in pairs:
        if old not in src:
            return f"anchor not found: {old.strip()[:60]}"
    backup = path.with_suffix(path.suffix + ".orig")
    if not backup.exists():
        shutil.copy2(path, backup)
    for old, new in pairs:
        src = src.replace(old, new, 1)
    path.write_text(src, encoding="utf-8")
    return "patched"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    root = Path(argv[1]).expanduser().resolve()
    server = root / "server" / "server.py"
    hud = root / "server" / "hud" / "index.html"
    for p in (server, hud):
        if not p.exists():
            print(f"missing: {p}", file=sys.stderr)
            return 1

    src = server.read_text(encoding="utf-8")
    if "import socket" not in src:
        src = src.replace("import os\n", "import os\nimport socket\n", 1)
        server.write_text(src, encoding="utf-8")

    print("server.py:", _patch(server, [
        ('@app.get("/api/machines")', ENDPOINT + '@app.get("/api/machines")'),
        (MACHINE_OLD, MACHINE_NEW),
    ], MARKER))

    # The Flight Deck (scripts/install-hud.sh) is our own page and has all of
    # this built in, so there is nothing to splice into it. Skipping is the
    # correct outcome rather than a failure — returning non-zero here aborted
    # the whole patch run and silently left later patchers unapplied.
    if (root / "server" / "hud" / "app.js").exists():
        print("hud: skipped (Flight Deck installed)")
        return 0

    print("hud:", _patch(hud, [
        (HUD_ROWS_OLD, HUD_ROWS_NEW),
        (HUD_QUOTA_OLD, HUD_QUOTA_NEW),
        (HUD_JS_ANCHOR, HUD_JS_NEW),
    ], HUD_MARKER))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
