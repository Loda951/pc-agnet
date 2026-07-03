import type { AuthSession, AuthTokenResponse, AuthUser, ChatResponse } from "./types";

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "";
const ACCESS_TOKEN_KEY = "pc-agent.accessToken";
const REFRESH_TOKEN_KEY = "pc-agent.refreshToken";
const EXPIRES_IN_KEY = "pc-agent.expiresIn";

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

export async function login(loginIdentifier: string, password: string): Promise<AuthSession> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}/api/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        login_identifier: loginIdentifier,
        password
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
      retryable: response.status >= 500
    });
  }

  return saveAuthSession((await response.json()) as AuthTokenResponse);
}

export async function restoreSession(): Promise<AuthSession | null> {
  const stored = readStoredSession();
  if (!stored) return null;

  const response = await authorizedFetch("/api/auth/me", { method: "GET" });
  if (response.ok) {
    const user = (await response.json()) as AuthUser;
    return { ...readStoredSessionOrThrow(), user };
  }

  if (response.status === 401 || response.status === 403) {
    clearAuthSession();
    return null;
  }

  const detail = await parseErrorDetail(response);
  throw new ApiError(formatApiError(response.status, detail), {
    status: response.status,
    detail,
    retryable: response.status >= 500
  });
}

export async function refreshSession(): Promise<AuthSession> {
  const refreshToken = localStorage.getItem(REFRESH_TOKEN_KEY);
  if (!refreshToken) {
    clearAuthSession();
    throw new ApiError("登录已过期，请重新登录。", { status: 401 });
  }

  let response: Response;
  try {
    response = await fetch(`${API_BASE}/api/auth/refresh`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: refreshToken })
    });
  } catch {
    throw new ApiError("无法连接后端服务，请稍后重试。", { retryable: true });
  }

  if (!response.ok) {
    clearAuthSession();
    const detail = await parseErrorDetail(response);
    throw new ApiError(formatApiError(response.status, detail), {
      status: response.status,
      detail,
      retryable: response.status >= 500
    });
  }

  return saveAuthSession((await response.json()) as AuthTokenResponse);
}

export async function logout(): Promise<void> {
  const refreshToken = localStorage.getItem(REFRESH_TOKEN_KEY);
  clearAuthSession();
  if (!refreshToken) return;

  try {
    await fetch(`${API_BASE}/api/auth/logout`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: refreshToken })
    });
  } catch {
    // Local logout should still complete when the backend is unreachable.
  }
}

export async function sendChat(message: string, conversationId?: number): Promise<ChatResponse> {
  const response = await authorizedFetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      message,
      conversation_id: conversationId
    })
  });

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

function saveAuthSession(payload: AuthTokenResponse): AuthSession {
  localStorage.setItem(ACCESS_TOKEN_KEY, payload.access_token);
  localStorage.setItem(REFRESH_TOKEN_KEY, payload.refresh_token);
  localStorage.setItem(EXPIRES_IN_KEY, String(payload.expires_in));
  return {
    accessToken: payload.access_token,
    refreshToken: payload.refresh_token,
    expiresIn: payload.expires_in,
    user: payload.user
  };
}

export function clearAuthSession() {
  localStorage.removeItem(ACCESS_TOKEN_KEY);
  localStorage.removeItem(REFRESH_TOKEN_KEY);
  localStorage.removeItem(EXPIRES_IN_KEY);
}

function readStoredSession(): AuthSession | null {
  const accessToken = localStorage.getItem(ACCESS_TOKEN_KEY);
  const refreshToken = localStorage.getItem(REFRESH_TOKEN_KEY);
  if (!accessToken || !refreshToken) return null;
  return {
    accessToken,
    refreshToken,
    expiresIn: Number(localStorage.getItem(EXPIRES_IN_KEY) ?? 0),
    user: {
      id: 0,
      login_identifier: "",
      display_name: "",
      status: "unknown",
      last_login_at: null
    }
  };
}

function readStoredSessionOrThrow(): AuthSession {
  const stored = readStoredSession();
  if (!stored) {
    throw new ApiError("登录已过期，请重新登录。", { status: 401 });
  }
  return stored;
}

async function authorizedFetch(
  path: string,
  init: RequestInit = {},
  retryAuth = true
): Promise<Response> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, withAuthHeader(init));
  } catch {
    throw new ApiError("无法连接后端服务，请确认 API 或 Vite 代理已启动。", {
      retryable: true
    });
  }

  if (response.status !== 401 || !retryAuth) {
    return response;
  }

  try {
    await refreshSession();
  } catch {
    return response;
  }

  try {
    return await fetch(`${API_BASE}${path}`, withAuthHeader(init));
  } catch {
    throw new ApiError("无法连接后端服务，请确认 API 或 Vite 代理已启动。", {
      retryable: true
    });
  }
}

function withAuthHeader(init: RequestInit): RequestInit {
  const headers = new Headers(init.headers);
  const accessToken = localStorage.getItem(ACCESS_TOKEN_KEY);
  if (accessToken) {
    headers.set("Authorization", `Bearer ${accessToken}`);
  }
  return { ...init, headers };
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
