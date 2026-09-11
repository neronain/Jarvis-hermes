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

  const S = {
    ws: null, wsReady: false,
    ttsRate: 24000,          // corrected from /api/loadout before any audio plays
    audioCtx: null, playhead: 0, activeSources: [], leftoverByte: null,
    mediaStream: null, workletReady: false,
    capturing: false, handsFree: false,
    agentBusy: false, lastAudioAt: 0,
    level: 0, turns: 0, currentRun: null,
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
    return S.activeSources.length > 0 && S.audioCtx && S.playhead > S.audioCtx.currentTime + 0.05;
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
    for (let i = 0; i < 4; i++) {
      try {
        const r = await fetch("/api/ack?i=" + i);
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
    const src = ctx.createMediaStreamSource(S.mediaStream);
    const node = new AudioWorkletNode(ctx, "pcm16k");
    node.port.onmessage = (e) => {
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

  function setState(st, hint) {
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
    if (e.media === "image") {
      body.innerHTML = '<img alt="' + esc(e.title || "") + '" src="' + esc(src) + '">';
    } else if (e.media === "video") {
      body.innerHTML = '<video controls autoplay playsinline src="' + esc(src) + '"></video>';
    } else if (!src) {
      body.innerHTML = '<div class="msg-empty"><div><b>ไม่มีอะไรจะแสดง</b>' +
        'คำสั่งมาถึงแล้วแต่ไม่มี src</div></div>';
    } else {
      // allow-scripts is what the Maps Embed API needs; the frame gets no
      // access to this page, which is the point of listing them one at a time.
      body.innerHTML = '<iframe src="' + esc(src) + '" loading="lazy" ' +
        'referrerpolicy="no-referrer-when-downgrade" ' +
        'sandbox="allow-scripts allow-same-origin allow-popups" ' +
        'allow="fullscreen" title="' + esc(e.title || "panel") + '"></iframe>';
    }
    $("chatCard").hidden = true;
    $("stagePanel").hidden = false;
  }

  function dismissPanel() {
    const p = $("stagePanel");
    if (!p || p.hidden) return;
    p.hidden = true;
    $("panelBody").innerHTML = "";     // stop whatever was playing or polling
    $("chatCard").hidden = false;
  }

  /* Typed "แผนที่ <ที่ไหน>" is a shortcut past the agent, for when you just
     want to look at somewhere. */
  const MAP_PREFIX = /^\s*(?:แผนที่|map|ดาวเทียม|satellite|earth|โลก3มิติ|3d)\s+(.+)$/i;

  async function requestMap(q, mode) {
    try {
      const r = await fetch("/api/map", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ q, mode: mode || "satellite" }),
      });
      const j = await r.json();
      if (j.error) {
        $("panelTitle").textContent = q;
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
    S.ws.onclose = () => {
      S.wsReady = false; linkDot(false);
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
          if (!quiet) addMsg("sys", e.message);
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
    loadAcks();                        // not awaited: the first turn can go without
    S.handsFree = true;
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

  function drawMic() {
    const W = micC.width, H = micC.height, cx = W / 2, cy = H / 2;
    const R = Math.min(W, H) * 0.30;
    mc.clearRect(0, 0, W, H);
    for (let i = 0; i < 52; i++) {
      const a = (i / 52) * Math.PI * 2;
      const wob = Math.abs(Math.sin(i * 0.55 + t * 3.1));
      const len = R * 0.08 + wob * shown * R * 0.46;
      mc.beginPath();
      mc.moveTo(cx + Math.cos(a) * (R + R * 0.11), cy + Math.sin(a) * (R + R * 0.11));
      mc.lineTo(cx + Math.cos(a) * (R + R * 0.11 + len), cy + Math.sin(a) * (R + R * 0.11 + len));
      mc.strokeStyle = "rgba(124,92,255," + (0.12 + wob * 0.42) + ")";
      mc.lineWidth = Math.max(2, R * 0.02); mc.lineCap = "round"; mc.stroke();
    }
    const g = mc.createRadialGradient(cx - R * 0.27, cy - R * 0.3, R * 0.06, cx, cy, R);
    g.addColorStop(0, "rgba(53,232,255,.95)");
    g.addColorStop(0.45, "rgba(124,92,255,.9)");
    g.addColorStop(1, "rgba(255,79,216,.85)");
    mc.fillStyle = g;
    mc.beginPath(); mc.arc(cx, cy, R, 0, Math.PI * 2); mc.fill();
    mc.globalCompositeOperation = "destination-out";
    mc.beginPath(); mc.arc(cx, cy, R * 0.83, 0, Math.PI * 2); mc.fill();
    mc.globalCompositeOperation = "source-over";
    ring(mc, cx, cy, R * 1.03 + shown * R * 0.12, 0, Math.PI * 2, Math.max(1.5, R * 0.012), 0.3);

    mc.strokeStyle = "#eef3ff"; mc.lineWidth = R * 0.047; mc.lineCap = "round";
    mc.beginPath(); mc.roundRect(cx - R * 0.127, cy - R * 0.35, R * 0.253, R * 0.44, R * 0.127); mc.stroke();
    mc.beginPath(); mc.arc(cx, cy + R * 0.04, R * 0.253, 0.15 * Math.PI, 0.85 * Math.PI); mc.stroke();
    mc.beginPath(); mc.moveTo(cx, cy + R * 0.293); mc.lineTo(cx, cy + R * 0.413); mc.stroke();
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
    set("sysMic", S.mediaStream ? "เปิดอยู่ · AGC ปิด" : "ยังไม่ได้เปิด");
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
    if (reduce) { drawOrb(); drawMic(); } else requestAnimationFrame(frame);
  }

  if (document.readyState === "loading") addEventListener("DOMContentLoaded", boot);
  else boot();
})();
