/*
 * Tests for the hands-free voice detector.
 *
 * Every case here is a bug someone heard in a live conversation, not a
 * hypothetical. The detector used to live as a JavaScript string inside a
 * Python patcher, where none of this could be run at all.
 *
 *   node --test tests/
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";

const require = createRequire(import.meta.url);
const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const { createVAD, VAD_DEFAULTS } = require(path.join(ROOT, "host/hud/vad.js"));

/** A detector with a clock we control, and a log of everything it did. */
function harness(opts = {}) {
  const log = [];
  let clock = 0;
  let busy = false, echo = false;
  const vad = createVAD({
    now: () => clock,
    tuning: opts.tuning,
    isAgentBusy: () => busy,
    isEchoRisk: () => echo,
    onTurnStart: (interrupting) => { log.push({ ev: "start", interrupting }); return opts.prerollMs || 0; },
    onTurnEnd: (reason) => log.push({ ev: "end", reason }),
    onTurnDrop: () => log.push({ ev: "drop" }),
  });
  const step = VAD_DEFAULTS.frameMs;
  return {
    vad, log,
    set busy(v) { busy = v; },
    set echo(v) { echo = v; },
    get clock() { return clock; },
    /** Feed `ms` of audio at `level`. */
    feed(level, ms) {
      for (let t = 0; t < ms; t += step) { clock += step; vad.frame(level); }
      return this;
    },
    events: (ev) => log.filter((e) => e.ev === ev),
  };
}

const QUIET = 0.004;   // a still room
const TALK  = 0.30;    // someone speaking

test("a turn opens on speech and closes on silence", () => {
  const h = harness();
  h.vad.enable();
  h.feed(QUIET, 2000);                       // learn the room
  assert.equal(h.events("start").length, 0);
  h.feed(TALK, 1200);
  assert.equal(h.events("start").length, 1);
  h.feed(QUIET, 1200);
  assert.deepEqual(h.events("end").map((e) => e.reason), ["silence"]);
});

test("a turn containing no speech is dropped, never sent", () => {
  // The pre-roll used to seed the length AND the speech counter, so a turn
  // opened by a door closing passed the minimum-length filter — and the
  // assistant acknowledged an empty room out loud.
  const h = harness({ prerollMs: 1200 });
  h.vad.enable();
  h.feed(QUIET, 2000);
  h.feed(TALK, 240);            // a bang: just enough to open a turn
  h.feed(QUIET, 1200);          // ... then nothing
  assert.equal(h.events("drop").length, 1, "should have dropped");
  assert.equal(h.events("end").length, 0, "must not send a turn with no speech");
});

test("a turn always ends, even if silence never arrives", () => {
  const h = harness();
  h.vad.enable();
  h.feed(QUIET, 2000);
  h.feed(TALK, 30000);          // someone leaves a fan on
  const ends = h.events("end");
  assert.ok(ends.length >= 1);
  assert.equal(ends[0].reason, "max");
});

test("the strict bar applies while the agent is thinking, not only while it speaks", () => {
  // The regression: the strict bar was tied to echo risk alone, on the
  // reasoning that nothing echoes while the agent is thinking. But opening a
  // turn CANCELS the run in flight, so room noise during those seconds meant
  // the answer never arrived — every turn showed "INTERRUPTED", Turns: 0.
  const nudge = 0.02;           // above speakMargin*floor, below bargeMargin*floor
  const quiet = harness();
  quiet.vad.enable();
  quiet.feed(QUIET, 2000);
  quiet.feed(nudge, 800);
  assert.equal(quiet.events("start").length, 1, "should open when nobody is working");

  const working = harness();
  working.vad.enable();
  working.feed(QUIET, 2000);
  working.busy = true;          // thinking, and silent
  working.echo = false;
  working.feed(nudge, 800);
  assert.equal(working.events("start").length, 0,
    "the same noise must not open a turn while the agent is working");
});

test("speaking over the assistant still interrupts it", () => {
  const h = harness();
  h.vad.enable();
  h.feed(QUIET, 2000);
  h.busy = true; h.echo = true;
  h.feed(TALK, 800);            // longer than bargeMs
  assert.equal(h.events("start").length, 1);
  assert.equal(h.events("start")[0].interrupting, true);
});

test("a new turn cannot open during the cooldown", () => {
  const h = harness();
  h.vad.enable();
  h.feed(QUIET, 2000);
  h.feed(TALK, 1200);
  h.feed(QUIET, 1200);          // turn sent; cooldown starts here
  assert.equal(h.events("end").length, 1);
  h.feed(TALK, 400);            // talking again immediately
  assert.equal(h.events("start").length, 1, "cooldown must swallow this");
  h.feed(QUIET, 1400);          // wait it out
  h.feed(TALK, 400);
  assert.equal(h.events("start").length, 2, "and release afterwards");
});

test("a turn ends even when the noise floor rises to meet the voice", () => {
  // Browser auto gain control lifts the signal once you stop talking, so room
  // noise sits where your voice did and an absolute gate never sees silence.
  // The turn then never ended at all. The peak-relative gate is what fixed it.
  const h = harness();
  h.vad.enable();
  h.feed(QUIET, 2000);
  h.feed(TALK, 1200);
  assert.equal(h.events("start").length, 1);
  h.feed(TALK * 0.15, 1200);    // "silence", but far above the old absolute gate
  assert.deepEqual(h.events("end").map((e) => e.reason), ["silence"]);
});

test("the noise floor rises slowly and falls quickly", () => {
  const h = harness();
  h.vad.enable();
  h.feed(0.05, 4000);                       // a noisy room
  const noisy = h.vad.state().noiseFloor;
  h.feed(QUIET, 1000);                      // it goes quiet
  const settled = h.vad.state().noiseFloor;
  assert.ok(settled < noisy * 0.5,
    `floor should drop fast: ${noisy.toFixed(4)} -> ${settled.toFixed(4)}`);
});

test("disable() reports whether a turn was in flight", () => {
  const h = harness();
  h.vad.enable();
  h.feed(QUIET, 2000);
  h.feed(TALK, 1200);
  assert.equal(h.vad.capturing, true);
  assert.equal(h.vad.disable(), true, "caller must know to close the open turn");
  h.feed(TALK, 2000);
  assert.equal(h.events("end").length, 0, "a disabled detector does nothing");
});

test("tuning overrides reach the detector", () => {
  const h = harness({ tuning: { endMs: 200 } });
  h.vad.enable();
  h.feed(QUIET, 2000);
  h.feed(TALK, 1200);
  h.feed(QUIET, 280);                       // shorter than the default endMs
  assert.equal(h.events("end").length, 1);
});
