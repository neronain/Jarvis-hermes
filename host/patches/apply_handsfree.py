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

JS = MARKER + """
// Tunables. Rooms differ, so these can be overridden at runtime without a
// redeploy:  localStorage.setItem("jarvisVad", JSON.stringify({endMs:1200}))
const VAD = Object.assign({
  frameMs:      80,   // one worklet frame; everything below is milliseconds
  startMs:     200,   // speech must persist this long before a turn opens
  bargeMs:     420,   // ... and longer to interrupt the assistant
  endMs:       900,   // silence this long closes the turn
  minTurnMs:   400,   // anything shorter is a cough, not a sentence
  maxTurnMs: 20000,   // hard stop: a turn always ends, whatever the detector thinks
  speakMargin: 3.0,   // level over the room's noise floor to count as speech
  bargeMargin: 6.0,   // ... over the assistant's echo, to count as interrupting
  dropRatio:  0.22,   // ... or this fraction of the turn's own peak (see below)
  floorUp:    0.02,   // noise floor rises slowly
  floorDown:  0.25,   // ... and falls quickly, to recover after speech
  floorMin:  0.004,
}, (()=>{ try{ return JSON.parse(localStorage.getItem("jarvisVad")||"{}") }catch{ return {} } })());
const VAD_DEBUG = !!localStorage.getItem("jarvisVadDebug");

let handsFree=false, vadSpeech=0, vadSilence=0, noiseFloor=null, turnMs=0, turnPeak=0;

function botSpeaking(){
  return activeSources.length>0 && audioCtx && playhead > audioCtx.currentTime + 0.05;
}

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
  const speaking = botSpeaking();

  // Learn the room only when neither side is talking, or the floor climbs to
  // match whoever is speaking and the detector goes deaf. Asymmetric on
  // purpose: rise slowly so a passing noise does not raise the bar, fall
  // quickly so the room is re-learned as soon as a turn ends.
  if(!capturing && !speaking){
    const a = (noiseFloor===null || lvl < noiseFloor) ? VAD.floorDown : VAD.floorUp;
    noiseFloor = (noiseFloor===null) ? lvl : noiseFloor*(1-a) + lvl*a;
  }
  const floor  = Math.max(noiseFloor===null?VAD.floorMin:noiseFloor, VAD.floorMin);
  const absGate = floor * (speaking ? VAD.bargeMargin : VAD.speakMargin);

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
  if(vadSpeech >= (speaking ? VAD.bargeMs : VAD.startMs)) beginTurn(speaking);
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
  handsFree=true; noiseFloor=null; vadSpeech=0; vadSilence=0; turnPeak=0;
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
    if not hud.exists():
        print(f"missing: {hud}", file=sys.stderr)
        return 1

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
    for needle in (BTN_OLD, LEVEL_OLD, BIND_OLD, SPACE_OLD, MIC_OLD, anchor):
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
    src = src.replace(anchor, JS + "\n" + anchor, 1)
    hud.write_text(src, encoding="utf-8")
    print(f"patched {hud}")
    print("big button = hands-free session; ring and Space stay push-to-talk, "
          "guarded so the two modes cannot run at once.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
