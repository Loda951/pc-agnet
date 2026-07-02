import type { ChatResponse } from "./types";

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "";

export async function sendChat(message: string, conversationId?: number): Promise<ChatResponse> {
  const response = await fetch(`${API_BASE}/api/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      message,
      conversation_id: conversationId
    })
  });
  if (!response.ok) {
    throw new Error(`Chat request failed: ${response.status}`);
  }
  return response.json();
}
