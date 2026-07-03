import type { ChatResponse } from "./types";

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "";

type ApiErrorOptions = {
  status?: number;
  detail?: unknown;
  retryable?: boolean;
};

export class ApiError extends Error {
  status?: number;
  detail?: unknown;
  retryable: boolean;

  constructor(message: string, options: ApiErrorOptions = {}) {
    super(message);
    this.name = "ApiError";
    this.status = options.status;
    this.detail = options.detail;
    this.retryable = options.retryable ?? false;
  }
}

export async function sendChat(message: string, conversationId?: number): Promise<ChatResponse> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}/api/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message,
        conversation_id: conversationId
      })
    });
  } catch {
    throw new ApiError("无法连接后端服务，请确认 API 或 Vite 代理已启动。", {
      retryable: true
    });
  }

  if (!response.ok) {
    const detail = await parseErrorDetail(response);
    throw new ApiError(formatApiError(response.status, detail), {
      status: response.status,
      detail,
      retryable: response.status === 408 || response.status === 429 || response.status >= 500
    });
  }
  return response.json();
}

async function parseErrorDetail(response: Response): Promise<unknown> {
  const contentType = response.headers.get("content-type") ?? "";
  if (contentType.includes("application/json")) {
    try {
      const payload: unknown = await response.json();
      return isRecord(payload) && "detail" in payload ? payload.detail : payload;
    } catch {
      return null;
    }
  }

  try {
    const text = await response.text();
    return text || null;
  } catch {
    return null;
  }
}

function formatApiError(status: number, detail: unknown): string {
  if (isRecord(detail) && typeof detail.display_message === "string") {
    return detail.display_message;
  }
  if (isRecord(detail) && typeof detail.message === "string") {
    return detail.message;
  }
  if (typeof detail === "string" && detail.trim()) {
    return detail;
  }
  if (Array.isArray(detail)) {
    return "请求参数未通过校验，请检查输入后重试。";
  }
  if (status >= 500) {
    return `后端暂时不可用（HTTP ${status}），稍后可以重试。`;
  }
  return `请求失败（HTTP ${status}）。`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
