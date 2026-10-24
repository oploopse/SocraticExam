// Gọi API của backend (app/main.py), cùng các endpoint mà giao diện cũ (web/app.js) dùng.

import { askPassword } from "./login.js";

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }

  get offline() {
    return this.status === 0;
  }
}

function errorMessage(status, data) {
  const detail = data && data.detail;
  if (typeof detail === "string" && detail.trim()) return detail;
  if (Array.isArray(detail) && detail.length) return detail.map((d) => d.msg).join("; ");
  return `Máy chủ báo lỗi (HTTP ${status}).`;
}

let pendingLogin = null;

/** Trả về null khi đăng nhập được, hoặc câu báo lỗi cho hộp mật khẩu. */
async function submitPassword(password) {
  try {
    const response = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ token: password }),
    });
    if (response.ok) return null;
    return response.status === 401 ? "Mật khẩu không đúng." : `Máy chủ báo lỗi (HTTP ${response.status}).`;
  } catch {
    return "Không kết nối được máy chủ.";
  }
}

// Backend bật APP_ACCESS_TOKEN (mật khẩu truy cập) thì mọi API trả 401 cho tới khi đăng nhập; cookie HttpOnly do
// /api/auth/login đặt được gửi kèm cả các request sau và WebSocket. Nhiều request cùng gặp 401 chỉ hỏi một lần.
function login() {
  if (!pendingLogin) {
    pendingLogin = askPassword(submitPassword).finally(() => {
      pendingLogin = null;
    });
  }
  return pendingLogin;
}

async function request(path, options = {}, retryAuth = true) {
  const { method = "GET", json, form, timeoutMs = 120_000 } = options;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    let response;
    try {
      response = await fetch(path, {
        method,
        headers: json ? { "Content-Type": "application/json", Accept: "application/json" } : { Accept: "application/json" },
        body: json ? JSON.stringify(json) : form,
        signal: controller.signal,
      });
    } catch (error) {
      throw new ApiError(
        error.name === "AbortError" ? "Giám khảo AI phản hồi quá lâu." : "Không kết nối được máy chủ.",
        error.name === "AbortError" ? 408 : 0,
      );
    }
    if (response.status === 401 && retryAuth) {
      clearTimeout(timer);
      if (await login()) return request(path, options, false);
    }
    let data = null;
    try {
      data = await response.json();
    } catch {
      data = null;
    }
    if (!response.ok) throw new ApiError(errorMessage(response.status, data), response.status);
    return data;
  } finally {
    clearTimeout(timer);
  }
}

/** { llm_configured, models: { brain, fast, live }, interview_minutes, session_options } */
export function health() {
  return request("/api/health", { timeoutMs: 10_000 });
}

/** Các tài liệu đã tải, mới nhất trước. */
export function listDocuments() {
  return request("/api/documents", { timeoutMs: 15_000 });
}

/**
 * Tải file gốc lên để backend đọc và soạn bản đồ kiến thức.
 * Trả về { id, filename, title, summary, truncated, concepts: [{ id, name, summary, importance }] }.
 */
export function uploadDocument(file) {
  const form = new FormData();
  form.append("file", file);
  return request("/api/documents", { method: "POST", form, timeoutMs: 300_000 });
}

/**
 * Trả về { session, message }; message là câu mở đầu ở chế độ nhắn tin, null ở chế độ giọng nói.
 * pageFrom/pageTo (bỏ trống là cả tài liệu), questionCount (5–10), durationMinutes (5–15) là tuỳ chọn.
 */
export function startSession({ documentId, learnerName, mode, pageFrom, pageTo, questionCount, durationMinutes }) {
  return request("/api/sessions", {
    method: "POST",
    // Ở chế độ nhắn tin máy chủ soạn luôn câu hỏi đầu nên cần chờ lâu hơn một lượt gọi Gemini.
    timeoutMs: mode === "text" ? 90_000 : 30_000,
    json: {
      document_id: documentId,
      learner_name: learnerName || "bạn",
      mode,
      page_from: pageFrom ?? null,
      page_to: pageTo ?? null,
      question_count: questionCount ?? null,
      duration_minutes: durationMinutes ?? null,
    },
  });
}

/** Trả về { message, finished }. Máy chủ chấm rồi soạn câu tiếp theo, mỗi bước tự chuyển model khi quá chậm. */
export function sendAnswer(sessionId, text) {
  return request(`/api/sessions/${sessionId}/messages`, {
    method: "POST",
    json: { text: text.slice(0, 4000) },
    timeoutMs: 90_000,
  });
}

/** Điểm từng lượt và quyết định của giám khảo (hỏi sâu hay chuyển chủ đề). */
export function getInsights(sessionId) {
  return request(`/api/sessions/${sessionId}/insights`, { timeoutMs: 15_000 });
}

/** Kết thúc phiên và lấy báo cáo. */
export function finishSession(sessionId) {
  return request(`/api/sessions/${sessionId}/finish`, { method: "POST", timeoutMs: 180_000 });
}

/** Thông tin phiên, gồm toàn bộ lượt hỏi đáp. */
export function getSession(sessionId) {
  return request(`/api/sessions/${sessionId}`, { timeoutMs: 15_000 });
}
