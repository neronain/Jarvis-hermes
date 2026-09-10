#!/usr/bin/env python3
"""Turn the HUD's push-to-talk into a hands-free conversation.

Upstream is click-to-start, click-to-send. This keeps the microphone open
after one press and lets a voice-activity detector decide where turns begin
and end — including cutting the assistant off when you start talking over it.

The detector runs on the level meter the capture worklet already computes, so
nothing is added to the audio path: one number per 80 ms frame.

Three things make it work in a real room rather than only on a desk:

- **The threshold follows the room.** A fixed level works in one room and
  nowhere else, so the noise floor is tracked continuously while nobody is
  speaking and the threshold is a multiple of it.
- **Barge-in is stricter than a cold start.** While the assistant is speaking
  the microphone hears it through the speakers, so interrupting needs both a
  higher level and a longer run than starting from silence. Echo cancellation
  is already on in getUserMedia; this is the second line of defence.
- **Short bursts are discarded.** A cough, a door, a keyboard — anything under
  minTurnMs of speech is dropped instead of being sent as a turn.

    python apply_handsfree.py /path/to/jarvis_ai

Idempotent; writes a .orig backup on first run.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

MARKER = "/* jarvis-hermes: hands-free */"
SRV_MARKER = "# --- jarvis-hermes: instant ack ---"

ACK_ENDPOINT = '''
''' + SRV_MARKER + '''
# A turn takes six to fourteen seconds. People do not wait that long in silence
# without assuming they were not heard, so the assistant says something within
# a few hundred milliseconds of you stopping — the way a person says "mm" while
# they think. It changes nothing about the real latency and everything about
# how the wait feels.
#
# Upstream has ack_after_seconds and ack_texts in its example config but never
# reads either (grep says zero), so this is built rather than configured.
ACK_TEXTS = ["ครับ", "ได้ครับ", "รับทราบครับ", "อืม"]
_ACK_CACHE: dict = {}


@app.get("/api/ack")
async def ack(i: int = -1):
    """One short acknowledgment as 16 kHz PCM. Synthesised once, then cached.

    The clips are fetched and decoded by the HUD when a hands-free session
    starts, so playing one at end-of-turn costs nothing at the moment it
    matters.
    """
    import random
    from fastapi.responses import Response as _Resp

    idx = i if 0 <= i < len(ACK_TEXTS) else random.randrange(len(ACK_TEXTS))
    if idx not in _ACK_CACHE:
        pcm = b""
        voice = CFG.get("voice") or {}
        try:
            if voice.get("provider") == "f5_tts_th":
                from f5_tts_provider import F5TTSProvider
                pcm = F5TTSProvider.from_config(voice).synthesize(ACK_TEXTS[idx])
        except Exception:
            # No ack is a worse experience, not a broken one - stay quiet.
            import traceback; traceback.print_exc()
        _ACK_CACHE[idx] = pcm
    return _Resp(content=_ACK_CACHE[idx], media_type="application/octet-stream")


'''


JS = MARKER + """
// Tunables. Rooms differ, so these can be overridden at runtime without a
// redeploy:  localStorage.setItem("jarvisVad", JSON.stringify({endMs:1200}))
const VAD = Object.assign({
  frameMs:      80,   // one worklet frame; everything below is milliseconds
  startMs:     200,   // speech must persist this long before a turn opens
  bargeMs:     420,   // ... and longer to interrupt the assistant
  endMs:       650,   // silence this long closes the turn (dead time you feel)
  minTurnMs:   400,   // anything shorter is a cough, not a sentence
  maxTurnMs: 20000,   // hard stop: a turn always ends, whatever the detector thinks
  speakMargin: 3.0,   // level over the room's noise floor to count as speech
  bargeMargin: 6.0,   // ... over the assistant's echo, to count as interrupting
  echoGuardMs: 700,   // keep the strict bar this long after the last audio chunk
  dropRatio:  0.22,   // ... or this fraction of the turn's own peak (see below)
  floorUp:    0.02,   // noise floor rises slowly
  floorDown:  0.25,   // ... and falls quickly, to recover after speech
  floorMin:  0.004,
}, (()=>{ try{ return JSON.parse(localStorage.getItem("jarvisVad")||"{}") }catch{ return {} } })());
const VAD_DEBUG = !!localStorage.getItem("jarvisVadDebug");

let handsFree=false, vadSpeech=0, vadSilence=0, noiseFloor=null, turnMs=0, turnPeak=0;
let agentBusy=false, lastAudioAt=0, ackBuffers=[];

// Fetched once per session so end-of-turn playback is instant. Failures are
// silent: no acknowledgment is a duller experience, not a broken one.
async function loadAcks(){
  ackBuffers=[];
  for(let i=0;i<4;i++){
    try{
      const r=await fetch("/api/ack?i="+i);
      if(!r.ok) continue;
      const raw=await r.arrayBuffer();
      if(raw.byteLength<640) continue;          // under 20 ms is not a word
      const i16=new Int16Array(raw), f32=new Float32Array(i16.length);
      for(let k=0;k<i16.length;k++) f32[k]=i16[k]/32768;
      const ab=audioCtx.createBuffer(1,f32.length,16000); ab.copyToChannel(f32,0);
      ackBuffers.push(ab);
    }catch{}
  }
}

function playAck(){
  if(!ackBuffers.length || !audioCtx) return;
  const ab=ackBuffers[Math.floor(Math.random()*ackBuffers.length)];
  const src=audioCtx.createBufferSource();
  src.buffer=ab; src.connect(audioCtx.destination); src.start();
  // Deliberately not added to activeSources: barge-in must never cancel it,
  // and it is over before the reply begins. But the microphone will hear it,
  // so the echo guard has to cover it or it opens a turn of its own.
  lastAudioAt = performance.now() + ab.duration*1000;
}

function botSpeaking(){
  return activeSources.length>0 && audioCtx && playhead > audioCtx.currentTime + 0.05;
}

// The assistant's own voice comes back through the microphone, and the queue
// runs dry between sentences - so botSpeaking() alone reports "silent" in the
// gaps, the loose threshold applies, and the echo of its own last word opens a
// new turn. The agent answers again, that answer echoes, and it loops. Hence a
// guard window after the last chunk, and a busy flag that spans the gaps.
function echoRisk(){
  return botSpeaking() || (performance.now() - lastAudioAt) < VAD.echoGuardMs;
}
function assistantActive(){ return agentBusy || echoRisk(); }

// Derived by wrapping rather than by patching four separate event handlers:
// every one of them routes through setState, and both are called by name at
// event time, so the wrappers are what actually run.
const _setState = setState;
setState = function(st, label, hint){
  if(st==="thinking" || st==="tool" || st==="speaking") agentBusy=true;
  else if(st==="standby") agentBusy=false;
  return _setState(st, label, hint);
};
const _playChunk = playChunk;
playChunk = function(buf){ lastAudioAt = performance.now(); return _playChunk(buf); };

function hfStatus(text, cls){
  const el=$("hfState"); if(el){ el.textContent=text; el.className=cls||""; }
}

function beginTurn(interrupting){
  stopPlayback();                       // barge-in: drop what is still queued
  audioArrived=false;
  ws.send(JSON.stringify({type:"start",sample_rate:16000,format:"pcm_s16le",
                          channels:1,conversation:CONV}));
  capturing=true; turnMs=0; vadSilence=0; turnPeak=0;
  setState("listening","LISTENING", interrupting?"INTERRUPTED — GO AHEAD":"SPEAKING DETECTED");
  hfStatus(interrupting?"interrupting":"listening","ok");
}

function endTurn(why){
  capturing=false;
  ws.send(JSON.stringify({type:"stop"}));
  playAck();   // answer the silence immediately, before the agent has started
  vadSpeech=0; vadSilence=0; turnMs=0; turnPeak=0;
  $("levelBar").style.width="0%";
  setState("thinking","PROCESSING");
  hfStatus(why==="max"?"sent (max length)":"thinking");
}

function dropTurn(){
  // Too short to be speech - a cough, a door. Nothing is sent: the server only
  // processes a buffer on "stop", and the next "start" clears it, so simply
  // not finishing the turn discards it.
  capturing=false;
  vadSpeech=0; vadSilence=0; turnMs=0; turnPeak=0;
  $("levelBar").style.width="0%";
  setState("standby","STANDBY","HANDS-FREE — JUST TALK");
  hfStatus("waiting");
}

function vadFrame(lvl){
  if(!handsFree || !wsReady) return;
  const echo   = echoRisk();        // is the microphone hearing the assistant?
  const active = assistantActive(); // ... or is it working on an answer?

  // Learn the room only when neither side is talking, or the floor climbs to
  // match whoever is speaking and the detector goes deaf. Asymmetric on
  // purpose: rise slowly so a passing noise does not raise the bar, fall
  // quickly so the room is re-learned as soon as a turn ends.
  if(!capturing && !active){
    const a = (noiseFloor===null || lvl < noiseFloor) ? VAD.floorDown : VAD.floorUp;
    noiseFloor = (noiseFloor===null) ? lvl : noiseFloor*(1-a) + lvl*a;
  }
  const floor  = Math.max(noiseFloor===null?VAD.floorMin:noiseFloor, VAD.floorMin);
  // The strict bar applies only when echo is actually possible. While the
  // agent is merely thinking there is nothing to echo, and holding the bar
  // high there would make it needlessly hard to interrupt.
  const absGate = floor * (echo ? VAD.bargeMargin : VAD.speakMargin);

  // Absolute levels alone are not enough. Browser auto gain control lifts the
  // signal once you stop talking, so room noise can sit at the same level your
  // voice did and an absolute gate never sees silence - the turn then never
  // ends, which is exactly the bug this replaced. Comparing against the
  // loudest frame of this turn survives that: whatever the gain does, silence
  // is far below the peak of actual speech.
  if(capturing) turnPeak = Math.max(turnPeak, lvl);
  const gate = capturing ? Math.max(absGate, turnPeak*VAD.dropRatio) : absGate;

  if(lvl > gate){ vadSpeech += VAD.frameMs; vadSilence = 0; }
  else          { vadSilence += VAD.frameMs; if(!capturing) vadSpeech = 0; }

  if(VAD_DEBUG) hfStatus(`${lvl.toFixed(3)}>${gate.toFixed(3)} sil${vadSilence}`);

  if(capturing){
    turnMs += VAD.frameMs;
    // The safety net. Even if the detector is wrong about the room, a turn
    // must not hang forever waiting for a silence it will never see.
    if(turnMs >= VAD.maxTurnMs){ endTurn("max"); return; }
    if(vadSilence >= VAD.endMs){
      // turnMs includes the trailing silence; the speech is what came before.
      if(turnMs - vadSilence >= VAD.minTurnMs) endTurn(); else dropTurn();
    }
    return;
  }
  if(vadSpeech >= (active ? VAD.bargeMs : VAD.startMs)) beginTurn(active);
}

// Push-to-talk and hands-free drive the same capture state, so one has to
// win rather than both racing. A guard at the call sites, not a reassignment
// of upstream's function: the ring's handler captured its reference before
// this code runs, so reassigning would leave that one path unguarded.
function talkGuarded(){
  if(handsFree){ addMsg("sys","hands-free เปิดอยู่ — พูดได้เลย ไม่ต้องกด"); return; }
  return toggleTalk();
}

async function toggleHandsFree(){
  if(!wsReady){ addMsg("sys","voice server offline"); return; }
  if(handsFree){
    handsFree=false;
    if(capturing) endTurn();
    $("talkBtn").textContent="ENGAGE VOICE";
    $("micState").textContent="ready";
    $("levelBar").style.width="0%";
    setState("standby","STANDBY","CLICK RING OR SPACE TO TALK");
    hfStatus("off");
    addMsg("sys","hands-free ปิดแล้ว — กลับไปกดพูดทีละครั้ง (วงแหวน หรือ Space)");
    return;
  }
  try{ await initMic() }catch(err){ addMsg("sys","mic blocked: "+err.message); return }
  loadAcks();                       // not awaited: the first turn can go without
  handsFree=true; noiseFloor=null; vadSpeech=0; vadSilence=0; turnPeak=0;
  agentBusy=false; lastAudioAt=0;
  $("talkBtn").textContent="■ END SESSION";
  $("micState").textContent="LIVE";
  setState("standby","STANDBY","HANDS-FREE — JUST TALK");
  hfStatus("waiting");
  addMsg("sys","hands-free เปิดแล้ว — พูดได้เลย หยุดพูดแล้วระบบส่งให้เอง · "
              +"พูดแทรกตอนบอทกำลังพูดเพื่อขัดจังหวะได้ · กด END SESSION เพื่อปิด");
}
"""

BTN_OLD = '''        <button class="btn" id="talkBtn" style="flex:1">ENGAGE VOICE</button>'''
BTN_NEW = '''        <button class="btn" id="talkBtn" style="flex:1">ENGAGE VOICE</button>
      </div>
      <div class="kv"><span>Hands-free</span><b id="hfState">off</b></div>
      <div style="display:none">'''

# Auto gain control is the single thing that breaks an energy-based detector:
# it lifts the signal once you stop talking, so the room noise ends up at the
# level your voice was and silence never arrives. Echo cancellation and noise
# suppression stay on - both help, neither hides the pause.
MIC_OLD = '''echoCancellation:true,noiseSuppression:true,autoGainControl:true'''
MIC_NEW = '''echoCancellation:true,noiseSuppression:true,autoGainControl:false'''

LEVEL_OLD = '''      if(capturing)$("levelBar").style.width=(level*100).toFixed(0)+"%";'''
LEVEL_NEW = '''      if(capturing)$("levelBar").style.width=(level*100).toFixed(0)+"%";
      vadFrame(level);   // hands-free turn detection rides the level meter'''

# Only the big button becomes the session toggle. The ring and Space stay
# push-to-talk, so a single deliberate turn is still one press — useful when
# the room is loud, or when you want to think mid-sentence without the pause
# being read as the end of your turn.
# An empty transcript is routine in hands-free - a cough that beat the length
# filter, or a barge-in the room triggered. Upstream shows it as an error and
# leaves the ring in whatever state it was; here it just re-arms.
ERR_OLD = '''    else if(e.type==="error"){ clearLive(); addMsg("sys","error: "+e.message); setState("standby","STANDBY"); showStop(false); }'''
ERR_NEW = '''    else if(e.type==="error"){
      clearLive(); showStop(false);
      const quiet = handsFree && /no transcript/i.test(e.message||"");
      if(!quiet) addMsg("sys","error: "+e.message);
      setState("standby","STANDBY", handsFree?"HANDS-FREE — JUST TALK":undefined);
      if(handsFree) hfStatus("waiting");
    }'''

BIND_OLD = '''$("talkBtn").onclick=toggleTalk;
$("reactorWrap").onclick=toggleTalk;'''
BIND_NEW = '''$("talkBtn").onclick=toggleHandsFree;
$("reactorWrap").onclick=()=>talkGuarded();'''

# Space calls toggleTalk() by name at event time, so it has to go through the
# guard too or the keyboard bypasses what the mouse respects.
SPACE_OLD = '''  if(e.code==="Space"&&document.activeElement!==$("chatInput")){e.preventDefault();toggleTalk()}'''
SPACE_NEW = '''  if(e.code==="Space"&&document.activeElement!==$("chatInput")){e.preventDefault();talkGuarded()}'''


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    root = Path(argv[1]).expanduser().resolve()
    hud = root / "server" / "hud" / "index.html"
    srv = root / "server" / "server.py"
    for f in (hud, srv):
        if not f.exists():
            print(f"missing: {f}", file=sys.stderr)
            return 1

    # The ack endpoint lives beside the other /api routes.
    ssrc = srv.read_text(encoding="utf-8")
    if SRV_MARKER not in ssrc:
        srv_anchor = '@app.get("/api/machines")'
        if srv_anchor not in ssrc:
            print("server anchor not found for the ack endpoint", file=sys.stderr)
            return 1
        sbak = srv.with_suffix(".py.orig")
        if not sbak.exists():
            shutil.copy2(srv, sbak)
        srv.write_text(ssrc.replace(srv_anchor, ACK_ENDPOINT + srv_anchor, 1), encoding="utf-8")
        print(f"patched {srv.name} (ack endpoint)")

    backup = hud.with_suffix(".html.orig")
    src = hud.read_text(encoding="utf-8")
    if MARKER in src:
        # Re-apply rather than refuse: this file gets iterated on, and patching
        # a patched file would nest the block.
        if not backup.exists():
            print("already patched but no .orig to rebuild from", file=sys.stderr)
            return 1
        print("already patched - rebuilding from .orig")
        src = backup.read_text(encoding="utf-8")

    anchor = "/* ---- pop-up viewer ---- */"
    for needle in (BTN_OLD, LEVEL_OLD, BIND_OLD, SPACE_OLD, MIC_OLD, ERR_OLD, anchor):
        if needle not in src:
            print(f"anchor not found: {needle.strip()[:60]}", file=sys.stderr)
            return 1

    if not backup.exists():
        shutil.copy2(hud, backup)
        print(f"backed up {backup.name}")

    src = src.replace(BTN_OLD, BTN_NEW, 1)
    src = src.replace(LEVEL_OLD, LEVEL_NEW, 1)
    src = src.replace(BIND_OLD, BIND_NEW, 1)
    src = src.replace(SPACE_OLD, SPACE_NEW, 1)
    src = src.replace(MIC_OLD, MIC_NEW, 1)
    src = src.replace(ERR_OLD, ERR_NEW, 1)
    src = src.replace(anchor, JS + "\n" + anchor, 1)
    hud.write_text(src, encoding="utf-8")
    print(f"patched {hud}")
    print("big button = hands-free session; ring and Space stay push-to-talk, "
          "guarded so the two modes cannot run at once.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
