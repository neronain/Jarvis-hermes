/*
 * J.A.R.V.I.S Flight Deck — HUD v2.
 *
 * Replaces upstream's index.html rather than patching it. The four patchers
 * that used to splice features into upstream's markup had no anchors left once
 * the layout changed, and the detector they carried is now host/hud/vad.js.
 *
 * Talks to the voice server over the same protocol upstream used, so the
 * server is unchanged:
 *
 *   -> {type:"start", sample_rate, format, channels, conversation}
 *   -> <binary int16 PCM frames at 16 kHz>
 *   -> {type:"stop"} | {type:"stop_run"} | {type:"approval_decision", ...}
 *   <- <binary int16 PCM at the TTS rate>
 *   <- partial_transcript | transcript | run_started | agent_status
 *   <- approval_request | error | done | status | summon_panel
 */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? "" : s)
    .replace(/[<>&"]/g, (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;" }[c]));

  /* The HUD is served behind a token. Upstream accepted ?token= and set a
     cookie; keep that, so existing bookmarks and QR codes still work. */
  (function token() {
    const t = new URLSearchParams(location.search).get("token");
    if (t) {
      document.cookie = "jarvis_token=" + encodeURIComponent(t) + ";path=/;max-age=31536000;samesite=lax";
      history.replaceState({}, "", location.pathname);
    }
  })();

  const CONV = new URLSearchParams(location.search).get("conversation") || "jarvis-main";

  /* ───────────────────────── the token gate ─────────────────────── */

  /* The only way in used to be a URL carrying ?token=, which works when you
     paste a link and is useless the moment you open the HUD from a device's
     own bookmark or type the address on a tablet. The page loaded, every API
     call answered 401, and there was nowhere to put the token. So: ask.

     The origin is shown as well, because the other way in fails — a host that
     is not in security.extra_origin_hosts is refused whatever the token says,
     and the two failures look identical from the sofa. */
  function showTokenGate(reason) {
    if ($("tokenGate")) return;
    const g = document.createElement("div");
    g.id = "tokenGate";
    g.className = "gate";
    g.innerHTML =
      '<form class="gatebox" id="gateForm">' +
      "<h2>ต้องใส่รหัสเข้าใช้งาน</h2>" +
      "<p>" + esc(reason || "เซิร์ฟเวอร์ปฏิเสธคำขอ") + "</p>" +
      '<input id="gateInput" type="password" autocomplete="current-password" ' +
      'placeholder="วางรหัส HUD ที่นี่" spellcheck="false" autocapitalize="off">' +
      '<button class="btn ok" type="submit">เข้าใช้งาน</button>' +
      '<small>หารหัสได้จาก <code>JARVIS_HUD_TOKEN</code> ใน <code>~/.hermes/.env</code> ' +
      'บนเครื่องที่รันเซิร์ฟเวอร์<br>' +
      "ตอนนี้เปิดจาก <code>" + esc(location.host) + "</code> — " +
      "ถ้ารหัสถูกแล้วยังเข้าไม่ได้ ต้องเพิ่มชื่อนี้ใน " +
      "<code>security.extra_origin_hosts</code></small>" +
      "</form>";
    document.body.appendChild(g);
    const input = $("gateInput");
    input.focus();
    $("gateForm").onsubmit = (e) => {
      e.preventDefault();
      const t = input.value.trim();
      if (!t) return;
      document.cookie = "jarvis_token=" + encodeURIComponent(t) +
        ";path=/;max-age=31536000;samesite=lax";
      location.reload();
    };
  }

  const S = {
    ws: null, wsReady: false,
    ttsRate: 24000,          // corrected from /api/loadout before any audio plays
    audioCtx: null, playhead: 0, activeSources: [], leftoverByte: null,
    mediaStream: null, workletReady: false,
    capturing: false, handsFree: false,
    agentBusy: false, lastAudioAt: 0,
    level: 0, turns: 0, currentRun: null,
    lastFrameAt: 0, recovering: false,
    camStream: null, camFacing: "environment", waitTimer: null,
    workBuffers: [], progressTimer: null, progressFirst: null,
    preroll: [], ackBuffers: [], ackTimer: null,
    latency: [], liveEl: null,
    uiState: "standby",
  };

  /* ───────────────────────────── audio out ───────────────────────── */

  function ensureCtx() {
    if (!S.audioCtx) {
      const Ctor = window.AudioContext || window.webkitAudioContext;
      // Playback runs at the synthesiser's own rate. Pinning this to 16 kHz is
      // what capped the voice: F5-TTS generates at 24 kHz and everything above
      // 8 kHz was gone before it reached the speaker. The capture worklet
      // resamples down to 16 kHz for the STT on its way out.
      try { S.audioCtx = new Ctor({ sampleRate: S.ttsRate }); }
      catch (e) { S.audioCtx = new Ctor(); }
    }
    if (S.audioCtx.state === "suspended") S.audioCtx.resume();
    return S.audioCtx;
  }

  function playChunk(buf) {
    const ctx = ensureCtx();
    cancelAck();                    // the real reply beat it
    let bytes = new Uint8Array(buf);
    if (S.leftoverByte !== null) {
      const m = new Uint8Array(bytes.length + 1);
      m[0] = S.leftoverByte; m.set(bytes, 1); bytes = m; S.leftoverByte = null;
    }
    if (bytes.length % 2 === 1) {
      S.leftoverByte = bytes[bytes.length - 1];
      bytes = bytes.subarray(0, bytes.length - 1);
    }
    if (!bytes.length) return;
    const i16 = new Int16Array(bytes.buffer, bytes.byteOffset, bytes.length / 2);
    const f32 = new Float32Array(i16.length);
    for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 32768;
    const ab = ctx.createBuffer(1, f32.length, S.ttsRate);
    ab.copyToChannel(f32, 0);
    const src = ctx.createBufferSource();
    src.buffer = ab; src.connect(ctx.destination);
    const t = Math.max(ctx.currentTime + 0.06, S.playhead);
    src.start(t); S.playhead = t + ab.duration;
    S.activeSources.push(src);
    src.onended = () => { S.activeSources = S.activeSources.filter((x) => x !== src); };
    S.lastAudioAt = performance.now();
  }

  function stopPlayback() {
    S.activeSources.forEach((s) => { try { s.stop(); } catch (e) {} });
    S.activeSources = []; S.playhead = 0;
  }

  function botSpeaking() {
    // A suspended context freezes currentTime while playhead stays ahead of
    // it, so this said "still speaking" forever — and since isAgentBusy() is
    // built on it, the detector held the strict bar and went deaf. That is the
    // "I had to refresh to be heard again" bug, and it needs both halves: the
    // context resumed, and this not lying while it is not running.
    if (!S.audioCtx || S.audioCtx.state !== "running") return false;
    if (!S.activeSources.length) { S.playhead = 0; return false; }
    return S.playhead > S.audioCtx.currentTime + 0.05;
  }
  function echoRisk() {
    return botSpeaking() || (performance.now() - S.lastAudioAt) < vad.cfg.echoGuardMs;
  }

  /* Acknowledgement clips. Armed, never played outright: playing one the
     instant a turn was sent meant room noise that got past the detector was
     acknowledged out loud and then thrown away by the server for having no
     transcript — the agent saying "ครับ" to an empty room. */
  async function loadAcks() {
    S.ackBuffers = [];
    const ctx = ensureCtx();
    // Fetch until the server runs out, rather than assuming a count the
    // server is free to change.
    for (let i = 0; i < 8; i++) {
      try {
        const r = await fetch("/api/ack?i=" + i);
        if (r.status === 404) break;
        if (!r.ok) continue;
        const raw = await r.arrayBuffer();
        if (raw.byteLength < 640) continue;       // under 20 ms is not a word
        const i16 = new Int16Array(raw), f32 = new Float32Array(i16.length);
        for (let k = 0; k < i16.length; k++) f32[k] = i16[k] / 32768;
        const ab = ctx.createBuffer(1, f32.length, S.ttsRate);
        ab.copyToChannel(f32, 0);
        S.ackBuffers.push(ab);
      } catch (e) { /* no acknowledgement is duller, not broken */ }
    }
  }
  /* Progress, spoken. A tool-running turn can go minutes without a sound, and
     one measured 169 seconds before somebody spoke over it out of impatience
     and lost the lot. These say "still here" at 15 s and then every 30 s, so
     the room can tell work from death without looking at the screen. */
  async function loadWorking() {
    S.workBuffers = [];
    const ctx = ensureCtx();
    for (let i = 0; i < 6; i++) {
      try {
        const r = await fetch("/api/working?i=" + i);
        if (r.status === 404) break;
        if (!r.ok) continue;
        const raw = await r.arrayBuffer();
        if (raw.byteLength < 640) continue;
        const i16 = new Int16Array(raw), f32 = new Float32Array(i16.length);
        for (let k = 0; k < i16.length; k++) f32[k] = i16[k] / 32768;
        const ab = ctx.createBuffer(1, f32.length, S.ttsRate);
        ab.copyToChannel(f32, 0);
        S.workBuffers.push(ab);
      } catch (e) {}
    }
  }

  function startProgress() {
    stopProgress();
    let n = 0;
    S.progressTimer = setInterval(() => {
      // Only while nothing else is coming out: over the reply it would be noise.
      if (!S.agentBusy || botSpeaking()) return;
      const b = S.workBuffers;
      if (!b || !b.length || !S.audioCtx) return;
      const ab = b[n++ % b.length];
      const src = S.audioCtx.createBufferSource();
      src.buffer = ab; src.connect(S.audioCtx.destination); src.start();
      S.lastAudioAt = performance.now() + ab.duration * 1000;
    }, 30000);
    S.progressFirst = setTimeout(() => {
      if (S.agentBusy && !botSpeaking() && S.workBuffers && S.workBuffers.length) {
        const src = S.audioCtx.createBufferSource();
        src.buffer = S.workBuffers[0];
        src.connect(S.audioCtx.destination); src.start();
        S.lastAudioAt = performance.now() + S.workBuffers[0].duration * 1000;
      }
    }, 15000);
  }
  function stopProgress() {
    if (S.progressTimer) { clearInterval(S.progressTimer); S.progressTimer = null; }
    if (S.progressFirst) { clearTimeout(S.progressFirst); S.progressFirst = null; }
  }

  function armAck() {
    cancelAck();
    S.ackTimer = setTimeout(() => { S.ackTimer = null; playAck(); }, vad.cfg.ackDelayMs);
  }
  function cancelAck() {
    if (S.ackTimer) { clearTimeout(S.ackTimer); S.ackTimer = null; }
  }
  function playAck() {
    if (!S.ackBuffers.length || !S.audioCtx) return;
    const ab = S.ackBuffers[Math.floor(Math.random() * S.ackBuffers.length)];
    const src = S.audioCtx.createBufferSource();
    src.buffer = ab; src.connect(S.audioCtx.destination); src.start();
    // Deliberately not in activeSources: barge-in must never cancel it. The
    // microphone will hear it though, so the echo guard has to cover it or it
    // opens a turn of its own.
    S.lastAudioAt = performance.now() + ab.duration * 1000;
  }

  /* ───────────────────────────── audio in ────────────────────────── */

  const WORKLET = `
class PCM16K extends AudioWorkletProcessor{
  constructor(){super();this.frac=0;this.acc=0;this.n=0;this.out=[];}
  process(inputs){
    const ch=inputs[0][0]; if(!ch)return true;
    if(sampleRate===16000){
      for(let i=0;i<ch.length;i++){
        const v=Math.max(-1,Math.min(1,ch[i])); this.out.push(v*32767|0);
      }
    }else{                              // averaging downsample to 16 kHz
      for(let i=0;i<ch.length;i++){
        this.acc+=ch[i]; this.n++; this.frac+=16000;
        if(this.frac>=sampleRate){ this.frac-=sampleRate;
          const v=Math.max(-1,Math.min(1,this.acc/this.n));
          this.out.push(v*32767|0); this.acc=0; this.n=0; }
      }
    }
    if(this.out.length>=1280){          // 80 ms — one VAD frame
      const a=new Int16Array(this.out.splice(0,1280));
      this.port.postMessage(a.buffer,[a.buffer]);
    }
    return true;
  }
}
registerProcessor("pcm16k",PCM16K);`;

  const PREROLL_FRAMES = Math.max(1, Math.round(1200 / 80));

  /* Frames arrive every 80 ms while the microphone is live. When they stop,
     the session is deaf and nothing on screen says so — which is precisely
     what "I have to refresh before it hears me again" means. Three things stop
     them, and none of them raises an error:

       the AudioContext suspends (tab hidden, OS audio change, autoplay policy)
       the OS ends the track (sleep/wake, a headset connecting)
       the worklet node is collected or its stream is replaced

     So the state is checked rather than trusted, and rebuilt when it is wrong. */
  async function micWatchdog() {
    if (!S.mediaStream || S.recovering) return;
    const ctx = S.audioCtx;
    if (ctx && ctx.state === "suspended") {
      try { await ctx.resume(); } catch (e) {}
    }
    const track = S.mediaStream.getAudioTracks()[0];
    const trackDead = !track || track.readyState === "ended";
    const silent = S.lastFrameAt && (performance.now() - S.lastFrameAt) > 4000;
    if (!trackDead && !silent) return;

    S.recovering = true;
    micHealth(false, trackDead ? "ไมค์หลุด — กำลังต่อใหม่" : "ไม่ได้ยินเสียงเข้า — กำลังต่อใหม่");
    try {
      try { S.mediaStream.getTracks().forEach((t) => t.stop()); } catch (e) {}
      S.mediaStream = null;
      await initMic();
      micHealth(true, "");
    } catch (err) {
      micHealth(false, "ต่อไมค์ใหม่ไม่สำเร็จ: " + (err.message || err));
    } finally {
      S.recovering = false;
    }
  }

  function micHealth(ok, why) {
    const d = $("micDot"), t = $("micHealth");
    if (d) d.className = "dot " + (ok ? "live" : "down");
    if (t) t.textContent = ok ? "MIC" : (why || "MIC DOWN");
    if (!ok && why) addMsg("sys", why);
  }

  async function initMic() {
    const ctx = ensureCtx();
    if (!S.workletReady) {
      const url = URL.createObjectURL(new Blob([WORKLET], { type: "application/javascript" }));
      await ctx.audioWorklet.addModule(url);
      S.workletReady = true;
    }
    if (S.mediaStream) return;
    S.mediaStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1, sampleRate: 16000,
        echoCancellation: true, noiseSuppression: true,
        // Auto gain lifts the room the moment you stop talking, so noise sits
        // where your voice did and the turn never ends. Off, always.
        autoGainControl: false,
      },
    });
    S.lastFrameAt = performance.now();
    const src = ctx.createMediaStreamSource(S.mediaStream);
    const node = new AudioWorkletNode(ctx, "pcm16k");
    node.port.onmessage = (e) => {
      S.lastFrameAt = performance.now();
      if (S.capturing && S.wsReady) S.ws.send(e.data);
      const a = new Int16Array(e.data);
      let sum = 0;
      for (let i = 0; i < a.length; i += 8) sum += Math.abs(a[i]);
      S.level = Math.min(1, (sum / (a.length / 8)) / 9000);
      if (!S.capturing && S.handsFree) {
        S.preroll.push(e.data);
        while (S.preroll.length > PREROLL_FRAMES) S.preroll.shift();
      }
      vad.frame(S.level);
      paintLevel();
    };
    src.connect(node);
    // A worklet with no downstream connection is stopped by some browsers.
    const sink = ctx.createGain(); sink.gain.value = 0;
    node.connect(sink); sink.connect(ctx.destination);
  }

  /* ───────────────────────────── the detector ────────────────────── */

  const vad = window.createVAD({
    tuning: (function () {
      try { return JSON.parse(localStorage.getItem("jarvisVad") || "{}"); }
      catch (e) { return {}; }
    })(),
    isAgentBusy: () => S.agentBusy || echoRisk(),
    isEchoRisk: echoRisk,
    onTurnStart(interrupting) {
      if (interrupting) { stopPlayback(); if (S.wsReady) S.ws.send(JSON.stringify({ type: "stop_run" })); }
      S.ws.send(JSON.stringify({
        type: "start", sample_rate: 16000, format: "pcm_s16le", channels: 1, conversation: CONV,
      }));
      const flushed = S.preroll.slice();
      S.preroll = [];
      flushed.forEach((b) => S.ws.send(b));
      S.capturing = true;
      setState("listening", interrupting ? "ขัดจังหวะ — เชิญเลยครับ" : "กำลังฟัง…");
      return flushed.length * 80;        // the pre-roll counts toward turn length
    },
    onTurnEnd(reason) {
      S.capturing = false;
      S.ws.send(JSON.stringify({ type: "stop" }));
      armAck();
      setState("thinking", reason === "max" ? "ส่งแล้ว (ยาวเกินกำหนด)" : "กำลังคิด…");
    },
    onTurnDrop() {
      S.capturing = false; S.preroll = [];
      setState("standby", "พูดได้เลย ไม่ต้องกด");
    },
  });

  /* ───────────────────────────── UI state ────────────────────────── */

  const STATE_LABEL = {
    standby: "พร้อม", listening: "กำลังฟัง", thinking: "กำลังคิด",
    tool: "กำลังทำงาน", speaking: "กำลังพูด",
  };

  /* Three seconds of silence after the acknowledgement and a person assumes
     they were not heard. The turn log says otherwise — every turn answered,
     none interrupted — but time to first audio runs from three seconds to
     twenty-seven when the session is long or a tool is involved, and nothing
     on screen or in the room said so. Now the wait counts itself. */
  function startWaitClock() {
    stopWaitClock();
    const began = performance.now();
    S.waitTimer = setInterval(() => {
      const sec = Math.round((performance.now() - began) / 1000);
      const h = $("stateHint");
      if (h) h.textContent = sec < 4 ? "กำลังคิด…" : "กำลังคิด… " + sec + " วินาที";
      const b = $("waitBadge");
      if (b) { b.hidden = sec < 4; b.textContent = sec + "s"; }
    }, 500);
  }
  function stopWaitClock() {
    if (S.waitTimer) { clearInterval(S.waitTimer); S.waitTimer = null; }
    const b = $("waitBadge"); if (b) b.hidden = true;
  }

  function setState(st, hint) {
    if (st === "thinking" || st === "tool") { startWaitClock(); startProgress(); }
    else { stopWaitClock(); stopProgress(); }
    S.uiState = st;
    S.agentBusy = (st === "thinking" || st === "tool" || st === "speaking");
    if (st === "standby") cancelAck();
    const el = $("stateName"); if (el) el.textContent = STATE_LABEL[st] || st;
    const h = $("stateHint"); if (h && hint !== undefined) h.textContent = hint;
    document.body.dataset.state = st;
  }

  function paintLevel() {
    const bar = $("levelBar");
    if (bar) bar.style.width = (S.level * 100).toFixed(0) + "%";
  }

  /* ───────────────────────────── transcript ──────────────────────── */

  function addMsg(who, text) {
    const feed = $("thread");
    if (!feed) return null;
    const empty = feed.querySelector(".empty"); if (empty) empty.remove();
    const wrap = document.createElement("div");
    wrap.className = "msg" + (who === "me" ? " me" : "") + (who === "sys" ? " sys" : "");
    if (who === "sys") {
      wrap.innerHTML = '<div class="note-line">' + esc(text) + "</div>";
    } else {
      wrap.innerHTML = '<div class="av">' + (who === "me" ? "ME" : "AI") + "</div>" +
        '<div class="bub">' + esc(text) + "</div>";
    }
    feed.appendChild(wrap);
    feed.scrollTop = feed.scrollHeight;
    while (feed.children.length > 60) feed.removeChild(feed.firstChild);
    return wrap;
  }

  function addMeta(text) {
    const feed = $("thread"); if (!feed) return;
    const d = document.createElement("div");
    d.className = "meta"; d.textContent = text;
    feed.appendChild(d); feed.scrollTop = feed.scrollHeight;
  }

  function showLive(text) {
    if (!text) return;
    if (!S.liveEl) { S.liveEl = addMsg("me", ""); if (S.liveEl) S.liveEl.classList.add("live"); }
    if (S.liveEl) S.liveEl.querySelector(".bub").textContent = text;
  }
  function clearLive() { if (S.liveEl) { S.liveEl.remove(); S.liveEl = null; } }

  function showApproval(e) {
    const data = e.data || {};
    const id = data.approval_id || data.id || "";
    const desc = data.preview || data.command || data.description || JSON.stringify(data).slice(0, 300);
    const card = document.createElement("div");
    card.className = "appr";
    card.innerHTML = "<h3>ขออนุมัติก่อนทำ</h3><pre>" + esc(desc) + "</pre>" +
      '<div class="rowbtn"><button class="btn ok">อนุญาต</button><button class="btn no">ปฏิเสธ</button></div>';
    const [allow, deny] = card.querySelectorAll("button");
    const send = (d) => {
      if (S.wsReady) S.ws.send(JSON.stringify({
        type: "approval_decision", run_id: e.run_id || S.currentRun, approval_id: id, decision: d,
      }));
      card.remove();
    };
    allow.onclick = () => send("allow");
    deny.onclick = () => send("deny");
    $("approvals").appendChild(card);
  }

  /* ─────────────────────── summoned panels ──────────────────────── */

  /* The agent can put something on the HUD mid-conversation — a map, a feed,
     an image — by POSTing /api/map or /api/summon. It takes the transcript's
     place rather than floating over it: a map is something you look at, and
     half a conversation showing through helps nobody. The ✕ puts the
     conversation back, and nothing was lost while it was away. */
  const MODE_LABEL = {
    satellite: "ดาวเทียม", roadmap: "แผนที่", earth: "3 มิติ",
    streetview: "สตรีทวิว", directions: "เส้นทาง",
  };

  function summonPanel(e) {
    const body = $("panelBody"); if (!body) return;
    $("panelTitle").textContent = e.title || "แผนที่";
    $("panelMode").textContent = MODE_LABEL[e.mode] || (e.media || "").toUpperCase();
    const src = e.src || "";
    if (e.media === "camera") { openCamera(e.facing); return; }
    if (e.media === "image") {
      body.innerHTML = '<img alt="' + esc(e.title || "") + '" src="' + esc(src) + '">';
    } else if (e.media === "video") {
      body.innerHTML = '<video controls autoplay playsinline src="' + esc(src) + '"></video>';
    } else if (!src) {
      body.innerHTML = '<div class="msg-empty"><div><b>ไม่มีอะไรจะแสดง</b>' +
        'คำสั่งมาถึงแล้วแต่ไม่มี src</div></div>';
    } else {
      // No sandbox attribute: allow-scripts together with allow-same-origin
      // sandboxes nothing (the browser warns about exactly this), and the src
      // is never user input — the server builds it from a template, so the
      // frame is either Google's own embed or a page we serve.
      body.innerHTML = '<iframe src="' + esc(src) + '" ' +
        'referrerpolicy="no-referrer-when-downgrade" ' +
        'allow="fullscreen" allowfullscreen title="' + esc(e.title || "panel") + '"></iframe>';
    }
    $("chatCard").hidden = true;
    $("stagePanel").hidden = false;
  }

  function dismissPanel() {
    const p = $("stagePanel");
    if (!p || p.hidden) return;
    stopCamera();                      // the indicator light goes off with it
    p.hidden = true;
    $("panelBody").innerHTML = "";     // stop whatever was playing or polling
    $("panelActions").innerHTML = "";
    $("chatCard").hidden = false;
  }

  /* ─────────────────────────── the camera ───────────────────────── */

  /* Shown in the same panel as a map, and closed the same way: ✕ or Escape.
     Closing stops the track rather than only hiding the preview — a camera
     that is still running behind a hidden element is a camera the person
     thinks they turned off. */
  function stopCamera() {
    if (!S.camStream) return;
    try { S.camStream.getTracks().forEach((t) => t.stop()); } catch (e) {}
    S.camStream = null;
  }

  /* The panel always opens, and holds either the preview or the reason there
     isn't one. When it failed silently the agent said "camera's open" — true
     from where it stood, since it had asked — while the screen showed nothing
     and the message explaining why scrolled past in the transcript. */
  function cameraProblem(title, detail) {
    $("panelTitle").textContent = "กล้อง";
    $("panelMode").textContent = "";
    $("panelBody").innerHTML = '<div class="msg-empty"><div><b>' + esc(title) + "</b>" +
      detail + "</div></div>";
    $("panelActions").innerHTML = "";
    $("chatCard").hidden = true;
    $("stagePanel").hidden = false;
  }

  async function openCamera(facing) {
    if (!window.isSecureContext) {
      return cameraProblem("เปิดกล้องบน http ไม่ได้",
        "เบราว์เซอร์อนุญาตให้ใช้กล้องเฉพาะหน้าเว็บที่ปลอดภัย<br>" +
        "เปิด HUD ผ่าน <code>https</code> พอร์ต <code>8766</code> แทนพอร์ต 8765");
    }
    S.camFacing = facing || S.camFacing || "environment";
    stopCamera();
    try {
      S.camStream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: S.camFacing, width: { ideal: 1920 }, height: { ideal: 1080 } },
        audio: false,
      });
    } catch (err) {
      const name = err && err.name;
      return cameraProblem(
        name === "NotAllowedError" ? "ยังไม่ได้อนุญาตให้ใช้กล้อง"
          : name === "NotFoundError" ? "ไม่พบกล้องบนเครื่องนี้"
          : "เปิดกล้องไม่ได้",
        esc(err && err.message ? err.message : String(err)));
    }
    $("panelTitle").textContent = "กล้อง";
    $("panelMode").textContent = S.camFacing === "user" ? "กล้องหน้า" : "กล้องหลัง";
    const body = $("panelBody");
    body.innerHTML = '<video id="camView" autoplay playsinline muted></video>';
    $("camView").srcObject = S.camStream;
    $("panelActions").innerHTML =
      '<button class="btn" id="camShot">ถ่ายแล้วให้อ่าน</button>' +
      '<button class="btn" id="camFlip">สลับกล้อง</button>' +
      '<input id="camAsk" type="text" placeholder="อยากถามอะไรเกี่ยวกับภาพนี้ (ไม่ใส่ก็ได้)">';
    $("camShot").onclick = captureAndAsk;
    $("camFlip").onclick = () => openCamera(S.camFacing === "user" ? "environment" : "user");
    $("camAsk").addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); captureAndAsk(); }
    });
    $("chatCard").hidden = true;
    $("stagePanel").hidden = false;
  }

  async function captureAndAsk() {
    const v = $("camView");
    if (!v || !v.videoWidth) { addMsg("sys", "กล้องยังไม่พร้อม รออีกครู่"); return; }
    // 1280 wide is where a document stops getting more readable and the
    // upload starts getting slower.
    const w = Math.min(1280, v.videoWidth);
    const h = Math.round(v.videoHeight * (w / v.videoWidth));
    const c = document.createElement("canvas");
    c.width = w; c.height = h;
    c.getContext("2d").drawImage(v, 0, 0, w, h);
    const image = c.toDataURL("image/jpeg", 0.82);

    const ask = $("camAsk");
    const question = ask ? ask.value.trim() : "";
    const btn = $("camShot");
    if (btn) { btn.disabled = true; btn.textContent = "กำลังดู…"; }
    addMsg("me", question || "[ภาพจากกล้อง]");
    setState("thinking", "กำลังดูภาพ…");
    try {
      const r = await fetch("/api/look", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ image, question, conversation: CONV }),
      });
      const j = await r.json();
      if (j.error) addMsg("sys", j.error);
      else if (j.text) addMsg("ai", j.text);
      S.turns++;
    } catch (err) {
      addMsg("sys", "ส่งภาพไม่สำเร็จ: " + err.message);
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = "ถ่ายแล้วให้อ่าน"; }
      setState("standby", S.handsFree ? "พูดได้เลย ไม่ต้องกด" : "กดปุ่ม AI เพื่อเริ่มคุยต่อเนื่อง");
    }
  }

  /* Typed "แผนที่ <ที่ไหน>" is a shortcut past the agent, for when you just
     want to look at somewhere. "ทางไป <ที่ไหน>" adds your own position. */
  const MAP_PREFIX = /^\s*(?:แผนที่|map|ดาวเทียม|satellite|earth|โลก3มิติ|3d)\s+(.+)$/i;
  const CAM_PREFIX = /^\s*(?:เปิดกล้อง|กล้อง|ดูนี่|อ่านเอกสาร|ถ่ายรูป|camera|look)\s*(.*)$/i;
  const ROUTE_PREFIX =
    /^\s*(?:ทางไป|เส้นทางไป|เส้นทาง|ไปยัง|จาก(?:ที่|พิกัด)?(?:ผม|ฉัน)?(?:อยู่)?ไป|route to|directions to)\s+(.+)$/i;

  /* Only the browser knows where it is, and only with permission — so this is
     asked for at the moment somebody wants a route, not on page load, where a
     permission prompt out of nowhere gets refused on reflex.
     Geolocation needs a secure context: it works on the https port (8766) and
     is simply absent on plain http, which is worth saying out loud rather than
     failing silently. */
  async function shareLocation() {
    if (!navigator.geolocation) throw new Error("เบราว์เซอร์นี้ไม่รองรับการระบุตำแหน่ง");
    if (!window.isSecureContext) {
      throw new Error("ต้องเปิด HUD ผ่าน https (พอร์ต 8766) — เบราว์เซอร์ไม่ให้ขอตำแหน่งบน http ธรรมดา");
    }
    const pos = await new Promise((res, rej) =>
      navigator.geolocation.getCurrentPosition(res, rej,
        { enableHighAccuracy: true, timeout: 12000, maximumAge: 120000 }));
    await fetch("/api/here", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        lat: pos.coords.latitude, lng: pos.coords.longitude,
        accuracy: Math.round(pos.coords.accuracy),
      }),
    });
    return Math.round(pos.coords.accuracy);
  }

  async function routeFromHere(destination) {
    addMsg("sys", "กำลังขอตำแหน่งของคุณ…");
    let acc;
    try { acc = await shareLocation(); }
    catch (e) {
      addMsg("sys", "ขอตำแหน่งไม่สำเร็จ: " + (e.message || "ถูกปฏิเสธ"));
      return;
    }
    addMsg("sys", "ได้ตำแหน่งแล้ว (คลาดเคลื่อน ~" + acc + " เมตร)");
    return requestMap(null, "directions", { origin: "here", destination });
  }

  async function requestMap(q, mode, extra) {
    try {
      const r = await fetch("/api/map", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(Object.assign({ q, mode: mode || "satellite" }, extra || {})),
      });
      const j = await r.json();
      if (j.error) {
        $("panelTitle").textContent = q || (extra && extra.destination) || "แผนที่";
        $("panelMode").textContent = "";
        $("panelBody").innerHTML = '<div class="msg-empty"><div><b>เปิดแผนที่ไม่ได้</b>' +
          esc(j.error) + "</div></div>";
        $("chatCard").hidden = true; $("stagePanel").hidden = false;
      }
    } catch (err) { addMsg("sys", "เรียกแผนที่ไม่สำเร็จ: " + err.message); }
  }

  /* ───────────────────────────── websocket ───────────────────────── */

  function connect() {
    const url = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws";
    S.ws = new WebSocket(url);
    S.ws.binaryType = "arraybuffer";
    S.ws.onopen = () => { S.wsReady = true; linkDot(true); };
    S.ws.onclose = (ev) => {
      S.wsReady = false; linkDot(false);
      if (ev && ev.code === 4401) {
        // The server refuses a socket for two different reasons with one code:
        // a wrong token, or an origin it does not know. Say both.
        showTokenGate("เซิร์ฟเวอร์ปฏิเสธการเชื่อมต่อ (รหัสผิด หรือยังไม่อนุญาตที่อยู่นี้)");
        return;                       // reconnecting in a loop helps nobody
      }
      setState("standby", "ลิงก์หลุด — กำลังต่อใหม่");
      setTimeout(connect, 3000);
    };
    S.ws.onerror = () => {};
    S.ws.onmessage = (ev) => {
      if (ev.data instanceof ArrayBuffer) { playChunk(ev.data); return; }
      let e; try { e = JSON.parse(ev.data); } catch (err) { return; }
      switch (e.type) {
        case "summon_panel": summonPanel(e); break;
        case "dismiss_panels": dismissPanel(); break;
        case "partial_transcript": showLive(e.text); break;
        case "transcript": clearLive(); if (e.text) addMsg("me", e.text); break;
        case "run_started": S.currentRun = e.run_id; $("stopBtn").hidden = false; break;
        case "approval_request": showApproval(e); break;
        case "agent_status":
          if (e.state === "thinking") { setState("thinking", "กำลังคิด…"); $("stopBtn").hidden = false; }
          else if (e.state === "tool_use") { setState("tool", e.tool || "เรียกเครื่องมือ"); }
          else if (e.state === "speaking") setState("speaking", "กำลังพูด");
          else if (e.state === "stopped") {
            setState("standby", S.handsFree ? "พูดได้เลย ไม่ต้องกด" : "กดวงแหวนหรือ Space");
            $("stopBtn").hidden = true; stopPlayback();
          }
          break;
        case "error": {
          clearLive(); $("stopBtn").hidden = true;
          // A dropped turn is the silence gate working, not a failure worth
          // shouting about while hands-free is running.
          const quiet = S.handsFree && /no transcript/i.test(e.message || "");
          if (/cancelled|barge-in/i.test(e.message || "")) {
            // Silence is the one answer that tells you nothing. A turn that
            // ran for minutes and was then interrupted has to say so, or it
            // looks exactly like a turn that was never heard.
            addMsg("sys", "เทิร์นนั้นถูกยกเลิกกลางทาง — พูดใหม่ได้เลยครับ");
          } else if (!quiet) addMsg("sys", e.message);
          setState("standby", S.handsFree ? "พูดได้เลย ไม่ต้องกด" : "กดวงแหวนหรือ Space");
          break;
        }
        case "done": {
          S.currentRun = null; $("stopBtn").hidden = true;
          const tm = e.timing || {};
          S.turns++;
          if (tm.response_text) addMsg("ai", tm.response_text);
          const bits = [];
          if (tm.stt_finalize_seconds != null) bits.push("STT " + tm.stt_finalize_seconds + "s");
          if (tm.llm_time_to_first_token_seconds != null) bits.push("LLM " + tm.llm_time_to_first_token_seconds + "s");
          if (tm.end_of_speech_to_first_audio_seconds != null) {
            const v = tm.end_of_speech_to_first_audio_seconds;
            bits.push("เสียงแรก " + v + "s");
            S.latency.push(v); if (S.latency.length > 30) S.latency.shift();
            drawSpark();
          }
          if (bits.length) addMeta(bits.join(" · "));
          setTimeout(() => { if (S.uiState !== "listening") setState("standby"); }, 400);
          break;
        }
        default: break;
      }
    };
  }

  /* A websocket that dies without closing never fires onclose, so wsReady
     stays true and every send goes into a hole. readyState is the truth. */
  function socketWatchdog() {
    if (!S.ws) return;
    const rs = S.ws.readyState;
    if (rs === WebSocket.OPEN) return;
    if (rs === WebSocket.CONNECTING) return;
    if (S.wsReady) { S.wsReady = false; linkDot(false); connect(); }
  }

  function linkDot(up) {
    const d = $("linkDot"); if (d) d.className = "dot " + (up ? "live" : "down");
    const t = $("linkText"); if (t) t.textContent = up ? "UP" : "DOWN";
  }

  /* ───────────────────────────── controls ───────────────────────── */

  async function toggleHandsFree() {
    if (!S.wsReady) { addMsg("sys", "ยังต่อกับ voice server ไม่ได้"); return; }
    if (S.handsFree) {
      S.handsFree = false;
      if (vad.disable() && S.capturing) { S.capturing = false; S.ws.send(JSON.stringify({ type: "stop" })); }
      S.preroll = []; cancelAck();
      $("coreBtn").setAttribute("aria-pressed", "false");
      setState("standby", "กดปุ่ม AI เพื่อเริ่มคุยต่อเนื่อง");
      addMsg("sys", "ปิดโหมดคุยต่อเนื่องแล้ว");
      return;
    }
    try { await initMic(); }
    catch (err) { addMsg("sys", "เปิดไมค์ไม่ได้: " + err.message); return; }
    loadAcks(); loadWorking();         // not awaited: the first turn can go without
    S.handsFree = true;
    micHealth(true, "");
    vad.enable();
    $("coreBtn").setAttribute("aria-pressed", "true");
    setState("standby", "พูดได้เลย ไม่ต้องกด");
    addMsg("sys", "เปิดโหมดคุยต่อเนื่อง — พูดได้เลย พูดแทรกได้ตลอด");
  }

  /* Push-to-talk shares the capture state with hands-free, so one has to win
     rather than both racing. */
  async function pushToTalk() {
    if (S.handsFree) { addMsg("sys", "โหมดคุยต่อเนื่องเปิดอยู่ — พูดได้เลย"); return; }
    if (!S.wsReady) return;
    if (S.capturing) {
      S.capturing = false;
      S.ws.send(JSON.stringify({ type: "stop" }));
      $("micLabel").textContent = "แตะเพื่อพูดครั้งเดียว";
      setState("thinking", "กำลังคิด…");
      return;
    }
    try { await initMic(); } catch (err) { addMsg("sys", "เปิดไมค์ไม่ได้: " + err.message); return; }
    stopPlayback();
    S.ws.send(JSON.stringify({
      type: "start", sample_rate: 16000, format: "pcm_s16le", channels: 1, conversation: CONV,
    }));
    S.capturing = true;
    $("micLabel").textContent = "แตะอีกครั้งเพื่อส่ง";
    setState("listening", "กำลังฟัง… แตะอีกครั้งเพื่อส่ง");
  }

  function stopRun() {
    stopPlayback();
    if (S.wsReady) S.ws.send(JSON.stringify({ type: "stop_run" }));
    $("stopBtn").hidden = true;
    setState("standby");
  }

  /* Typed turns answer over HTTP, not the socket: /api/chat runs the turn and
     returns the whole reply in its response. Voice turns stream back through
     the websocket instead, which is why these two paths look different. */
  async function sendText(text) {
    if (!text) return;
    const cam = text.match(CAM_PREFIX);
    if (cam) {
      addMsg("me", text);
      return openCamera(/หน้า|front|selfie/i.test(cam[1] || "") ? "user" : "environment");
    }
    const r = text.match(ROUTE_PREFIX);
    if (r) {
      addMsg("me", text);
      return routeFromHere(r[1].trim());
    }
    const m = text.match(MAP_PREFIX);
    if (m) {
      addMsg("me", text);
      const mode = /earth|3d|โลก3มิติ/i.test(text) ? "earth"
        : /ดาวเทียม|satellite/i.test(text) ? "satellite" : "satellite";
      return requestMap(m[1].trim(), mode);
    }
    addMsg("me", text);
    setState("thinking", "กำลังคิด…");
    try {
      const r = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ input: text, conversation: CONV }),
      });
      const j = await r.json();
      if (j.error) addMsg("sys", j.error);
      else if (j.text) addMsg("ai", j.text);
      (j.tools || []).forEach((t) => addMeta("เครื่องมือ · " + (t.name || "tool")));
      S.turns++;
    } catch (e) {
      addMsg("sys", "ส่งข้อความไม่สำเร็จ: " + e.message);
    } finally {
      setState("standby", S.handsFree ? "พูดได้เลย ไม่ต้องกด" : "กดปุ่ม AI เพื่อเริ่มคุยต่อเนื่อง");
    }
  }

  /* ───────────────────────────── panels ─────────────────────────── */

  async function getJSON(url) {
    const r = await fetch(url);
    if (r.status === 401) {
      showTokenGate("รหัสไม่ถูกต้อง หรือยังไม่ได้ใส่");
      throw new Error("unauthorised");
    }
    if (!r.ok) throw new Error(url + " → HTTP " + r.status);
    return r.json();
  }

  async function refreshLoadout() {
    try {
      const j = await getJSON("/api/loadout");
      if (j.tts_rate && !S.audioCtx) S.ttsRate = Number(j.tts_rate) || S.ttsRate;
      $("ldBrain").textContent = j.brain || "—";
      $("ldStt").textContent = j.stt || "—";
      $("ldTts").textContent = j.tts || "—";
      $("ldVoice").textContent = j.voice || "—";
      $("rateBadge").textContent = (S.ttsRate / 1000) + " kHz";
      const dead = /offline|down/i.test((j.stt || "") + " " + (j.tts || ""));
      $("ldCard").classList.toggle("degraded", dead);
    } catch (e) { /* the card keeps its last known values */ }
  }

  async function refreshWarm() {
    try {
      const j = await getJSON("/api/warm");
      const el = $("warmText"); if (!el) return;
      el.textContent = j.warm ? "WARM" : "COLD";
      $("warmDot").className = "dot " + (j.warm ? "warm" : "down");
      $("warmAge").textContent = j.age_seconds != null ? Math.round(j.age_seconds) + "s ago" : "—";
    } catch (e) {}
  }

  const TEMP_HISTORY = [];

  async function refreshMachines() {
    let j;
    try { j = await getJSON("/api/machines"); } catch (e) { return; }
    const list = [].concat(j.machines || [], j.mac ? [j.mac] : []);
    const rows = list.map((m) => {
      const online = m.online !== false;
      const gpus = m.gpu || [];
      const hot = gpus.length ? Math.max.apply(null, gpus.map((g) => g.temp_c || 0)) : null;
      const badge = !online ? '<span class="pill idle">OFFLINE</span>'
        : hot != null ? '<span class="pill ' + tempClass(hot) + '">' + hot + "°C</span>"
        : '<span class="pill ok">UP</span>';
      const sub = gpus.length
        ? gpus.length + " GPU · RAM " + fmtPct(m.ram) + " · ดิสก์ " + fmtPct(m.disk_pct)
        : (m.note || "voice server");
      return '<div class="row"><div class="ico c">' + ICON.chip + "</div>" +
        '<div class="body"><b>' + esc(m.name || "—") + "</b><span>" + esc(sub) + "</span></div>" +
        '<div class="end">' + badge + "</div></div>";
    });
    $("machineRows").innerHTML = rows.join("") ||
      '<div class="empty">ยังไม่มีเครื่องที่รายงานเข้ามา</div>';

    // The thermal card reads the hottest GPU on the node, and keeps a short
    // history so the strip means something rather than repeating one number.
    const node = list.find((m) => (m.gpu || []).length);
    if (node) {
      const g = node.gpu.slice().sort((a, b) => (b.temp_c || 0) - (a.temp_c || 0))[0];
      $("gpuTemp").textContent = Math.round(g.temp_c || 0);
      $("gpuName").textContent = g.name || node.name || "GPU";
      $("gpuVram").textContent = (g.mem_used_mb / 1024).toFixed(1) + " / " +
        (g.mem_total_mb / 1024).toFixed(1) + " GB";
      $("gpuUtil").textContent = Math.round(g.util || 0) + "%";
      $("gpuSecond").textContent = node.gpu.length > 1
        ? Math.round(node.gpu[1].temp_c || 0) + "°C" : "—";
      TEMP_HISTORY.push(Math.round(g.temp_c || 0));
      while (TEMP_HISTORY.length > 5) TEMP_HISTORY.shift();
      drawStrip();
    }
  }

  function tempClass(t) { return t >= 85 ? "crit" : t >= 75 ? "warn" : "ok"; }
  function fmtPct(v) { return v == null ? "—" : Math.round(v) + "%"; }

  function drawStrip() {
    const el = $("tempStrip"); if (!el) return;
    const n = TEMP_HISTORY.length;
    el.innerHTML = TEMP_HISTORY.map((v, i) => {
      const label = i === n - 1 ? "ตอนนี้" : "-" + ((n - 1 - i) * 30) + "s";
      const h = Math.max(4, Math.min(100, ((v - 30) / 60) * 100));
      return '<div><div class="t">' + label + '</div><div class="b"><i style="height:' +
        h.toFixed(0) + '%"></i></div><div class="v">' + v + "°</div></div>";
    }).join("");
  }

  async function refreshUsage() {
    try {
      const j = await getJSON("/api/usage");
      $("uTurns").textContent = j.turns != null ? j.turns : "—";
      $("uIn").textContent = fmtK(j.llm_in);
      $("uOut").textContent = fmtK(j.llm_out);
      $("uChars").textContent = fmtK(j.tts_chars);
    } catch (e) {}
  }
  function fmtK(v) {
    if (v == null) return "—";
    return v >= 1000 ? (v / 1000).toFixed(1) + "k" : String(v);
  }

  function drawSpark() {
    const line = $("sparkLine"), fill = $("sparkFill"), end = $("sparkEnd");
    if (!line) return;
    const d = S.latency;
    if (d.length < 2) { line.setAttribute("d", ""); fill.setAttribute("d", ""); return; }
    const W = 300, H = 70, P = 8;
    const lo = Math.min.apply(null, d), hi = Math.max.apply(null, d);
    const span = (hi - lo) || 1;
    const x = (i) => i * (W / (d.length - 1));
    const y = (v) => P + (1 - (v - lo) / span) * (H - P * 2);
    let path = "M" + x(0) + "," + y(d[0]);
    for (let i = 1; i < d.length; i++) path += "L" + x(i) + "," + y(d[i]);
    line.setAttribute("d", path);
    fill.setAttribute("d", path + "L" + W + "," + H + "L0," + H + "Z");
    end.setAttribute("cx", x(d.length - 1)); end.setAttribute("cy", y(d[d.length - 1]));
    $("sparkHi").textContent = hi.toFixed(1) + "s";
    $("sparkLo").textContent = lo.toFixed(1) + "s";
    $("latNow").textContent = d[d.length - 1].toFixed(1);
    const avg = d.reduce((a, b) => a + b, 0) / d.length;
    $("latAvg").textContent = "เฉลี่ย " + avg.toFixed(1) + "s จาก " + d.length + " เทิร์น";
  }

  const ICON = {
    chip: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4"/></svg>',
  };

  /* ───────────────────────────── views ──────────────────────────── */

  function showView(name) {
    document.querySelectorAll("[data-view]").forEach((el) => {
      el.hidden = el.dataset.view !== name;
    });
    document.querySelectorAll(".nav button[data-goto]").forEach((b) => {
      b.setAttribute("aria-current", String(b.dataset.goto === name));
    });
    if (name === "dashboard") {
      const f = $("dashFrame");
      if (f && !f.src) f.src = (window.JARVIS_DASHBOARD_URL || "/dashboard/");
    }
  }

  /* ───────────────────────────── the orb ────────────────────────── */

  const orb = $("orb"), oc = orb.getContext("2d");
  const micC = $("micring"), mc = micC.getContext("2d");
  let t = 0, shown = 0.2;
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;

  function fit(c) {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = c.clientWidth || 600, h = c.clientHeight || 260;
    c.width = Math.round(w * dpr); c.height = Math.round(h * dpr);
  }
  function fitAll() { fit(orb); fit(micC); }
  fitAll();
  addEventListener("resize", () => { fitAll(); if (reduce) { drawOrb(); drawMic(); } });

  function ring(ctx, cx, cy, r, from, to, width, alpha, dash) {
    const g = ctx.createLinearGradient(cx - r, cy - r, cx + r, cy + r);
    g.addColorStop(0, "rgba(53,232,255," + alpha + ")");
    g.addColorStop(0.55, "rgba(124,92,255," + alpha + ")");
    g.addColorStop(1, "rgba(255,79,216," + alpha * 0.8 + ")");
    ctx.strokeStyle = g; ctx.lineWidth = width; ctx.lineCap = "round";
    ctx.setLineDash(dash || []);
    ctx.beginPath(); ctx.arc(cx, cy, r, from, to); ctx.stroke();
    ctx.setLineDash([]);
  }

  function drawOrb() {
    const W = orb.width, H = orb.height, cx = W / 2, cy = H / 2;
    oc.clearRect(0, 0, W, H);
    const base = Math.min(H * 0.195, W * 0.13) * (1 + shown * 0.1);
    const u = base * 0.022;

    const gl = oc.createRadialGradient(cx, cy, 0, cx, cy, base * 2.5);
    gl.addColorStop(0, "rgba(124,92,255,.40)");
    gl.addColorStop(0.4, "rgba(53,232,255,.13)");
    gl.addColorStop(1, "rgba(5,6,15,0)");
    oc.fillStyle = gl; oc.fillRect(0, 0, W, H);

    ring(oc, cx, cy, base * 2.35, t * 0.35, t * 0.35 + 2.1, u * 2, 0.34);
    ring(oc, cx, cy, base * 2.05, -t * 0.5 + 1.2, -t * 0.5 + 2.6, u * 1.5, 0.26);
    ring(oc, cx, cy, base * 1.72, t * 0.8 + 3.4, t * 0.8 + 5.0, u * 3, 0.5);
    ring(oc, cx, cy, base * 1.45, -t * 0.22, Math.PI * 2 - t * 0.22, u, 0.16, [u * 3, u * 9]);

    for (let i = 0; i < 64; i++) {
      const a = (i / 64) * Math.PI * 2 - Math.PI / 2;
      const wob = Math.sin(i * 0.7 + t * 2.4) * 0.5 + 0.5;
      const len = base * 0.1 + wob * shown * base * 0.5;
      const r0 = base * 1.06;
      oc.beginPath();
      oc.moveTo(cx + Math.cos(a) * r0, cy + Math.sin(a) * r0);
      oc.lineTo(cx + Math.cos(a) * (r0 + len), cy + Math.sin(a) * (r0 + len));
      oc.strokeStyle = "rgba(53,232,255," + (0.16 + wob * 0.5) + ")";
      oc.lineWidth = Math.max(1.5, u); oc.lineCap = "round"; oc.stroke();
    }

    const core = oc.createRadialGradient(cx - base * 0.3, cy - base * 0.35, base * 0.05, cx, cy, base);
    core.addColorStop(0, "rgba(238,243,255,.95)");
    core.addColorStop(0.35, "rgba(53,232,255,.75)");
    core.addColorStop(0.75, "rgba(124,92,255,.45)");
    core.addColorStop(1, "rgba(124,92,255,0)");
    oc.fillStyle = core;
    oc.beginPath(); oc.arc(cx, cy, base, 0, Math.PI * 2); oc.fill();

    const band = base * 0.15;
    const sy = cy - base * 1.9 + ((t * base * 0.45) % (base * 3.8));
    const sg = oc.createLinearGradient(0, sy - band, 0, sy + band);
    sg.addColorStop(0, "rgba(53,232,255,0)");
    sg.addColorStop(0.5, "rgba(53,232,255,.16)");
    sg.addColorStop(1, "rgba(53,232,255,0)");
    oc.fillStyle = sg; oc.fillRect(cx - base * 2.4, sy - band, base * 4.8, band * 2);
  }

  /* The microphone control, drawn to the brief: a thick glowing ring around a
     dark core, concentric arcs outside it, and a waveform running left and
     right — which is why the canvas is wide rather than square. Every bar
     height is the live level, so the wings are a meter, not decoration. */
  function drawMic() {
    const W = micC.width, H = micC.height, cx = W / 2, cy = H / 2;
    const R = Math.min(H * 0.34, W * 0.13);       // core ring radius
    const ringW = R * 0.30;                        // ... and how thick it is
    mc.clearRect(0, 0, W, H);

    // ambient bloom, so the ring sits in light rather than on black
    const bloom = mc.createRadialGradient(cx, cy, R * 0.2, cx, cy, R * 3.2);
    bloom.addColorStop(0, "rgba(124,92,255,.30)");
    bloom.addColorStop(.45, "rgba(53,232,255,.10)");
    bloom.addColorStop(1, "rgba(5,6,15,0)");
    mc.fillStyle = bloom; mc.fillRect(0, 0, W, H);

    // the waveform wings — mirrored, tallest nearest the ring, fading out
    const gap = R * 1.75, barW = Math.max(2, R * 0.055), step = barW * 2.4;
    const room = cx - gap - barW;
    const bars = Math.max(4, Math.floor(room / step));
    for (let i = 0; i < bars; i++) {
      const d = i / bars;                          // 0 at the ring, 1 at the edge
      const fade = Math.pow(1 - d, 1.5);
      const wob = Math.abs(Math.sin(i * 0.85 - t * 5.2)) * 0.65
                + Math.abs(Math.sin(i * 0.31 + t * 2.7)) * 0.35;
      const h = Math.max(barW, (R * 0.16 + wob * shown * R * 1.25) * fade);
      const x = gap + i * step;
      mc.fillStyle = "rgba(53,232,255," + (0.14 + fade * 0.55).toFixed(3) + ")";
      mc.beginPath(); mc.roundRect(cx + x, cy - h / 2, barW, h, barW / 2); mc.fill();
      mc.beginPath(); mc.roundRect(cx - x - barW, cy - h / 2, barW, h, barW / 2); mc.fill();
    }

    // thin arcs outside the ring, turning slowly
    ring(mc, cx, cy, R * 1.42, t * 0.3, t * 0.3 + 2.4, Math.max(1, R * 0.018), .30);
    ring(mc, cx, cy, R * 1.42, t * 0.3 + Math.PI, t * 0.3 + Math.PI + 1.6,
         Math.max(1, R * 0.018), .18);
    ring(mc, cx, cy, R * 1.24 + shown * R * 0.10, 0, Math.PI * 2,
         Math.max(1, R * 0.012), .22, [R * 0.08, R * 0.16]);

    // the ring itself: one sweep of the whole palette, thick enough to glow
    const g = mc.createLinearGradient(cx - R, cy - R, cx + R, cy + R);
    g.addColorStop(0, "#35e8ff");
    g.addColorStop(.42, "#7c5cff");
    g.addColorStop(1, "#ff4fd8");
    mc.save();
    mc.shadowColor = "rgba(124,92,255,.85)";
    mc.shadowBlur = R * 0.55;
    mc.strokeStyle = g; mc.lineWidth = ringW;
    mc.beginPath(); mc.arc(cx, cy, R, 0, Math.PI * 2); mc.stroke();
    mc.restore();

    // a dark core, so the glyph reads against the glow rather than through it
    const core = mc.createRadialGradient(cx, cy - R * 0.3, R * 0.1, cx, cy, R * 0.9);
    core.addColorStop(0, "rgba(20,26,54,.96)");
    core.addColorStop(1, "rgba(6,8,20,.98)");
    mc.fillStyle = core;
    mc.beginPath(); mc.arc(cx, cy, R - ringW * 0.5, 0, Math.PI * 2); mc.fill();

    // microphone
    const s = R / 100;
    mc.strokeStyle = "#eef3ff"; mc.lineCap = "round"; mc.lineJoin = "round";
    mc.lineWidth = 9 * s;
    mc.beginPath(); mc.roundRect(cx - 15 * s, cy - 42 * s, 30 * s, 52 * s, 15 * s); mc.stroke();
    mc.lineWidth = 8 * s;
    mc.beginPath(); mc.arc(cx, cy - 2 * s, 30 * s, 0.16 * Math.PI, 0.84 * Math.PI); mc.stroke();
    mc.beginPath(); mc.moveTo(cx, cy + 28 * s); mc.lineTo(cx, cy + 44 * s); mc.stroke();
  }

  function frame() {
    t += 0.016;
    // The orb follows the microphone, not a timer: what you see is what the
    // detector is hearing, which is the question everyone asks mid-conversation.
    const target = S.handsFree || S.capturing ? S.level : (botSpeaking() ? 0.45 : 0.12);
    shown += (target - shown) * 0.18;
    drawOrb(); drawMic();
    requestAnimationFrame(frame);
  }

  /* The system view shows the detector's own numbers, so tuning it is a matter
     of reading rather than guessing. Only painted while it is on screen. */
  function refreshSystem() {
    const view = document.querySelector('[data-view="system"]');
    if (!view || view.hidden) return;
    const st = vad.state();
    const set = (id, v) => { const e = $(id); if (e) e.textContent = v; };
    set("sysLink", S.wsReady ? "เชื่อมต่ออยู่" : "หลุด");
    set("sysWarm", $("warmText") ? $("warmText").textContent : "—");
    set("sysRate", (S.ttsRate / 1000) + " kHz (เล่นกลับ) · 16 kHz (ไมค์)");
    const age = S.lastFrameAt ? Math.round(performance.now() - S.lastFrameAt) : null;
    set("sysMic", !S.mediaStream ? "ยังไม่ได้เปิด"
      : "เปิดอยู่ · AGC ปิด · เฟรมล่าสุด " + (age === null ? "—" : age + " ms"));
    set("sysCtx", S.audioCtx ? S.audioCtx.state : "ยังไม่สร้าง");
    set("sysTurns", String(S.turns));
    set("sysLevel", st.level.toFixed(4));
    set("sysGate", st.gate ? st.gate.toFixed(4) : "—");
    set("sysFloor", st.noiseFloor == null ? "ยังไม่ได้เรียนรู้" : st.noiseFloor.toFixed(4));
    set("sysSilence", st.silenceMs + " ms / " + vad.cfg.endMs + " ms");
  }

  /* ───────────────────────────── boot ───────────────────────────── */

  function wire() {
    $("coreBtn").onclick = toggleHandsFree;
    $("micring").parentElement.onclick = () => { if (!S.handsFree) pushToTalk(); };
    $("stopBtn").onclick = stopRun;
    $("panelClose").onclick = dismissPanel;
    document.querySelectorAll(".nav button[data-goto]").forEach((b) => {
      b.onclick = () => showView(b.dataset.goto);
    });
    document.querySelectorAll(".nav button[data-scroll]").forEach((b) => {
      b.onclick = () => {
        showView("deck");
        const el = $(b.dataset.scroll);
        if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
      };
    });
    document.querySelectorAll("[data-prompt]").forEach((b) => {
      b.onclick = () => sendText(b.dataset.prompt);
    });
    document.querySelectorAll('[data-shortcut="camera"]').forEach((b) => {
      b.onclick = () => openCamera();
    });
    const input = $("textInput");
    if (input) input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && input.value.trim()) {
        e.preventDefault(); sendText(input.value.trim()); input.value = "";
      }
    });
    addEventListener("keydown", (e) => {
      if (e.code === "Space" && e.target === document.body) { e.preventDefault(); pushToTalk(); }
      if (e.key === "Escape" && !$("stagePanel").hidden) { dismissPanel(); return; }
      if (e.key === "Escape" && !$("stopBtn").hidden) stopRun();
    });
    const clock = $("clock");
    const tick = () => {
      const d = new Date();
      clock.textContent = String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
    };
    tick(); setInterval(tick, 20000);
  }

  async function boot() {
    wire();
    setState("standby", "กดปุ่ม AI เพื่อเริ่มคุยต่อเนื่อง");
    // Loadout first: it carries the TTS rate, and the AudioContext has to be
    // created at that rate or every reply plays at the wrong speed.
    await refreshLoadout();
    connect();
    refreshMachines(); refreshUsage(); refreshWarm();
    setInterval(refreshMachines, 10000);
    setInterval(refreshUsage, 60000);
    setInterval(refreshWarm, 30000);
    setInterval(refreshLoadout, 60000);
    setInterval(refreshSystem, 500);
    setInterval(micWatchdog, 2000);
    setInterval(socketWatchdog, 5000);
    // A hidden tab is the commonest way the context suspends; resume the
    // moment it comes back rather than waiting for the watchdog to notice.
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden && S.audioCtx && S.audioCtx.state === "suspended") {
        S.audioCtx.resume().catch(() => {});
      }
    });
    if (reduce) { drawOrb(); drawMic(); } else requestAnimationFrame(frame);
  }

  /* A handle for diagnosing the detector from the console, because the last
     two bugs in here were both invisible from the outside. Read-only in
     spirit: nothing in the app reads it back. */
  window.JARVIS = {
    S, vad,
    frame: (lvl) => vad.frame(lvl),
    state: () => Object.assign({}, vad.state(), {
      capturing: S.capturing, handsFree: S.handsFree, agentBusy: S.agentBusy,
      wsReady: S.wsReady, preroll: S.preroll.length, uiState: S.uiState,
    }),
  };

  if (document.readyState === "loading") addEventListener("DOMContentLoaded", boot);
  else boot();
})();
