const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const LEVELS = { strong: "Vững", good: "Khá", basic: "Cơ bản", gap: "Cần ôn lại", not_assessed: "Chưa đánh giá" };
const STATUS = {
  pending: "Chưa hỏi", in_progress: "Đang hỏi", mastered: "Hiểu tốt",
  partial: "Hiểu một phần", gap: "Chưa nắm", unassessed: "Chưa đủ dữ liệu",
};
const ACTIONS = {
  ask_main: "Hỏi câu chính", probe_deeper: "Hỏi sâu", challenge: "Phản biện", hint: "Gợi ý",
  clarify: "Diễn đạt lại", encourage: "Động viên", redirect: "Kéo về câu hỏi",
  next_concept: "Chuyển chủ đề", wrap_up: "Kết thúc",
};
const BLOOM = ["—", "Nhớ", "Hiểu", "Vận dụng", "Phân tích", "Đánh giá", "Sáng tạo"];
const IMPORTANCE = { 3: "Cốt lõi", 2: "Quan trọng", 1: "Bổ trợ" };
const SPEAKING_LEVEL = 0.04;

const state = {
  docs: [],
  selectedDoc: null,
  mode: "voice",
  session: null,
  voice: null,
  startedAt: 0,
  timer: null,
  insightsTimer: null,
  finished: false,
  sending: false,
  bubbles: { learner: null, interviewer: null },
};

// -- Tiện ích ---------------------------------------------------------------

function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key === "class") node.className = value;
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

async function api(path, options = {}, retryAuth = true) {
  const headers = {
    ...(options.body && !(options.body instanceof FormData) ? { "Content-Type": "application/json" } : {}),
    ...(options.headers || {}),
  };
  const response = await fetch(path, { ...options, headers });
  if (response.status === 401 && retryAuth && path !== "/api/auth/login") {
    const token = window.prompt("Nhập mật khẩu để dùng ứng dụng:");
    if (token) {
      const login = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token }),
      });
      if (login.ok) return api(path, options, false);
    }
  }
  let data = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  if (!response.ok) {
    const detail = data && data.detail;
    const message = typeof detail === "string"
      ? detail
      : Array.isArray(detail) ? detail.map((d) => d.msg).join("; ") : `Lỗi máy chủ (${response.status})`;
    throw new Error(message);
  }
  return data;
}

let toastTimer = null;
function toast(message, kind = "error") {
  const node = $("#toast");
  node.textContent = message;
  node.className = `toast ${kind === "warn" ? "warn" : ""}`;
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.hidden = true; }, 6000);
}

function formatClock(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

function truncate(text, max) {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

function showView(name) {
  for (const view of ["setup", "interview", "report"]) $(`#view-${view}`).hidden = view !== name;
  window.scrollTo({ top: 0 });
}

// -- Sức khỏe hệ thống và tài liệu -----------------------------------------

async function loadHealth() {
  const pill = $("#health");
  try {
    const health = await api("/api/health");
    $("#time-limit").textContent = formatClock(health.interview_minutes * 60);
    if (health.llm_configured) {
      pill.textContent = `Gemini · ${health.models.live}`;
      pill.className = "pill ok";
    } else {
      pill.textContent = "Chưa có GEMINI_API_KEY";
      pill.className = "pill warn";
      pill.title = "Tạo file .env từ .env.example, điền key rồi khởi động lại server";
    }
  } catch {
    pill.textContent = "Không kết nối được máy chủ";
    pill.className = "pill warn";
  }
}

async function loadDocs() {
  try {
    state.docs = await api("/api/documents");
  } catch (error) {
    toast(error.message);
    return;
  }
  renderDocList();
  if (!state.selectedDoc && state.docs.length) selectDoc(state.docs[0]);
}

function renderDocList() {
  const list = $("#doc-list");
  list.replaceChildren();
  $("#doc-list-head").hidden = state.docs.length === 0;
  for (const doc of state.docs) {
    const active = state.selectedDoc && state.selectedDoc.id === doc.id;
    list.append(
      h("li", {},
        h("button", { type: "button", class: `doc-item${active ? " active" : ""}`, onclick: () => selectDoc(doc) },
          h("span", { class: "doc-name" }, doc.title),
          h("span", { class: "doc-meta" }, `${doc.filename} · ${doc.concepts.length} chủ đề`))));
  }
}

function selectDoc(doc) {
  state.selectedDoc = doc;
  renderDocList();
  $("#doc-empty").hidden = true;
  $("#doc-detail").hidden = false;
  $("#doc-title").textContent = doc.title;
  $("#doc-summary").textContent = doc.summary;
  $("#doc-truncated").hidden = !doc.truncated;
  const list = $("#concept-list");
  list.replaceChildren(
    ...doc.concepts.map((concept) =>
      h("li", {},
        h("div", { class: "concept-name" },
          h("span", {}, concept.name),
          h("span", { class: `imp imp-${concept.importance}` }, IMPORTANCE[concept.importance] || "")),
        h("div", { class: "concept-summary" }, concept.summary))));
}

async function uploadFile(file) {
  const status = $("#upload-status");
  const started = Date.now();
  const render = () => {
    const seconds = Math.round((Date.now() - started) / 1000);
    status.replaceChildren(
      h("div", { class: "spinner small" }),
      h("span", {}, `Đang đọc "${file.name}" và soạn câu hỏi… ${seconds}s`));
  };
  status.hidden = false;
  status.className = "upload-status busy";
  render();
  const interval = setInterval(render, 1000);
  try {
    const form = new FormData();
    form.append("file", file);
    const doc = await api("/api/documents", { method: "POST", body: form });
    state.docs = [doc, ...state.docs.filter((d) => d.id !== doc.id)];
    selectDoc(doc);
    status.className = "upload-status ok";
    status.textContent = `Đã soạn ${doc.concepts.length} chủ đề từ "${doc.filename}".`;
  } catch (error) {
    status.className = "upload-status error";
    status.textContent = error.message;
  } finally {
    clearInterval(interval);
  }
}

function setupDropzone() {
  const zone = $("#dropzone");
  const input = $("#file-input");
  input.addEventListener("change", () => {
    if (input.files[0]) uploadFile(input.files[0]);
    input.value = "";
  });
  zone.addEventListener("dragover", (event) => { event.preventDefault(); zone.classList.add("dragging"); });
  zone.addEventListener("dragleave", () => zone.classList.remove("dragging"));
  zone.addEventListener("drop", (event) => {
    event.preventDefault();
    zone.classList.remove("dragging");
    const file = event.dataTransfer.files[0];
    if (file) uploadFile(file);
  });
}

function setupModeSwitch() {
  for (const button of $$(".seg")) {
    button.addEventListener("click", () => {
      state.mode = button.dataset.mode;
      for (const other of $$(".seg")) {
        other.classList.toggle("active", other === button);
        other.setAttribute("aria-checked", String(other === button));
      }
      $("#barge-in-row").hidden = state.mode !== "voice";
    });
  }
}

// -- Hội thoại --------------------------------------------------------------

function chatScrollToEnd() {
  const chat = $("#chat");
  chat.scrollTop = chat.scrollHeight;
}

function addBubble(role, text) {
  const bubble = h("div", { class: `bubble ${role}` },
    h("span", { class: "bubble-label" }, role === "interviewer" ? "Minh (AI)" : state.session.learner_name),
    h("span", { class: "bubble-text" }, text));
  $("#chat").append(bubble);
  chatScrollToEnd();
  return bubble;
}

function addTyping() {
  const node = h("div", { class: "typing", "aria-label": "AI đang soạn câu trả lời" }, h("span"), h("span"), h("span"));
  $("#chat").append(node);
  chatScrollToEnd();
  return node;
}

function appendTranscript(role, text) {
  let bubble = state.bubbles[role];
  if (!bubble) {
    bubble = addBubble(role, "");
    // Lời người học thuộc về lượt hiện tại nên luôn đứng trước câu trả lời của AI trong cùng lượt.
    if (role === "learner" && state.bubbles.interviewer) {
      $("#chat").insertBefore(bubble, state.bubbles.interviewer);
    }
    state.bubbles[role] = bubble;
  }
  bubble.querySelector(".bubble-text").textContent += text;
  chatScrollToEnd();
}

function endTranscriptTurn(interrupted) {
  if (interrupted && state.bubbles.interviewer) state.bubbles.interviewer.classList.add("interrupted");
  state.bubbles = { learner: null, interviewer: null };
}

function startTimers() {
  state.startedAt = Date.now();
  clearInterval(state.timer);
  state.timer = setInterval(() => {
    $("#timer").textContent = formatClock((Date.now() - state.startedAt) / 1000);
  }, 1000);
}

function stopTimers() {
  clearInterval(state.timer);
  clearInterval(state.insightsTimer);
  state.timer = null;
  state.insightsTimer = null;
}

function setupInterviewView() {
  const session = state.session;
  $("#stage-title").textContent = session.document_title;
  $("#timer").textContent = "00:00";
  $("#time-limit").textContent = formatClock(session.time_limit_seconds);
  $("#chat").replaceChildren();
  $("#chat").classList.remove("captions-off");
  $("#show-captions").checked = true;
  $("#finished-banner").hidden = true;
  $("#end-btn").disabled = false;
  $("#voice-stage").hidden = session.mode !== "voice";
  $("#chat-form").hidden = session.mode !== "text";
  $("#chat-input").value = "";
  state.bubbles = { learner: null, interviewer: null };
  startTimers();
  if (!$("#teacher-panel").hidden) refreshInsights();
}

function markFinished() {
  state.finished = true;
  $("#chat-form").hidden = true;
  $("#finished-banner").hidden = false;
  clearInterval(state.timer);
  refreshInsights();
}

async function sendMessage(event) {
  event.preventDefault();
  const input = $("#chat-input");
  const text = input.value.trim();
  if (!text || state.sending || state.finished) return;
  state.sending = true;
  $("#send-btn").disabled = true;
  input.value = "";
  const bubble = addBubble("learner", text);
  const typing = addTyping();
  try {
    const result = await api(`/api/sessions/${state.session.id}/messages`, {
      method: "POST",
      body: JSON.stringify({ text }),
    });
    typing.remove();
    addBubble("interviewer", result.message);
    if (result.finished) markFinished();
  } catch (error) {
    // Máy chủ không lưu lượt bị lỗi nên trả lại nội dung để người học gửi lại.
    typing.remove();
    bubble.remove();
    input.value = text;
    toast(error.message);
  } finally {
    state.sending = false;
    $("#send-btn").disabled = false;
    input.focus();
    if (!$("#teacher-panel").hidden) refreshInsights();
  }
}

// -- Giọng nói --------------------------------------------------------------

class VoiceClient {
  constructor(handlers) {
    this.handlers = handlers;
    this.allowBargeIn = true;
    this.muted = false;
    this.playing = false;
    this.ended = false;
    this.stopped = false;
  }

  // Tạo AudioContext ngay trong thao tác bấm nút để trình duyệt cho phép phát âm thanh.
  prepareAudio() {
    this.captureCtx = new AudioContext();
    this.playCtx = new AudioContext({ sampleRate: 24000 });
  }

  async start(sessionId, allowBargeIn) {
    this.allowBargeIn = allowBargeIn;
    await Promise.all([
      this.captureCtx.audioWorklet.addModule("/capture-worklet.js"),
      this.playCtx.audioWorklet.addModule("/playback-worklet.js"),
    ]);
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    });
    await Promise.all([this.captureCtx.resume(), this.playCtx.resume()]);

    this.capture = new AudioWorkletNode(this.captureCtx, "capture-processor");
    const silent = this.captureCtx.createGain();
    silent.gain.value = 0;
    this.captureCtx.createMediaStreamSource(this.stream).connect(this.capture);
    this.capture.connect(silent).connect(this.captureCtx.destination);
    this.capture.port.onmessage = (event) => {
      if (event.data.type === "audio") {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(event.data.buffer);
      } else if (event.data.type === "level") {
        this.handlers.onLevel(this.isMicOpen() ? event.data.value : 0);
      }
    };

    this.player = new AudioWorkletNode(this.playCtx, "playback-processor", {
      numberOfInputs: 0,
      outputChannelCount: [1],
    });
    this.player.connect(this.playCtx.destination);
    this.player.port.onmessage = (event) => {
      if (event.data.type === "state") this.setPlaying(event.data.playing);
    };

    const protocol = location.protocol === "https:" ? "wss" : "ws";
    this.ws = new WebSocket(`${protocol}://${location.host}/api/sessions/${sessionId}/voice`);
    this.ws.binaryType = "arraybuffer";
    this.ws.onmessage = (event) => this.onMessage(event);
    this.ws.onclose = (event) => this.onClose(event);
  }

  isMicOpen() {
    return !this.muted && (this.allowBargeIn || !this.playing);
  }

  syncMic() {
    if (this.capture) this.capture.port.postMessage({ type: "mute", value: !this.isMicOpen() });
  }

  setMuted(muted) {
    this.muted = muted;
    this.syncMic();
    this.handlers.onState();
  }

  setPlaying(playing) {
    this.playing = playing;
    this.syncMic();
    this.handlers.onState();
    if (!playing && this.ended) this.finishSoon();
  }

  onMessage(event) {
    if (typeof event.data !== "string") {
      const buffer = event.data.byteLength % 2 ? event.data.slice(0, event.data.byteLength - 1) : event.data;
      this.player.port.postMessage({ type: "audio", buffer }, [buffer]);
      return;
    }
    const message = JSON.parse(event.data);
    switch (message.type) {
      case "transcript":
        this.handlers.onTranscript(message.role, message.text);
        break;
      case "interrupted":
        this.player.port.postMessage({ type: "flush" });
        this.handlers.onTurnEnd(true);
        break;
      case "turn_complete":
        this.handlers.onTurnEnd(false);
        break;
      case "status":
        this.connection = message.state;
        this.handlers.onState();
        break;
      case "warning":
        toast(message.message, "warn");
        break;
      case "error":
        this.error = message.message;
        this.handlers.onState();
        toast(message.message);
        break;
      case "session_ended":
        this.ended = true;
        this.handlers.onState();
        if (!this.playing) this.finishSoon();
        break;
      default:
        break;
    }
  }

  finishSoon() {
    clearTimeout(this.finishTimer);
    this.finishTimer = setTimeout(() => {
      this.stop();
      this.handlers.onEnded();
    }, 600);
  }

  onClose() {
    const unexpected = !this.stopped && !this.ended;
    this.stop();
    if (unexpected) this.handlers.onDisconnected(this.error);
  }

  stop() {
    if (this.stopped) return;
    this.stopped = true;
    try {
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify({ type: "end" }));
        this.ws.close();
      }
    } catch {
      // WebSocket đã đóng.
    }
    if (this.stream) this.stream.getTracks().forEach((track) => track.stop());
    for (const ctx of [this.captureCtx, this.playCtx]) {
      if (ctx && ctx.state !== "closed") ctx.close();
    }
    this.handlers.onState();
  }

  get label() {
    if (this.error) return this.error;
    if (this.ended) return "Buổi phỏng vấn đã kết thúc";
    if (this.stopped) return "Đã dừng";
    if (this.connection === "reconnecting") return "Đang nối lại…";
    if (this.connection !== "connected") return "Đang kết nối…";
    if (this.playing) return this.allowBargeIn ? "AI đang nói · bạn có thể ngắt lời" : "AI đang nói";
    if (this.muted) return "Mic đang tắt";
    return "Đang nghe bạn nói…";
  }

  get visualState() {
    if (this.error) return "error";
    if (this.ended || this.stopped) return "ended";
    if (this.connection !== "connected") return this.connection === "reconnecting" ? "reconnecting" : "connecting";
    if (this.playing) return "speaking";
    if (this.muted) return "muted";
    return "listening";
  }
}

function renderVoiceState() {
  const voice = state.voice;
  if (!voice) return;
  $("#orb").dataset.state = voice.visualState;
  $("#orb-label").textContent = voice.label;
  $("#mute-btn").textContent = voice.muted ? "Bật mic" : "Tắt mic";
  $("#mute-btn").setAttribute("aria-pressed", String(voice.muted));
}

function renderLevel(level) {
  const voice = state.voice;
  $("#orb").style.setProperty("--level", Math.min(1, level / 0.12).toFixed(3));
  if (voice && !voice.playing && !voice.ended && !voice.error && voice.connection === "connected") {
    $("#orb-label").textContent = level > SPEAKING_LEVEL ? "Bạn đang nói…" : voice.label;
  }
}

function createVoiceClient() {
  return new VoiceClient({
    onState: renderVoiceState,
    onLevel: renderLevel,
    onTranscript: appendTranscript,
    onTurnEnd: endTranscriptTurn,
    onEnded: () => {
      markFinished();
      finishInterview();
    },
    onDisconnected: (error) => {
      renderVoiceState();
      markFinished();
      if (!error) toast("Mất kết nối giọng nói. Bạn có thể xem báo cáo với phần đã trả lời.", "warn");
    },
  });
}

// -- Bắt đầu và kết thúc ----------------------------------------------------

async function startInterview(event) {
  event.preventDefault();
  if (!state.selectedDoc) return;
  const button = $("#start-btn");
  button.disabled = true;
  button.textContent = "Đang chuẩn bị…";

  let voice = null;
  if (state.mode === "voice") {
    voice = createVoiceClient();
    voice.prepareAudio();
  }
  try {
    const result = await api("/api/sessions", {
      method: "POST",
      body: JSON.stringify({
        document_id: state.selectedDoc.id,
        learner_name: $("#learner-name").value.trim() || "bạn",
        mode: state.mode,
      }),
    });
    state.session = result.session;
    state.finished = false;
    showView("interview");
    setupInterviewView();
    if (state.mode === "text") {
      addBubble("interviewer", result.message);
      $("#chat-input").focus();
    } else {
      state.voice = voice;
      renderVoiceState();
      await voice.start(state.session.id, $("#barge-in").checked);
    }
  } catch (error) {
    if (voice) voice.stop();
    state.voice = null;
    const denied = error && error.name === "NotAllowedError";
    toast(denied ? "Trình duyệt chưa cho phép dùng micro. Hãy cấp quyền rồi thử lại." : error.message);
    stopTimers();
    state.session = null;
    showView("setup");
  } finally {
    button.disabled = false;
    button.textContent = "Bắt đầu phỏng vấn";
  }
}

async function finishInterview() {
  if (!state.session) return;
  if (state.voice) {
    state.voice.stop();
    state.voice = null;
  }
  stopTimers();
  showView("report");
  $("#report").hidden = true;
  $("#report-loading").hidden = false;
  $("#report-retry").hidden = true;
  $("#report-loading-text").textContent = "Đang tổng hợp báo cáo…";
  $(".loading-card .spinner").hidden = false;
  try {
    const report = await api(`/api/sessions/${state.session.id}/finish`, { method: "POST" });
    renderReport(report);
  } catch (error) {
    $(".loading-card .spinner").hidden = true;
    $("#report-loading-text").textContent = `Chưa tạo được báo cáo: ${error.message}`;
    $("#report-retry").hidden = false;
  }
}

function confirmEnd() {
  if (state.finished || window.confirm("Kết thúc buổi phỏng vấn và xem báo cáo?")) finishInterview();
}

// -- Bảng giáo viên ---------------------------------------------------------

function toggleTeacher() {
  const panel = $("#teacher-panel");
  panel.hidden = !panel.hidden;
  $("#interview-grid").classList.toggle("with-teacher", !panel.hidden);
  $("#toggle-teacher").setAttribute("aria-pressed", String(!panel.hidden));
  clearInterval(state.insightsTimer);
  if (!panel.hidden) {
    refreshInsights();
    state.insightsTimer = setInterval(refreshInsights, 3000);
  }
}

async function refreshInsights() {
  if (!state.session || $("#teacher-panel").hidden) return;
  try {
    renderInsights(await api(`/api/sessions/${state.session.id}/insights`));
  } catch {
    // Lần làm mới sau sẽ thử lại.
  }
}

function scoreChip(label, value) {
  return h("span", { class: "score" }, `${label} ${value}/4`);
}

function renderInsights(data) {
  $("#progress-list").replaceChildren(
    ...data.concepts.map((c) => {
      const meta = [
        c.evidence_count ? `${c.score}/100` : "Chưa có dữ liệu",
        `Bloom cao nhất: ${BLOOM[c.max_bloom] || "—"}`,
        `ý chính ${c.covered_key_points}/${c.total_key_points}`,
      ];
      if (c.hints) meta.push(`${c.hints} gợi ý`);
      if (c.rote_flags) meta.push(`${c.rote_flags} lần có dấu hiệu học thuộc`);
      return h("div", { class: `progress-item status-${c.status}${c.id === data.current_concept_id ? " current" : ""}` },
        h("div", { class: "progress-top" }, h("span", {}, c.name), h("span", { class: "status-chip" }, STATUS[c.status])),
        h("div", { class: "bar" }, h("span", { style: `width:${c.evidence_count ? c.score : 0}%` })),
        h("div", { class: "progress-meta" }, meta.join(" · ")),
        c.misconceptions.length ? h("div", { class: "progress-warn" }, `Hiểu lầm: ${c.misconceptions.join("; ")}`) : null);
    }));

  const list = $("#eval-list");
  if (!data.evaluations.length) {
    list.replaceChildren(h("p", { class: "muted small" }, "Chưa có lượt nào được chấm."));
    return;
  }
  list.replaceChildren(
    ...data.evaluations.map((e) =>
      h("div", { class: "eval-item" },
        h("div", { class: "eval-top" },
          h("span", {}, e.concept_name),
          e.action ? h("span", { class: "action-chip" }, ACTIONS[e.action] || e.action) : null),
        h("p", { class: "eval-answer" }, `“${truncate(e.answer, 180)}”`),
        h("div", { class: "scores" },
          scoreChip("Đúng", e.correctness), scoreChip("Đủ ý", e.completeness), scoreChip("Lập luận", e.reasoning),
          h("span", { class: "score" }, `Bloom: ${BLOOM[e.bloom_level] || "—"}`),
          e.rote_signal === "high" ? h("span", { class: "score" }, "Nghi học thuộc") : null),
        h("p", {}, e.summary),
        e.reason ? h("p", { class: "eval-reason" }, `Quyết định: ${e.reason}`) : null)));
}

// -- Báo cáo ----------------------------------------------------------------

function levelBadge(level) {
  return h("span", { class: `level-badge level-${level}` }, LEVELS[level] || level);
}

function bulletList(items) {
  return h("ul", { class: "bullets" }, items.map((item) => h("li", {}, item)));
}

function renderReport(report) {
  $("#report-loading").hidden = true;
  $("#report").hidden = false;

  const ring = $("#score-ring");
  ring.style.setProperty("--score", report.overall_score);
  const ringColors = { strong: "var(--success)", good: "#2f78c4", basic: "var(--warning)", gap: "var(--danger)" };
  ring.style.setProperty("--ring", ringColors[report.overall_level] || "var(--muted)");
  $("#score-value").textContent = report.overall_score;
  const badge = $("#overall-level");
  badge.className = `level-badge level-${report.overall_level}`;
  badge.textContent = LEVELS[report.overall_level];
  $("#report-title").textContent = state.session.document_title;
  $("#report-meta").textContent =
    `${state.session.learner_name} · ${new Date(report.generated_at).toLocaleString("vi-VN")} · ` +
    `${state.session.mode === "voice" ? "Phỏng vấn giọng nói" : "Phỏng vấn nhắn tin"}`;

  const learnerPanel = $("#tab-learner");
  const conceptCards = report.concepts.map((c) =>
    h("div", { class: "card concept-card" },
      h("div", { class: "concept-card-head" }, h("h4", {}, c.name), levelBadge(c.level)),
      c.level !== "not_assessed" ? h("div", { class: "bar" }, h("span", { style: `width:${c.score}%` })) : null,
      c.strengths.length ? h("div", {}, h("div", { class: "label" }, "Điểm mạnh"), bulletList(c.strengths)) : null,
      c.gaps.length ? h("div", {}, h("div", { class: "label" }, "Cần cải thiện"), bulletList(c.gaps)) : null,
      c.advice ? h("p", {}, c.advice) : null,
      c.review_pages.length ? h("p", { class: "pages" }, `Xem lại trang ${c.review_pages.join(", ")}`) : null));
  learnerPanel.replaceChildren(
    h("div", { class: "card report-section" }, h("h3", {}, "Nhận xét chung"), h("p", {}, report.summary_for_learner)),
    report.study_plan.length
      ? h("div", { class: "card report-section" },
        h("h3", {}, "Kế hoạch ôn tập"),
        h("ol", { class: "plan" }, report.study_plan.map((step) => h("li", {}, step))))
      : null,
    conceptCards.length ? h("div", { class: "concept-grid" }, conceptCards) : null);

  const rows = report.concepts.map((c) =>
    h("tr", {},
      h("td", {}, c.name),
      h("td", {}, levelBadge(c.level)),
      h("td", {}, c.level === "not_assessed" ? "—" : `${c.score}`),
      h("td", {}, BLOOM[c.max_bloom] || "—"),
      h("td", {}, String(c.hints)),
      h("td", {}, c.misconceptions.length ? c.misconceptions.join("; ") : "—"),
      h("td", {}, c.evidence.length ? c.evidence.map((q) => h("div", { class: "quote" }, `“${q}”`)) : "—")));
  $("#tab-teacher").replaceChildren(
    h("div", { class: "card report-section" }, h("h3", {}, "Đánh giá khách quan"), h("p", {}, report.summary_for_teacher)),
    report.teacher_notes.length
      ? h("div", { class: "card report-section" }, h("h3", {}, "Quan sát đáng chú ý"), bulletList(report.teacher_notes))
      : null,
    h("div", { class: "card report-section" },
      h("h3", {}, "Chi tiết theo chủ đề"),
      h("div", { class: "table-wrap" },
        h("table", {},
          h("thead", {}, h("tr", {}, ...["Chủ đề", "Mức", "Điểm", "Bloom cao nhất", "Gợi ý", "Hiểu lầm", "Trích dẫn"].map((t) => h("th", {}, t)))),
          h("tbody", {}, rows)))));
  selectTab("learner");
}

function selectTab(name) {
  for (const tab of $$(".tab")) {
    const active = tab.dataset.tab === name;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
  }
  $("#tab-learner").hidden = name !== "learner";
  $("#tab-teacher").hidden = name !== "teacher";
}

function restart() {
  stopTimers();
  state.session = null;
  state.finished = false;
  if (!$("#teacher-panel").hidden) toggleTeacher();
  showView("setup");
}

// -- Khởi động --------------------------------------------------------------

function init() {
  setupDropzone();
  setupModeSwitch();
  $("#start-form").addEventListener("submit", startInterview);
  $("#chat-form").addEventListener("submit", sendMessage);
  $("#chat-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      $("#chat-form").requestSubmit();
    }
  });
  $("#end-btn").addEventListener("click", confirmEnd);
  $("#report-btn").addEventListener("click", finishInterview);
  $("#report-retry").addEventListener("click", finishInterview);
  $("#toggle-teacher").addEventListener("click", toggleTeacher);
  $("#mute-btn").addEventListener("click", () => state.voice && state.voice.setMuted(!state.voice.muted));
  $("#show-captions").addEventListener("change", (event) => {
    $("#chat").classList.toggle("captions-off", !event.target.checked);
  });
  for (const tab of $$(".tab")) tab.addEventListener("click", () => selectTab(tab.dataset.tab));
  $("#restart-btn").addEventListener("click", restart);
  window.addEventListener("beforeunload", () => state.voice && state.voice.stop());
  loadHealth();
  loadDocs();
}

init();
