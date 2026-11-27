"use strict";
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
};

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

function scrollDown(force) {
  const near = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 240;
  if (force || near) thread.scrollTop = thread.scrollHeight;
}

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
  if (item.images && item.images.length) {
    const row = el("div", "photos");
    for (const src of item.images) {
      const img = el("img");
      img.src = src.startsWith("blob:") ? src : `/api/uploads/${src}`;
      img.alt = "Photo";
      row.append(img);
    }
    thread.append(row);
  }
  if (item.text) {
    const bubble = el("div", "msg user");
    if (item.voice) bubble.append(el("span", "voice-tag", "🎙"));
    bubble.append(document.createTextNode(item.text));
    thread.append(bubble);
  }
  scrollDown(true);
}

function showAssistant(text) {
  hideWelcome();
  const bubble = el("div", "msg assistant");
  bubble.innerHTML = markdown(text);
  thread.append(bubble);
  return bubble;
}

function showError(message) {
  thread.append(el("div", "msg error", message));
  scrollDown(true);
}

const BADGE = { add: "+", new: "+", remove: "−", open: "◐", edit: "✎", list: "✓", undo: "↺" };

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
  }
}

async function run(request) {
  state.busy = true;
  state.turnText = "";
  setActivity("Thinking");
  document.querySelectorAll(".ask").forEach((n) => n.remove());
  try {
    const response = await request();
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      showError(body.detail || "Something went wrong. Please try again.");
    } else {
      await readEvents(response);
    }
  } catch (err) {
    showError("I lost the connection. Please check your internet and try again.");
  } finally {
    state.busy = false;
    state.liveBubble = null;
    setActivity(null);
    refreshComposer();
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
  showUser({ text, images: images.map((i) => i.url), voice: opts.voice });
  const form = new FormData();
  form.append("text", text);
  form.append("voice", opts.voice ? "true" : "false");
  form.append("conversation_id", state.conversation || "");
  images.forEach((img, i) => form.append("images", img.blob, `photo${i}.jpg`));
  run(() => fetch("/api/chat", { method: "POST", body: form }));
}

function confirmCard(card, approved) {
  if (state.busy) return;
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

const voice = { stream: null, recorder: null, chunks: [], ctx: null, analyser: null, raf: 0, heard: false, lastSound: 0, started: 0, cancelled: false, recognition: null };
const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
const canRecord = !!(navigator.mediaDevices && window.MediaRecorder);

function showRecorder(on, status) {
  $("recorder").hidden = !on;
  $("composer").hidden = on;
  if (status) $("rec-status").textContent = status;
}

async function startListening() {
  if (state.busy || !$("recorder").hidden) return;
  window.speechSynthesis?.cancel();
  if (state.speak && window.speechSynthesis) speechSynthesis.speak(new SpeechSynthesisUtterance(""));  // unlock on iOS
  if (state.stt !== "server" || !canRecord) return startBrowserRecognition();
  try {
    voice.stream = voice.stream || await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
  } catch {
    toast("Please allow the microphone to talk to me.");
    return;
  }
  const type = ["audio/webm;codecs=opus", "audio/mp4", "audio/webm"].find((t) => MediaRecorder.isTypeSupported(t)) || "";
  voice.recorder = new MediaRecorder(voice.stream, type ? { mimeType: type } : undefined);
  voice.chunks = []; voice.heard = false; voice.cancelled = false;
  voice.started = voice.lastSound = performance.now();
  voice.recorder.ondataavailable = (e) => e.data.size && voice.chunks.push(e.data);
  voice.recorder.onstop = finishRecording;
  voice.recorder.start();
  voice.ctx = voice.ctx || new (window.AudioContext || window.webkitAudioContext)();
  await voice.ctx.resume();
  voice.source = voice.ctx.createMediaStreamSource(voice.stream);
  voice.analyser = voice.ctx.createAnalyser();
  voice.analyser.fftSize = 1024;
  voice.source.connect(voice.analyser);
  showRecorder(true, "Listening… tap ✓ when you're done");
  drawWave();
}

function drawWave() {
  const canvas = $("wave"), g = canvas.getContext("2d"), data = new Uint8Array(voice.analyser.fftSize);
  const bars = 48, levels = (drawWave.levels = drawWave.levels || new Array(bars).fill(0));
  const color = getComputedStyle(document.documentElement).getPropertyValue("--accent");
  const tick = () => {
    if (!voice.recorder || voice.recorder.state !== "recording") return;
    voice.analyser.getByteTimeDomainData(data);
    let sum = 0;
    for (const v of data) sum += (v - 128) * (v - 128);
    const rms = Math.sqrt(sum / data.length) / 128;
    const now = performance.now();
    if (rms > 0.035) { voice.lastSound = now; if (now - voice.started > 250) voice.heard = true; }
    levels.push(Math.min(1, rms * 6)); levels.shift();
    g.clearRect(0, 0, canvas.width, canvas.height);
    g.fillStyle = color;
    const w = canvas.width / bars;
    levels.forEach((l, i) => { const h = Math.max(4, l * canvas.height); g.fillRect(i * w + w * 0.25, (canvas.height - h) / 2, w * 0.5, h); });
    // Stop by itself after a pause, or give up if nothing was said.
    if (voice.heard && now - voice.lastSound > 1900) return stopListening();
    if (!voice.heard && now - voice.started > 9000) return stopListening(true);
    if (now - voice.started > 90000) return stopListening();
    voice.raf = requestAnimationFrame(tick);
  };
  tick();
}

function stopListening(cancel = false) {
  if (voice.recognition) { voice.cancelled = cancel; voice.recognition.stop(); return; }
  if (!voice.recorder || voice.recorder.state !== "recording") { showRecorder(false); return; }
  voice.cancelled = cancel;
  voice.recorder.stop();
}

function releaseMic() {
  voice.stream?.getTracks().forEach((t) => t.stop());
  voice.stream = null;
}

async function finishRecording() {
  voice.source?.disconnect();
  const type = voice.recorder.mimeType || "audio/webm";
  const blob = new Blob(voice.chunks, { type });
  voice.recorder = null;
  if (voice.cancelled || !voice.heard) {
    showRecorder(false); releaseMic();
    if (!voice.cancelled) toast("I didn't hear anything.");
    return;
  }
  showRecorder(true, "Writing down what you said…");
  try {
    const form = new FormData();
    form.append("audio", blob, type.includes("mp4") ? "speech.m4a" : "speech.webm");
    const r = await fetch("/api/transcribe", { method: "POST", body: form });
    const body = await r.json();
    if (!r.ok) throw new Error(body.detail);
    heard(body.text);
  } catch (err) {
    releaseMic();
    toast(err.message || "I couldn't understand that. Please try again.");
  } finally {
    showRecorder(false);
  }
}

function startBrowserRecognition() {
  if (!SpeechRecognition) { toast("Talking isn't available in this browser."); return; }
  const rec = (voice.recognition = new SpeechRecognition());
  rec.lang = navigator.language || "en-US";
  rec.interimResults = true;
  let finalText = "";
  voice.cancelled = false;
  rec.onresult = (e) => {
    finalText = [...e.results].map((r) => r[0].transcript).join(" ");
    $("rec-status").textContent = finalText;
  };
  rec.onerror = (e) => { if (e.error === "not-allowed") toast("Please allow the microphone to talk to me."); };
  rec.onend = () => {
    voice.recognition = null;
    showRecorder(false);
    if (!voice.cancelled && finalText.trim()) heard(finalText);
  };
  showRecorder(true, "Listening…");
  $("wave").getContext("2d").clearRect(0, 0, 600, 80);
  rec.start();
}

const YES = /^(yes|yeah|yep|yup|ok|okay|sure|correct|right|confirm|save|do it|go ahead|looks good|haan|han|ha|theek hai|thik hai)\b/i;
const NO = /^(no|nope|nah|cancel|stop|wrong|don't|do not|nahi|nahin)\b/i;

function heard(text) {
  text = (text || "").trim();
  if (!text) { releaseMic(); toast("I didn't catch that."); return; }
  const short = text.split(/\s+/).length <= 4, card = state.pendingCard;
  if (card && short && YES.test(text)) { showUser({ text, voice: true }); state.voiceTurn = true; return confirmCard(card, card.allIds); }
  if (card && short && NO.test(text)) { showUser({ text, voice: true }); state.voiceTurn = true; return confirmCard(card, []); }
  send(text, { voice: true });
}

// After a spoken turn with "read aloud" on: say the reply, then listen again.
function afterTurn() {
  if (!state.voiceTurn) return;
  state.voiceTurn = false;
  const text = state.turnText.replace(/[*_`#]/g, "").replace(/\s+/g, " ").trim();
  if (!state.speak || !window.speechSynthesis || !text) { releaseMic(); return; }
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.lang = navigator.language || "en-US";
  utterance.onend = () => { if (state.speak && !state.busy) startListening(); };
  utterance.onerror = releaseMic;
  speechSynthesis.speak(utterance);
}

// ----------------------------------------------------------------- composer

function autosize() {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 160) + "px";
}

function refreshComposer() {
  const hasContent = input.value.trim() || state.images.length;
  const canTalk = (state.stt === "server" && canRecord) || !!SpeechRecognition;
  $("send").hidden = !hasContent && canTalk;
  $("mic").hidden = !!hasContent || !canTalk;
  $("send").disabled = state.busy;
}

input.addEventListener("input", () => { autosize(); refreshComposer(); });
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing && matchMedia("(pointer: fine)").matches) { e.preventDefault(); send(input.value); }
});
$("composer").addEventListener("submit", (e) => { e.preventDefault(); send(input.value); });
$("photo").addEventListener("click", () => $("file").click());
$("file").addEventListener("change", (e) => { addFiles([...e.target.files]); e.target.value = ""; });
$("mic").addEventListener("click", startListening);
$("rec-done").addEventListener("click", () => stopListening(false));
$("rec-cancel").addEventListener("click", () => { stopListening(true); releaseMic(); });
$("speak-toggle").addEventListener("click", () => {
  state.speak = !state.speak;
  localStorage.setItem("speak", state.speak ? "1" : "0");
  $("speak-toggle").setAttribute("aria-pressed", state.speak);
  if (!state.speak) window.speechSynthesis?.cancel();
  toast(state.speak ? "When you talk to me, I'll answer out loud." : "I'll stay quiet.");
});
$("new-chat").addEventListener("click", () => {
  if (state.busy) return;
  state.conversation = null; state.pendingCard = null;
  [...thread.children].forEach((n) => n.id !== "welcome" && n.remove());
  $("welcome").hidden = false;
});
document.querySelector(".quick").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  if (b.dataset.action === "photo") $("file").click();
  else if (b.dataset.say) send(b.dataset.say);
  else { input.value = b.dataset.fill; input.focus(); autosize(); refreshComposer(); }
});

async function boot() {
  $("speak-toggle").setAttribute("aria-pressed", state.speak);
  try {
    const r = await fetch("/api/state");
    if (!r.ok) throw new Error();
    const data = await r.json();
    state.stt = data.stt;
    const hour = new Date().getHours();
    $("hello").textContent = `${hour < 12 ? "Good morning" : hour < 17 ? "Good afternoon" : "Good evening"}, ${data.user.name}`;
    if (data.conversation && data.conversation.items.length) {
      state.conversation = data.conversation.id;
      data.conversation.items.forEach(showItem);
      scrollDown(true);
    }
  } catch {
    showError("I couldn't load. Please refresh the page.");
  }
  refreshComposer();
}
boot();
