"use strict";
// "?v=..." of this script, reused for files it loads itself so they are never stale either.
const VERSION = document.currentScript ? new URL(document.currentScript.src).search : "";
const $ = (id) => document.getElementById(id);
const thread = $("thread"), input = $("input"), activity = $("activity");

const state = {
  conversation: null,   // id of the current conversation
  busy: false,
  images: [],           // {blob, url} waiting to be sent
  stt: "browser",
  pendingCard: null,    // the confirm card waiting for a tap, if any
  liveBubble: null,     // assistant bubble being streamed
  turnText: "",         // assistant text of the current turn, for reading aloud
  voiceTurn: false,     // the current turn was started by voice
  speak: localStorage.getItem("speak") === "1",
  atBottom: true,       // the thread is scrolled to its end, so new text should keep it there
  history: [],
};
const sleep = (ms) => new Promise((done) => setTimeout(done, ms));
const buzz = (ms = 8) => { try { navigator.vibrate?.(ms); } catch { /* not supported */ } };

// ---------------------------------------------------------------- utilities

const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
};

function toast(message, ms = 3200) {
  const t = $("toast");
  t.textContent = message;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (t.hidden = true), ms);
}

// Follow new content only while the person is at the end; if they scrolled up to read, stay put.
function scrollDown(force) {
  if (force || state.atBottom) { thread.scrollTop = thread.scrollHeight; state.atBottom = true; }
  $("jump").hidden = state.atBottom;
}
thread.addEventListener("scroll", () => {
  state.atBottom = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 60;
  $("jump").hidden = state.atBottom;
}, { passive: true });
$("jump").addEventListener("click", () => { thread.scrollTo({ top: thread.scrollHeight, behavior: "smooth" }); });

function setActivity(label) {
  activity.hidden = !label;
  if (label) $("activity-label").textContent = label;
}

// Small, safe markdown: paragraphs, bullet/numbered lists, bold, italics, code.
function markdown(src) {
  const esc = (s) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const inline = (s) => esc(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
  let html = "", list = null, para = [];
  const flush = () => {
    if (para.length) html += `<p>${para.join("<br>")}</p>`;
    if (list) html += `</${list}>`;
    para = []; list = null;
  };
  for (const raw of src.split("\n")) {
    const line = raw.trimEnd();
    const bullet = line.match(/^\s*[-*•]\s+(.*)/), numbered = line.match(/^\s*\d+[.)]\s+(.*)/);
    if (bullet || numbered) {
      const kind = bullet ? "ul" : "ol";
      if (list !== kind) { flush(); html += `<${kind}>`; list = kind; }
      html += `<li>${inline((bullet || numbered)[1])}</li>`;
    } else if (!line.trim()) {
      flush();
    } else {
      if (list) flush();
      para.push(inline(line.replace(/^#+\s*/, "")));
    }
  }
  flush();
  return html;
}

// ----------------------------------------------------------------- rendering

function hideWelcome() { $("welcome").hidden = true; }

function showUser(item) {
  hideWelcome();
  const nodes = [];
  if (item.images && item.images.length) {
    const row = el("div", "photos");
    for (const src of item.images) {
      const img = el("img");
      img.src = src.startsWith("blob:") ? src : `/api/uploads/${src}`;
      img.alt = "Photo";
      row.append(img);
    }
    thread.append(row);
    nodes.push(row);
  }
  if (item.text) {
    const bubble = el("div", "msg user");
    if (item.voice) {
      const tag = el("span", "voice-tag");
      tag.innerHTML = '<svg viewBox="0 0 24 24" aria-label="Spoken"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21"/></svg>';
      bubble.append(tag);
    }
    bubble.append(document.createTextNode(item.text));
    thread.append(bubble);
    nodes.push(bubble);
  }
  scrollDown(true);
  return nodes;
}

function showAssistant(text) {
  hideWelcome();
  const bubble = el("div", "msg assistant");
  bubble.innerHTML = markdown(text);
  thread.append(bubble);
  return bubble;
}

// action: optional {label, run} button under the message.
function showError(message, action) {
  const box = el("div", "msg error", message);
  if (action) {
    const b = el("button", "btn link", action.label);
    b.type = "button";
    b.addEventListener("click", () => { box.remove(); action.run(); });
    box.append(b);
  }
  thread.append(box);
  scrollDown(true);
}

// Reloading signs back in if the session ran out, and picks up where the chat was.
const RELOAD = { label: "Reload page", run: () => { sessionStorage.setItem("draft", input.value); location.reload(); } };

const BADGE = { add: "+", new: "+", remove: "−", open: "○", edit: "✎", list: "✓", undo: "↺" };

function showCard(item) {
  hideWelcome();
  let card = document.querySelector(`.card[data-id="${item.id}"]`);
  if (!card) { card = el("div"); card.dataset.id = item.id; thread.append(card); }
  card.className = `card ${item.state}${item.auto ? " auto" : ""}${item.undone ? " undone" : ""}`;
  card.replaceChildren();
  const pending = item.state === "pending";
  const many = item.actions.length > 1;

  const head = el("div", "card-head");
  head.append(el("span", null, pending ? "Is this right?" : item.undone ? "Undone" : "Saved"));
  if (many) head.append(el("small", null, `${item.actions.length} changes`));
  card.append(head);

  const rows = el("ul", "rows");
  for (const act of item.actions) {
    const failed = act.ok === false;
    const row = el(pending && many ? "label" : "li", `row${act.skipped ? " skipped" : ""}${failed ? " failed" : ""}`);
    if (pending && many) {
      const box = el("input");
      box.type = "checkbox"; box.checked = true; box.value = act.id;
      box.addEventListener("change", () => updateCount(card));
      row.append(box);
    }
    row.append(el("span", `badge ${failed ? "failed" : act.kind}`, failed ? "!" : BADGE[act.kind] || "•"));
    const text = el("div", "row-text");
    const title = el("div", "row-title", act.title);
    if (act.kind === "new") title.append(el("span", "new", "new"));
    text.append(title);
    const detail = failed ? `Could not save: ${act.error}` : act.skipped ? "Left out" : act.detail;
    if (detail) text.append(el("div", "row-detail", detail));
    row.append(text);
    rows.append(row);
  }
  card.append(rows);

  if (pending) {
    const foot = el("div", "card-foot");
    const cancel = el("button", "btn ghost", "Cancel");
    const ok = el("button", "btn primary", "Yes, save");
    cancel.type = ok.type = "button";
    cancel.addEventListener("click", () => confirmCard(card, []));
    ok.addEventListener("click", () => confirmCard(card, checkedIds(card, item)));
    foot.append(cancel, ok);
    card.append(foot);
    card.allIds = item.actions.map((a) => a.id);
    state.pendingCard = card;
  } else {
    if (state.pendingCard === card) state.pendingCard = null;
    if (item.state === "cancelled") card.append(el("div", "card-note", "Cancelled. Nothing was changed."));
    const undoable = item.state === "done" && !item.undone && item.actions.some((a) => a.ok && a.transaction_id);
    if (undoable) {
      const foot = el("div", "card-foot slim");
      const undo = el("button", "btn link", "Undo");
      undo.type = "button";
      undo.addEventListener("click", () => undoCard(item.id, undo));
      foot.append(undo);
      card.append(foot);
    }
  }
  scrollDown(pending);
}

function checkedIds(card, item) {
  const boxes = card.querySelectorAll("input[type=checkbox]");
  return boxes.length ? [...boxes].filter((b) => b.checked).map((b) => b.value) : item.actions.map((a) => a.id);
}

function updateCount(card) {
  const n = card.querySelectorAll("input:checked").length;
  const ok = card.querySelector(".btn.primary");
  ok.disabled = n === 0;
  ok.textContent = n === card.allIds.length ? "Yes, save" : `Save ${n} of ${card.allIds.length}`;
}

function showAsk(item) {
  hideWelcome();
  let box = document.querySelector(`.ask[data-id="${item.id}"]`);
  if (!box) {
    if (item.question) showAssistant(item.question);
    box = el("div", "ask"); box.dataset.id = item.id; thread.append(box);
  }
  box.replaceChildren();
  if (item.answered) { box.remove(); return; }
  for (const option of item.options) {
    const b = el("button", null, option);
    b.type = "button";
    b.addEventListener("click", () => send(option));
    box.append(b);
  }
  scrollDown(true);
}

function showItem(item) {
  if (item.t === "user") showUser(item);
  else if (item.t === "assistant") showAssistant(item.text);
  else if (item.t === "card") showCard(item);
  else if (item.t === "ask") showAsk(item);
}

// ------------------------------------------------------------------ network

async function readEvents(response) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut;
    while ((cut = buffer.indexOf("\n\n")) >= 0) {
      const chunk = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      for (const line of chunk.split("\n")) if (line.startsWith("data: ")) onEvent(JSON.parse(line.slice(6)));
    }
  }
}

function onEvent(e) {
  if (e.type === "start") {
    state.conversation = e.conversation_id;
  } else if (e.type === "text") {
    setActivity(null);
    if (!state.liveBubble) { state.liveBubble = showAssistant(""); state.liveBubble.raw = ""; }
    state.liveBubble.raw += e.text;
    state.turnText += e.text;
    state.liveBubble.innerHTML = markdown(state.liveBubble.raw);
    scrollDown();
  } else if (e.type === "text_end") {
    if (state.liveBubble && !state.liveBubble.raw.trim()) state.liveBubble.remove();
    state.liveBubble = null;
    state.turnText += "\n";
    setActivity("Thinking");
  } else if (e.type === "tool") {
    setActivity(e.label);
  } else if (e.type === "item") {
    if (e.item.t === "ask" && e.item.question) state.turnText += e.item.question;
    showItem(e.item);
  } else if (e.type === "error") {
    showError(e.message);
  } else if (e.type === "end") {
    state.ended = true;
  }
}

function renderConversation(chat) {
  clearThread();
  state.conversation = chat.id;
  $("welcome").hidden = chat.items.length > 0;
  chat.items.forEach(showItem);
  scrollDown(true);
}

// The server finishes a turn even if the phone drops off (locked screen, tunnel, lift).
// This waits for it and then shows the conversation as the server has it.
async function recover() {
  if (!state.conversation) { showError("I lost the connection. Please try again.", navigator.onLine ? RELOAD : null); return; }
  setActivity("Catching up");
  try {
    for (let i = 0; i < 200; i++) {
      const r = await fetch(`/api/conversations/${state.conversation}`);
      if (!r.ok) throw new Error();
      const chat = await r.json();
      if (!chat.busy) { renderConversation(chat); return; }
      setActivity("Still working");
      await sleep(1500);
    }
  } catch {
    showError("I lost the connection. Your last message may still have gone through.",
      navigator.onLine ? RELOAD : { label: "Check again", run: catchUp });
  }
}

async function catchUp() {
  if (state.busy) return;
  state.busy = true;
  refreshComposer();
  await recover();
  state.busy = false;
  setActivity(null);
  refreshComposer();
}

// onFail: called when the request never reached the server, to put things back.
async function run(request, onFail) {
  state.busy = true;
  state.turnText = "";
  state.ended = false;
  setActivity("Thinking");
  refreshComposer();
  document.querySelectorAll(".ask").forEach((n) => n.remove());
  let reached = false;
  try {
    const response = await request();
    reached = true;
    if (response.ok) {
      await readEvents(response);
      if (!state.ended) await recover();
    } else {
      const body = await response.json().catch(() => ({}));
      if (response.status === 409 || !onFail) await recover(); else onFail();
      if (response.status !== 409) showError(body.detail || "Something went wrong. Please try again.");
    }
  } catch (err) {
    if (!reached && onFail) {
      onFail();
      showError("That didn't send, so I put it back in the box. Please try again.", navigator.onLine ? RELOAD : null);
    } else {
      await recover();
    }
  } finally {
    state.busy = false;
    state.liveBubble = null;
    setActivity(null);
    refreshComposer();
    refreshHistory();
    afterTurn();
  }
}

function send(text, opts = {}) {
  text = (text || "").trim();
  const images = state.images;
  if (state.busy || (!text && !images.length)) return;
  state.images = [];
  renderPreviews();
  input.value = "";
  autosize();
  if (state.pendingCard) { state.pendingCard.className = "card cancelled"; state.pendingCard.querySelector(".card-foot")?.remove(); state.pendingCard = null; }
  state.voiceTurn = !!opts.voice;
  const nodes = showUser({ text, images: images.map((i) => i.url), voice: opts.voice });
  const form = new FormData();
  form.append("text", text);
  form.append("voice", opts.voice ? "true" : "false");
  form.append("conversation_id", state.conversation || "");
  images.forEach((img, i) => form.append("images", img.blob, `photo${i}.jpg`));
  run(() => fetch("/api/chat", { method: "POST", body: form }), () => {
    nodes.forEach((n) => n.remove());
    input.value = text;
    state.images = images;
    renderPreviews();
    autosize();
  });
}

function confirmCard(card, approved) {
  if (state.busy) return;
  buzz();
  card.querySelectorAll("button, input").forEach((n) => (n.disabled = true));
  state.pendingCard = null;
  run(() => fetch("/api/confirm", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ conversation_id: state.conversation, approved }),
  }));
}

async function undoCard(cardId, button) {
  button.disabled = true;
  button.textContent = "Undoing…";
  try {
    const r = await fetch("/api/undo", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ conversation_id: state.conversation, card_id: cardId }),
    });
    const body = await r.json();
    if (!r.ok) throw new Error(body.detail);
    showCard(body.card);
    toast(body.failed.length ? "Some changes could not be undone." : "Undone.");
  } catch (err) {
    button.disabled = false;
    button.textContent = "Undo";
    toast(err.message || "Could not undo that.");
  }
}

// ------------------------------------------------------------------- photos

// Receipts are tall. Scaling a long one into a single small image makes the print
// unreadable, so long photos are cut into overlapping slices instead.
async function prepareImage(file) {
  const bitmap = await createImageBitmap(file, { imageOrientation: "from-image" }).catch(() => null);
  if (!bitmap) throw new Error("unreadable");
  const scale = Math.min(1, 1300 / bitmap.width, bitmap.height > bitmap.width * 2 ? 1 : 1568 / bitmap.height);
  const width = Math.round(bitmap.width * scale), height = Math.round(bitmap.height * scale);
  const slice = 1500, overlap = 120, out = [];
  for (let top = 0; top < height && out.length < 5; top += slice - overlap) {
    const h = Math.min(slice, height - top);
    const canvas = document.createElement("canvas");
    canvas.width = width; canvas.height = h;
    canvas.getContext("2d").drawImage(bitmap, 0, top / scale, bitmap.width, h / scale, 0, 0, width, h);
    const blob = await new Promise((done) => canvas.toBlob(done, "image/jpeg", 0.85));
    out.push({ blob, url: URL.createObjectURL(blob) });
    if (top + h >= height) break;
  }
  return out;
}

async function addFiles(files) {
  for (const file of files) {
    try {
      for (const img of await prepareImage(file)) if (state.images.length < 8) state.images.push(img);
    } catch { toast("That photo could not be opened."); }
  }
  renderPreviews();
  refreshComposer();
  input.focus();
}

function renderPreviews() {
  const box = $("previews");
  box.replaceChildren();
  state.images.forEach((img, i) => {
    const wrap = el("div", "preview"), pic = el("img"), remove = el("button", null, "×");
    pic.src = img.url; pic.alt = "Photo to send";
    remove.type = "button"; remove.setAttribute("aria-label", "Remove photo");
    remove.addEventListener("click", () => { state.images.splice(i, 1); renderPreviews(); refreshComposer(); });
    wrap.append(pic, remove);
    box.append(wrap);
  });
}

// -------------------------------------------------------------------- voice
//
// Listening happens inside the message box: words appear as they are recognised, a small
// meter shows the microphone level, ✓ keeps the words in the box (ready to send) and ✕
// throws the recording away.
// The microphone is opened when the mic button is tapped and fully released (tracks
// stopped, audio context closed) the moment listening ends, whatever the reason.
//
// Three engines, picked by the server: "deepgram" streams audio and shows live words;
// "server" records and transcribes after ✓; "browser" uses the phone's own recognition.

const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
const canCapture = !!(navigator.mediaDevices && (window.AudioContext || window.webkitAudioContext));
const voice = { active: false, finishing: false };

function canTalk() {
  return state.stt === "browser" ? !!SpeechRecognition : canCapture;
}

function joinText(a, b) {
  return [a.trim(), b.trim()].filter(Boolean).join(" ");
}

function setListening(on, hint) {
  voice.active = on;
  const live = $("live");
  $("composer").classList.toggle("listening", on);
  $("composer").classList.remove("finishing");
  live.hidden = !on;
  input.hidden = on;
  live.dataset.hint = hint || "Listening…";
  $("wave").hidden = !on || voice.engine === "browser";
  $("rec-cancel").hidden = $("rec-done").hidden = !on;
  $("photo").hidden = on;
  if (on) showWords(); else live.replaceChildren();
  refreshComposer();
}

// Settled words in normal ink, words still being worked out in grey.
function showWords() {
  const live = $("live"), settled = joinText(voice.base, voice.finalText);
  live.replaceChildren();
  if (settled) live.append(el("span", null, settled + " "));
  if (voice.interim) live.append(el("span", "interim", voice.interim));
  live.scrollTop = live.scrollHeight;
}

// Tell the server when voice fails on a device, so it shows up in the logs.
function reportVoice(step, err) {
  const detail = `${step}: ${err && (err.name || "")} ${err && (err.message || err)}`.slice(0, 300);
  fetch("/api/log", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ message: detail }) }).catch(() => {});
}

const within = (promise, ms) => Promise.race([promise, new Promise((done) => setTimeout(done, ms))]);

async function startListening() {
  if (state.busy || voice.active) return;
  window.speechSynthesis?.cancel();
  Object.assign(voice, {
    engine: state.stt, finishing: false, ready: false, base: input.value, finalText: "", interim: "",
    heard: false, gotAudio: false, started: performance.now(), lastSound: performance.now(),
    levels: new Array(24).fill(0), chunks: [], level: 0, queue: [], ws: null,
  });
  if (voice.engine === "browser") return startBrowserRecognition();

  // iOS only lets audio start from inside the tap itself, so the audio context is created
  // and started here, before anything is awaited. Asking for the microphone comes after.
  try {
    voice.ctx = new (window.AudioContext || window.webkitAudioContext)();
    voice.ctx.resume().catch(() => {});
  } catch (err) {
    reportVoice("audio-context", err);
    toast("Talking isn't available in this browser.");
    return;
  }
  input.blur();
  setListening(true, "Starting…");
  const ctx = voice.ctx;
  const attempt = (voice.attempt = (voice.attempt || 0) + 1);
  const stale = () => !voice.active || voice.attempt !== attempt;
  // Loaded while the microphone is being opened, so listening starts sooner.
  const worklet = ctx.audioWorklet
    ? ctx.audioWorklet.addModule("/static/tap-worklet.js" + VERSION).then(() => true, (err) => { reportVoice("worklet-fallback", err); return false; })
    : Promise.resolve(false);

  try {
    voice.stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
  } catch (err) {
    if (stale()) return;
    reportVoice("microphone", err);
    cancelListening();
    toast(err && err.name === "NotAllowedError" ? "Please allow the microphone so I can hear you." : "I couldn't open the microphone.");
    return;
  }
  if (stale()) { voice.stream.getTracks().forEach((t) => t.stop()); return; }

  // The connection opens while audio is being set up. Anything said before it is ready
  // waits in voice.queue, so the first words are not lost.
  if (voice.engine === "deepgram") openSocket();

  try {
    await within(ctx.resume(), 1500);
    const useWorklet = await worklet;
    if (stale()) return;
    voice.source = ctx.createMediaStreamSource(voice.stream);
    const mute = ctx.createGain();
    mute.gain.value = 0;
    if (useWorklet) {
      voice.node = new AudioWorkletNode(ctx, "tap");
      voice.node.port.onmessage = (e) => onAudio(e.data);
    } else {
      // Older browsers: the deprecated but universal way of reading microphone audio.
      voice.node = ctx.createScriptProcessor(2048, 1, 1);
      voice.node.onaudioprocess = (e) => onAudio(new Float32Array(e.inputBuffer.getChannelData(0)));
    }
    voice.source.connect(voice.node);
    voice.node.connect(mute);
    mute.connect(ctx.destination);
  } catch (err) {
    if (stale()) return;
    reportVoice("audio-setup", err);
    cancelListening();
    toast("Talking isn't working on this device right now.");
    return;
  }

  if (voice.engine !== "deepgram") {
    const type = ["audio/webm;codecs=opus", "audio/mp4", "audio/webm"].find((t) => MediaRecorder.isTypeSupported(t)) || "";
    voice.recorder = new MediaRecorder(voice.stream, type ? { mimeType: type } : undefined);
    voice.recorder.ondataavailable = (e) => e.data.size && voice.chunks.push(e.data);
    voice.recorder.start();
  }
  voice.ready = true;
  voice.started = voice.lastSound = performance.now();
  $("live").dataset.hint = "Listening…";
  buzz();
  drawMeter();
}

function openSocket() {
  const ws = (voice.ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/listen`));
  ws.binaryType = "arraybuffer";
  ws.onopen = () => {
    for (const chunk of voice.queue) ws.send(chunk);
    voice.queue = [];
    voice.onWsOpen?.();
  };
  ws.onmessage = (e) => {
    if (voice.ws !== ws) return;
    const data = JSON.parse(e.data);
    if (data.error) { cancelListening(); toast(data.error); return; }
    if (data.final) { voice.finalText = joinText(voice.finalText, data.text); voice.interim = ""; }
    else voice.interim = data.text;
    voice.heard = true;
    if (voice.active) showWords();
  };
  ws.onerror = () => reportVoice("websocket", "connection error");
  ws.onclose = () => {
    if (voice.ws !== ws) return;
    voice.onWsOpen?.();
    voice.onWsClose?.();
    // Dropped while still listening: keep what was heard instead of pretending to listen.
    if (voice.active && !voice.finishing) finishListening(false);
  };
}

// Called with ~40 ms of microphone audio at a time.
function onAudio(samples) {
  voice.gotAudio = true;
  let sum = 0;
  for (const v of samples) sum += v * v;
  const rms = Math.sqrt(sum / samples.length);
  const now = performance.now();
  if (rms > 0.02) { voice.lastSound = now; if (voice.engine !== "deepgram" && now - voice.started > 250) voice.heard = true; }
  voice.level = Math.max(rms, (voice.level || 0) * 0.6);
  const ws = voice.ws;
  if (!ws || ws.readyState > WebSocket.OPEN || voice.finishing) return;
  // Deepgram gets 16 kHz 16-bit mono, whatever rate the phone records at.
  const ratio = voice.ctx.sampleRate / 16000;
  const out = new Int16Array(Math.floor(samples.length / ratio));
  for (let i = 0; i < out.length; i++) {
    const from = Math.floor(i * ratio), to = Math.max(from + 1, Math.floor((i + 1) * ratio));
    let total = 0;
    for (let j = from; j < to; j++) total += samples[j];
    out[i] = Math.max(-1, Math.min(1, total / (to - from))) * 0x7fff;
  }
  if (ws.readyState === WebSocket.OPEN) ws.send(out.buffer);
  else if (voice.queue.length < 400) voice.queue.push(out.buffer);  // still connecting
}

function drawMeter() {
  const canvas = $("wave"), g = canvas.getContext("2d");
  const color = getComputedStyle(document.documentElement).getPropertyValue("--muted");
  let lastPush = 0;
  const tick = (now) => {
    if (!voice.active || voice.finishing) return;
    if (now - lastPush > 70) { voice.levels.push(Math.min(1, (voice.level || 0) * 9)); voice.levels.shift(); lastPush = now; }
    g.clearRect(0, 0, canvas.width, canvas.height);
    g.fillStyle = color;
    const step = canvas.width / voice.levels.length;
    voice.levels.forEach((level, i) => {
      const h = Math.max(4, level * canvas.height), w = 4;
      g.beginPath();
      if (g.roundRect) g.roundRect(i * step + (step - w) / 2, (canvas.height - h) / 2, w, h, 2);
      else g.rect(i * step + (step - w) / 2, (canvas.height - h) / 2, w, h);
      g.fill();
    });
    const idle = performance.now() - voice.lastSound;
    // Never leave the microphone open on a silent room.
    if (!voice.gotAudio && performance.now() - voice.started > 4000) { reportVoice("no-audio", voice.ctx ? voice.ctx.state : "no context"); cancelListening(); toast("The microphone isn't sending sound. Please try again."); return; }
    if (!voice.heard && performance.now() - voice.started > 12000) { cancelListening(); toast("I didn't hear anything."); return; }
    if (voice.heard && idle > 7000) { finishListening(false); return; }
    voice.raf = requestAnimationFrame(tick);
  };
  voice.raf = requestAnimationFrame(tick);
}

function releaseMic() {
  cancelAnimationFrame(voice.raf);
  voice.stream?.getTracks().forEach((t) => t.stop());
  try { if (voice.node) voice.node.onaudioprocess = null; voice.node?.disconnect(); voice.source?.disconnect(); } catch { /* already gone */ }
  voice.ctx?.close().catch(() => {});
  voice.stream = voice.node = voice.source = voice.ctx = null;
}

function cancelListening() {
  if (!voice.active) return;
  voice.finishing = true;
  if (voice.recognition) { voice.cancelled = true; voice.recognition.abort(); }
  if (voice.recorder && voice.recorder.state !== "inactive") { voice.recorder.onstop = null; voice.recorder.stop(); }
  voice.recorder = null;
  const ws = voice.ws;
  voice.ws = null;
  ws?.close();
  releaseMic();
  setListening(false);
}

// ✓ and a long silence both end here with send=false: the words stay in the box to be
// checked, fixed if needed, and sent with the send button.
async function finishListening(send) {
  if (!voice.active || voice.finishing) return;
  if (!voice.ready && !voice.recognition) return cancelListening();
  voice.finishing = true;
  $("composer").classList.add("finishing");
  if (send) buzz();
  if (voice.recognition) { voice.sendAfter = send; voice.recognition.stop(); return; }
  let text = "";
  if (voice.engine === "deepgram") {
    releaseMic();
    const ws = voice.ws;
    if (ws && ws.readyState === WebSocket.CONNECTING) await new Promise((done) => { voice.onWsOpen = done; setTimeout(done, 2000); });
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send("stop");  // the last words arrive, then the server closes the socket
      await new Promise((done) => { voice.onWsClose = done; setTimeout(done, 2000); });
    }
    voice.ws = null; voice.onWsClose = voice.onWsOpen = null;
    ws?.close();
    text = joinText(voice.finalText, voice.interim);
  } else {
    const recorder = voice.recorder;
    const blob = await new Promise((done) => {
      recorder.onstop = () => done(new Blob(voice.chunks, { type: recorder.mimeType || "audio/webm" }));
      recorder.stop();
    });
    voice.recorder = null;
    releaseMic();
    if (voice.heard) {
      $("live").replaceChildren();
      $("live").dataset.hint = "Writing down what you said…";
      try {
        const form = new FormData();
        form.append("audio", blob, blob.type.includes("mp4") ? "speech.m4a" : "speech.webm");
        const r = await fetch("/api/transcribe", { method: "POST", body: form });
        const body = await r.json();
        if (!r.ok) throw new Error(body.detail);
        text = body.text;
      } catch (err) {
        toast(err.message || "I couldn't understand that. Please try again.");
      }
    }
  }
  deliver(text, send);
}

function deliver(text, send) {
  const spokenOnly = !voice.base.trim();
  input.value = joinText(voice.base, text);
  autosize();
  setListening(false);
  if (!text.trim()) { toast("I didn't catch that."); return; }
  state.dictated = true;  // sending this counts as a spoken message
  if (!send) { if (!phone.matches) input.focus(); return; }
  if (spokenOnly) heard(text); else send_(input.value);
}
const send_ = (text) => send(text, { voice: true });

function startBrowserRecognition() {
  const rec = (voice.recognition = new SpeechRecognition());
  rec.lang = navigator.language || "en-US";
  rec.interimResults = true;
  rec.continuous = true;
  voice.cancelled = false; voice.sendAfter = false;
  rec.onresult = (e) => {
    voice.finalText = [...e.results].map((r) => r[0].transcript).join(" ");
    if (voice.active && !voice.cancelled) showWords();
  };
  rec.onerror = (e) => { if (e.error === "not-allowed" || e.error === "service-not-allowed") toast("Please allow the microphone so I can hear you."); };
  rec.onend = () => {
    voice.recognition = null;
    if (voice.cancelled) return;
    deliver(voice.finalText, voice.sendAfter);
  };
  setListening(true);
  try { rec.start(); } catch { voice.recognition = null; setListening(false); toast("Talking isn't available right now."); }
}

const YES = /^(yes|yeah|yep|yup|ok|okay|sure|correct|right|confirm|save|do it|go ahead|looks good|haan|han|ha|theek hai|thik hai|हाँ|हां|ठीक है)(\s|[.!,]|$)/i;
const NO = /^(no|nope|nah|cancel|stop|wrong|don't|do not|nahi|nahin|नहीं)(\s|[.!,]|$)/i;

// A spoken message with nothing typed around it. A short yes/no answers a waiting card.
function heard(text) {
  if (!text || state.busy) return;
  const short = text.split(/\s+/).length <= 4, card = state.pendingCard;
  if (card && short && (YES.test(text) || NO.test(text))) {
    input.value = ""; autosize(); refreshComposer();
    showUser({ text, voice: true });
    state.voiceTurn = true;
    return confirmCard(card, YES.test(text) ? card.allIds : []);
  }
  send(text, { voice: true });
}

// After a spoken turn with "read replies aloud" on, say the reply. The microphone stays off.
function afterTurn() {
  if (!state.voiceTurn) return;
  state.voiceTurn = false;
  const text = state.turnText.replace(/[*_`#]/g, "").replace(/\s+/g, " ").trim();
  if (!state.speak || !window.speechSynthesis || !text) return;
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.lang = navigator.language || "en-US";
  speechSynthesis.speak(utterance);
}

// Leaving the page or locking the phone always ends listening and frees the microphone.
document.addEventListener("visibilitychange", () => { if (document.hidden) cancelListening(); });
window.addEventListener("pagehide", cancelListening);

// ----------------------------------------------------------------- composer

function autosize() {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 160) + "px";
}

function refreshComposer() {
  const hasContent = input.value.trim() || state.images.length;
  const talk = canTalk();
  $("send").hidden = voice.active || (!hasContent && talk);
  $("mic").hidden = voice.active || !!hasContent || !talk;
  $("send").disabled = $("mic").disabled = state.busy;
}

input.addEventListener("input", () => { autosize(); refreshComposer(); if (!input.value.trim()) state.dictated = false; });
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing && matchMedia("(pointer: fine)").matches) { e.preventDefault(); submit(); }
});
// Dictated text goes out as a spoken message (a short "yes"/"no" answers a waiting card).
function submit() {
  if (voice.active || state.busy) return;
  const spoken = state.dictated && !state.images.length;
  state.dictated = false;
  if (spoken) heard(input.value.trim()); else send(input.value);
}
$("composer").addEventListener("submit", (e) => { e.preventDefault(); submit(); });
$("photo").addEventListener("click", () => $("file").click());
$("file").addEventListener("change", (e) => { addFiles([...e.target.files]); e.target.value = ""; });
input.addEventListener("paste", (e) => {
  const files = [...(e.clipboardData?.files || [])].filter((f) => f.type.startsWith("image/"));
  if (files.length) { e.preventDefault(); addFiles(files); }
});
document.addEventListener("keydown", (e) => {
  if (voice.active && e.key === "Enter") { e.preventDefault(); finishListening(false); }
  else if (e.key === "Escape") { if (voice.active) cancelListening(); else if (!$("lightbox").hidden) $("lightbox").hidden = true; else setMenu(false); }
});

// Tap a photo to see it large; tap again to close.
thread.addEventListener("click", (e) => {
  const img = e.target.closest(".photos img");
  if (!img) return;
  $("lightbox").firstElementChild.src = img.src;
  $("lightbox").hidden = false;
});
$("lightbox").addEventListener("click", () => ($("lightbox").hidden = true));

// iOS slides the whole page up when the keyboard opens instead of resizing it. Sizing the
// page to the visible area keeps the message box sitting right on top of the keyboard.
if (window.visualViewport) {
  const fit = () => {
    if (visualViewport.scale > 1.01) return;  // pinch-zoomed: leave the layout alone
    document.documentElement.style.setProperty("--app-h", visualViewport.height + "px");
    window.scrollTo(0, 0);
    scrollDown();
  };
  visualViewport.addEventListener("resize", fit);
  visualViewport.addEventListener("scroll", fit);
  fit();
}
$("mic").addEventListener("click", startListening);
$("rec-done").addEventListener("click", () => { buzz(); finishListening(false); });
$("rec-cancel").addEventListener("click", cancelListening);
$("speak-toggle").addEventListener("click", () => {
  state.speak = !state.speak;
  localStorage.setItem("speak", state.speak ? "1" : "0");
  $("speak-toggle").setAttribute("aria-checked", state.speak);
  if (!state.speak) window.speechSynthesis?.cancel();
  else if (window.speechSynthesis) speechSynthesis.speak(new SpeechSynthesisUtterance(""));  // iOS needs a tap to allow speech
  toast(state.speak ? "When you talk to me, I'll answer out loud." : "I'll stay quiet.");
});
// ------------------------------------------------------- menu and history

const phone = matchMedia("(max-width: 860px)"), sidebar = $("sidebar"), scrim = $("scrim");

function setMenu(open) {
  if (open) { input.blur(); refreshHistory(); }
  document.body.classList.toggle("menu-open", open);
  sidebar.inert = phone.matches && !open;  // off-screen: keep it out of reach of keyboard and screen reader
  if (!open) disarmSignOut();
}
phone.addEventListener("change", () => setMenu(false));
$("menu").addEventListener("click", () => setMenu(true));
$("avatar-top").addEventListener("click", () => setMenu(true));
$("menu-close").addEventListener("click", () => setMenu(false));
scrim.addEventListener("click", () => setMenu(false));

// Swipe right anywhere on the chat to pull the menu out; swipe left to push it back.
// The menu follows the finger and settles open or closed depending on where it is let go.
let drag = null;
document.addEventListener("touchstart", (e) => {
  drag = null;
  if (!phone.matches || e.touches.length !== 1 || voice.active || !$("lightbox").hidden || $("look").open) return;
  const open = document.body.classList.contains("menu-open");
  if (!open && e.target.closest("#composer, #previews")) return;
  const t = e.touches[0];
  drag = { x: t.clientX, y: t.clientY, open, width: sidebar.offsetWidth, moving: false, lastX: t.clientX, lastT: e.timeStamp, speed: 0 };
}, { passive: true });
document.addEventListener("touchmove", (e) => {
  if (!drag) return;
  const t = e.touches[0], dx = t.clientX - drag.x, dy = t.clientY - drag.y;
  if (!drag.moving) {
    if (Math.abs(dx) < 12 && Math.abs(dy) < 12) return;
    // Only a clearly sideways drag in the useful direction counts; anything else is scrolling.
    if (Math.abs(dx) < Math.abs(dy) * 1.5 || (drag.open ? dx > 0 : dx < 0)) { drag = null; return; }
    drag.moving = true;
    drag.x = t.clientX;
    document.body.classList.add("dragging");
    return;
  }
  if (e.cancelable) e.preventDefault();
  const offset = Math.max(-drag.width, Math.min(0, drag.open ? dx : dx - drag.width));
  drag.offset = offset;
  drag.speed = (t.clientX - drag.lastX) / Math.max(1, e.timeStamp - drag.lastT);
  drag.lastX = t.clientX; drag.lastT = e.timeStamp;
  sidebar.style.transform = `translateX(${offset}px)`;
  scrim.style.opacity = 1 + offset / drag.width;
}, { passive: false });
const endDrag = () => {
  if (!drag || !drag.moving) { drag = null; return; }
  const shown = 1 + (drag.offset ?? (drag.open ? 0 : -drag.width)) / drag.width;
  const open = Math.abs(drag.speed) > 0.35 ? drag.speed > 0 : shown > 0.5;
  drag = null;
  document.body.classList.remove("dragging");
  sidebar.style.transform = scrim.style.opacity = "";
  setMenu(open);
};
document.addEventListener("touchend", endDrag);
document.addEventListener("touchcancel", endDrag);

// Signing out takes two taps, so a stray touch doesn't send anyone back to the sign-in page.
let signOutTimer = 0;
function disarmSignOut() {
  clearTimeout(signOutTimer);
  $("sign-out").classList.remove("armed");
  $("sign-out").lastElementChild.textContent = "Sign out";
}
$("sign-out").addEventListener("click", () => {
  const b = $("sign-out");
  if (b.classList.contains("armed")) { location.href = b.dataset.url; return; }
  b.classList.add("armed");
  b.lastElementChild.textContent = "Tap again to sign out";
  signOutTimer = setTimeout(disarmSignOut, 4000);
});

function clearThread() {
  state.pendingCard = null;
  [...thread.children].forEach((n) => n.id !== "welcome" && n.remove());
}

function dayLabel(seconds) {
  const midnight = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const days = Math.round((midnight(new Date()) - midnight(new Date(seconds * 1000))) / 864e5);
  return days <= 0 ? "Today" : days === 1 ? "Yesterday" : days < 7 ? "Earlier this week" : "Older";
}

const DOTS = '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="5.5" cy="12" r="1.2"/><circle cx="12" cy="12" r="1.2"/><circle cx="18.5" cy="12" r="1.2"/></svg>';

function renderHistory(list) {
  const nav = $("history");
  nav.replaceChildren();
  if (!list.length) nav.append(el("div", "empty", "Your chats will show up here."));
  let group = "";
  for (const chat of list) {
    const label = dayLabel(chat.updated_at);
    if (label !== group) { nav.append(el("div", "side-label", label)); group = label; }
    const row = el("div", "chat-row" + (chat.id === state.conversation ? " active" : ""));
    const open = el("button", "chat-open", chat.title), more = el("button", "chat-more");
    open.type = more.type = "button";
    open.title = new Date(chat.updated_at * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
    open.addEventListener("click", () => openConversation(chat.id));
    more.innerHTML = DOTS;
    more.setAttribute("aria-label", `Rename or delete "${chat.title}"`);
    more.addEventListener("click", () => toggleChatActions(row, chat));
    row.append(open, more);
    nav.append(row);
  }
  state.history = list;
}

// The ⋯ button opens a small strip under the chat with Rename and Delete.
function toggleChatActions(row, chat) {
  const wasOpen = row.classList.contains("open");
  $("history").querySelectorAll(".chat-actions").forEach((n) => n.remove());
  $("history").querySelectorAll(".chat-row.open").forEach((n) => n.classList.remove("open"));
  if (wasOpen) return;
  row.classList.add("open");
  const strip = el("div", "chat-actions"), rename = el("button", null, "Rename"), remove = el("button", "danger", "Delete");
  rename.type = remove.type = "button";
  rename.addEventListener("click", () => { strip.remove(); row.classList.remove("open"); renameChat(row, chat); });
  remove.addEventListener("click", () => {
    // Two taps, so a chat is never lost to a stray touch.
    if (!remove.classList.contains("armed")) { remove.classList.add("armed"); remove.textContent = "Tap again to delete"; return; }
    deleteChat(chat);
  });
  strip.append(rename, remove);
  row.after(strip);
}

function renameChat(row, chat) {
  const field = el("input", "chat-rename");
  field.value = chat.title; field.maxLength = 80; field.setAttribute("aria-label", "Chat name");
  field.enterKeyHint = "done";
  row.querySelector(".chat-open").replaceWith(field);
  row.querySelector(".chat-more").hidden = true;
  field.focus(); field.select();
  let finished = false;
  const finish = async (save) => {
    if (finished) return;
    finished = true;
    const title = field.value.trim();
    if (save && title && title !== chat.title) {
      try {
        const r = await fetch(`/api/conversations/${chat.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title }) });
        if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail);
        chat.title = (await r.json()).title;
      } catch (err) { toast(err.message || "I couldn't rename that chat."); }
    }
    renderHistory(state.history);
  };
  field.addEventListener("keydown", (e) => {
    e.stopPropagation();  // Escape here cancels the rename, not the whole menu
    if (e.key === "Enter") finish(true); else if (e.key === "Escape") finish(false);
  });
  field.addEventListener("blur", () => finish(true));
}

async function deleteChat(chat) {
  try {
    const r = await fetch(`/api/conversations/${chat.id}`, { method: "DELETE" });
    if (!r.ok && r.status !== 404) throw new Error((await r.json().catch(() => ({}))).detail);
  } catch (err) { toast(err.message || "I couldn't delete that chat."); return; }
  state.history = state.history.filter((c) => c.id !== chat.id);
  if (chat.id === state.conversation && !state.busy) {
    state.conversation = null;
    clearThread();
    $("welcome").hidden = false;
  }
  renderHistory(state.history);
  toast("Chat deleted.");
}

// ---------------------------------------------------------------- appearance

const LOOK = {
  theme: [["system", "Auto"], ["light", "Light"], ["dark", "Dark"]],
  tone: [["warm", "Warm"], ["cool", "Cool"]],
  accent: [["green", "Green"], ["teal", "Teal"], ["blue", "Blue"], ["purple", "Purple"], ["rose", "Rose"], ["clay", "Clay"], ["amber", "Amber"]],
  size: [["normal", "Normal"], ["large", "Large"], ["xlarge", "Largest"]],
};
const look = { theme: "system", tone: "warm", accent: "green", size: "normal" };
try { Object.assign(look, JSON.parse(localStorage.getItem("look") || "{}")); } catch { /* keep the defaults */ }
const systemDark = matchMedia("(prefers-color-scheme: dark)");

function applyLook() {
  for (const key of Object.keys(LOOK)) if (!LOOK[key].some(([value]) => value === look[key])) look[key] = LOOK[key][0][0];
  const root = document.documentElement;
  root.dataset.theme = look.theme === "system" ? (systemDark.matches ? "dark" : "light") : look.theme;
  root.dataset.tone = look.tone; root.dataset.accent = look.accent; root.dataset.size = look.size;
  document.querySelector('meta[name="theme-color"]').content = getComputedStyle(root).getPropertyValue("--bg").trim();
  document.querySelectorAll("#look [data-pref] button").forEach((b) => b.setAttribute("aria-pressed", look[b.parentElement.dataset.pref] === b.value));
}
systemDark.addEventListener("change", applyLook);

document.querySelectorAll("#look [data-pref]").forEach((group) => {
  const key = group.dataset.pref;
  for (const [value, label] of LOOK[key]) {
    const b = el("button", null, key === "accent" ? "" : label);
    b.type = "button"; b.value = value;
    if (key === "accent") { b.dataset.accent = value; b.setAttribute("aria-label", label); b.title = label; }
    b.addEventListener("click", () => {
      look[key] = value;
      try { localStorage.setItem("look", JSON.stringify(look)); } catch { /* private mode: lasts until reload */ }
      applyLook();
    });
    group.append(b);
  }
});
applyLook();
$("look-open").addEventListener("click", () => { setMenu(false); $("look").showModal(); });
$("look-close").addEventListener("click", () => $("look").close());
$("look").addEventListener("click", (e) => { if (e.target === $("look")) $("look").close(); });  // tap outside

async function refreshHistory() {
  try {
    const r = await fetch("/api/conversations");
    if (r.ok) renderHistory(await r.json());
  } catch { /* the list is a convenience; keep the old one */ }
}

async function openConversation(id) {
  if (state.busy) return;
  setMenu(false);
  try {
    const r = await fetch(`/api/conversations/${id}`);
    if (!r.ok) throw new Error();
    const chat = await r.json();
    renderConversation(chat);
    renderHistory(state.history);
    if (chat.busy) catchUp();
  } catch {
    toast("I couldn't open that chat.");
  }
}

function newChat() {
  if (state.busy) return;
  setMenu(false);
  state.conversation = null;
  clearThread();
  $("welcome").hidden = false;
  scrollDown(true);
  renderHistory(state.history);
  if (!phone.matches) input.focus();
}
$("new-chat").addEventListener("click", newChat);
$("new-top").addEventListener("click", newChat);
document.querySelector(".quick").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  if (b.dataset.action === "photo") $("file").click();
  else if (b.dataset.say) send(b.dataset.say);
  else { input.value = b.dataset.fill; input.focus(); autosize(); refreshComposer(); }
});

async function boot() {
  $("speak-toggle").setAttribute("aria-checked", state.speak);
  try {
    const r = await fetch("/api/state");
    if (!r.ok) throw new Error();
    const data = await r.json();
    state.stt = data.stt;
    const hour = new Date().getHours();
    $("hello").textContent = `${hour < 12 ? "Good morning" : hour < 17 ? "Good afternoon" : "Good evening"}, ${data.user.name}`;
    const initial = (data.user.name || "?").charAt(0).toUpperCase();
    $("avatar").textContent = $("avatar-top").textContent = initial;
    $("avatar-top").title = data.user.email;
    $("me-name").textContent = data.user.name;
    $("me-email").textContent = data.user.email.endsWith("@local") ? "Not signed in" : data.user.email;
    if (data.grocy_url) { $("grocy-link").href = data.grocy_url; $("grocy-link").hidden = false; }
    if (data.logout_url) { $("sign-out").dataset.url = data.logout_url; $("sign-out").hidden = false; }
    if (data.conversation && data.conversation.items.length) renderConversation(data.conversation);
    renderHistory(data.history || []);
    if (data.conversation && data.conversation.busy) catchUp();
  } catch {
    showError("I couldn't load.", RELOAD);
  }
  const draft = sessionStorage.getItem("draft");
  if (draft) { input.value = draft; sessionStorage.removeItem("draft"); autosize(); }
  setMenu(false);
  refreshComposer();
  if (!phone.matches) input.focus();
}
boot();

// Coming back to the page: show what the server has now. After a long time away, start fresh.
let hiddenAt = 0;
document.addEventListener("visibilitychange", async () => {
  if (document.hidden) { hiddenAt = Date.now(); return; }
  const away = hiddenAt ? Date.now() - hiddenAt : 0;
  if (state.busy || away < 5 * 60e3) return;
  if (away > 6 * 3600e3 && !input.value.trim() && !state.images.length) { location.reload(); return; }
  refreshHistory();
  if (state.conversation) catchUp();
});
window.addEventListener("online", () => { if (state.conversation && thread.querySelector(".msg.error")) { thread.querySelectorAll(".msg.error").forEach((n) => n.remove()); catchUp(); } });
window.addEventListener("offline", () => toast("You're offline. I'll be here when the internet is back.", 5000));
