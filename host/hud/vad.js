/*
 * Energy-based voice activity detection for a hands-free conversation.
 *
 * This is the most-tuned code in the project and every constant in it was
 * moved by a bug someone actually heard. It used to live as a string inside
 * host/patches/apply_handsfree.py, spliced into upstream's HUD — which made it
 * impossible to change the HUD without risking the detector. It is a module
 * now, with no DOM in it at all: it takes a level, and it calls you back.
 *
 *   const vad = createVAD({
 *     onTurnStart: interrupting => prerollMs,   // flush the pre-roll, return its length
 *     onTurnEnd:   reason => {},                // "silence" | "max" — send the turn
 *     onTurnDrop:  () => {},                    // too short — discard, send nothing
 *     isAgentBusy: () => bool,                  // thinking, tool-running or speaking
 *     isEchoRisk:  () => bool,                  // the mic may be hearing the assistant
 *   });
 *   vad.enable(); vad.frame(level);             // one frame every cfg.frameMs
 *
 * Levels are 0..1, however the caller chooses to measure them; the detector
 * only ever compares a level against other levels from the same source.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else { root.createVAD = api.createVAD; root.VAD_DEFAULTS = api.VAD_DEFAULTS; }
})(typeof self !== "undefined" ? self : globalThis, function () {
  "use strict";

  // Rooms differ, so every one of these can be overridden at runtime without a
  // redeploy:  localStorage.setItem("jarvisVad", JSON.stringify({endMs:1200}))
  const VAD_DEFAULTS = {
    frameMs:      80,   // one worklet frame; everything below is milliseconds
    startMs:     160,   // speech must persist this long before a turn opens
    bargeMs:     420,   // ... and longer to interrupt the assistant
    endMs:       650,   // silence this long closes the turn (dead time you feel)
    minTurnMs:   400,   // anything shorter is a cough, not a sentence
    maxTurnMs: 20000,   // hard stop: a turn always ends, whatever the detector thinks
    speakMargin: 3.0,   // level over the room's noise floor to count as speech
    bargeMargin: 6.0,   // ... over the assistant's echo, to count as interrupting
    echoGuardMs: 700,   // keep the strict bar this long after the last audio chunk
    cooldownMs: 1200,   // after sending a turn, refuse to open another at all
    ackDelayMs: 1800,   // wait this long for the real reply before saying "ครับ"
                        // (measured: end of speech to first audio is ~1.9 s, so
                        // an ordinary turn answers itself and this never fires)
    prerollMs:  1200,   // audio kept from *before* the detector fired
                        // 640 ms was not enough: a quiet opening syllable can
                        // take most of a second to cross the threshold and the
                        // first word was still being cut. Leading silence costs
                        // nothing — the STT trims it itself.
    dropRatio:  0.22,   // ... or this fraction of the turn's own peak
    floorUp:    0.02,   // noise floor rises slowly
    floorDown:  0.25,   // ... and falls quickly, to recover after speech
    floorMin:  0.004,
  };

  function readStoredTuning() {
    try { return JSON.parse(localStorage.getItem("jarvisVad") || "{}"); }
    catch (e) { return {}; }
  }

  function createVAD(opts) {
    opts = opts || {};
    const cfg = Object.assign({}, VAD_DEFAULTS, opts.tuning || {});
    const now = opts.now || (() => performance.now());
    const noop = () => {};
    const onTurnStart = opts.onTurnStart || (() => 0);
    const onTurnEnd   = opts.onTurnEnd   || noop;
    const onTurnDrop  = opts.onTurnDrop  || noop;
    const onDebug     = opts.onDebug     || noop;
    const isAgentBusy = opts.isAgentBusy || (() => false);
    const isEchoRisk  = opts.isEchoRisk  || (() => false);

    let enabled = false, capturing = false;
    let speechMs = 0, silenceMs = 0;
    let turnMs = 0, turnSpeechMs = 0, turnPeak = 0;
    let noiseFloor = null, cooldownUntil = 0;
    let lastGate = 0, lastLevel = 0;

    function resetTurn() {
      speechMs = 0; silenceMs = 0;
      turnMs = 0; turnSpeechMs = 0; turnPeak = 0;
    }

    function begin(interrupting) {
      capturing = true;
      // The pre-roll is audio from before the detector fired, so it counts
      // toward how long the turn is...
      turnMs = onTurnStart(interrupting) || 0;
      // ... but NOT toward the speech test. The pre-roll is whatever the room
      // was doing, and only what the detector called speech should let a turn
      // through. Seeding both from the pre-roll is how the assistant came to
      // acknowledge an empty room out loud: turnMs started at 640 and the
      // minimum-length filter passed on a turn containing no speech at all.
      turnSpeechMs = speechMs;
      silenceMs = 0; turnPeak = 0;
    }

    function end(reason) {
      capturing = false;
      // Nothing may open a turn right after one is sent: the acknowledgement
      // may still be playing, the room is still ringing, and a turn opened now
      // would cancel the run that was just submitted.
      cooldownUntil = now() + cfg.cooldownMs;
      resetTurn();
      onTurnEnd(reason);
    }

    function drop() {
      // Too short to be speech — a cough, a door. Nothing is sent: the server
      // only processes a buffer on "stop", and the next "start" clears it, so
      // simply not finishing the turn discards it.
      capturing = false;
      resetTurn();
      onTurnDrop();
    }

    function frame(level) {
      if (!enabled) return;
      lastLevel = level;
      if (!capturing && now() < cooldownUntil) { speechMs = 0; return; }

      const echo   = isEchoRisk();   // is the microphone hearing the assistant?
      const active = isAgentBusy();  // ... or is it working on an answer?

      // Learn the room only when neither side is talking, or the floor climbs
      // to match whoever is speaking and the detector goes deaf. Asymmetric on
      // purpose: rise slowly so a passing noise does not raise the bar, fall
      // quickly so the room is re-learned as soon as a turn ends.
      if (!capturing && !active) {
        const a = (noiseFloor === null || level < noiseFloor) ? cfg.floorDown : cfg.floorUp;
        noiseFloor = (noiseFloor === null) ? level : noiseFloor * (1 - a) + level * a;
      }
      const floor = Math.max(noiseFloor === null ? cfg.floorMin : noiseFloor, cfg.floorMin);

      // The strict bar applies for as long as the agent is working, not only
      // while it is audibly speaking. Tying it to echo alone was a real bug:
      // during the seconds of thinking the loose bar applied, ordinary room
      // noise opened a turn, and opening a turn cancels the run already in
      // flight — so the answer never arrived at all. Being hard to interrupt
      // is a far smaller problem than never being answered.
      const absGate = floor * ((echo || active) ? cfg.bargeMargin : cfg.speakMargin);

      // Absolute levels alone are not enough. Browser auto gain control lifts
      // the signal once you stop talking, so room noise can sit at the level
      // your voice did and an absolute gate never sees silence — the turn then
      // never ends, which is exactly the bug this replaced. Comparing against
      // the loudest frame of THIS turn survives that: whatever the gain does,
      // silence is far below the peak of actual speech.
      if (capturing) turnPeak = Math.max(turnPeak, level);
      const gate = capturing ? Math.max(absGate, turnPeak * cfg.dropRatio) : absGate;
      lastGate = gate;

      if (level > gate) {
        speechMs += cfg.frameMs; silenceMs = 0;
        if (capturing) turnSpeechMs += cfg.frameMs;
      } else {
        silenceMs += cfg.frameMs;
        if (!capturing) speechMs = 0;
      }

      onDebug({ level, gate, floor, silenceMs, speechMs, capturing, active });

      if (capturing) {
        turnMs += cfg.frameMs;
        // The safety net. Even if the detector is wrong about the room, a turn
        // must not hang forever waiting for a silence it will never see.
        if (turnMs >= cfg.maxTurnMs) { end("max"); return; }
        if (silenceMs >= cfg.endMs) {
          // Measured in speech, not in elapsed time: a turn opened by a door
          // closing runs the full end-of-turn window and would otherwise pass.
          if (turnSpeechMs >= cfg.minTurnMs) end("silence"); else drop();
        }
        return;
      }
      if (speechMs >= (active ? cfg.bargeMs : cfg.startMs)) begin(active);
    }

    return {
      cfg,
      frame,
      enable() { enabled = true; noiseFloor = null; resetTurn(); },
      disable() { const wasCapturing = capturing; enabled = false; capturing = false;
                  resetTurn(); return wasCapturing; },
      get enabled() { return enabled; },
      get capturing() { return capturing; },
      // Everything the HUD needs to draw the detector's state honestly.
      state() {
        return { enabled, capturing, level: lastLevel, gate: lastGate,
                 noiseFloor, silenceMs, speechMs, turnMs, turnSpeechMs,
                 cooldownFor: Math.max(0, cooldownUntil - now()) };
      },
    };
  }

  return { createVAD, VAD_DEFAULTS, readStoredTuning };
});
