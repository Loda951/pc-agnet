import type { AfterSalesTicket, ChatResponse } from "./types";

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

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

export async function createAfterSalesTicket(input: {
  order_id: number;
  order_item_id: number;
  ticket_type: string;
  reason: string;
  description?: string;
}): Promise<AfterSalesTicket> {
  const response = await fetch(`${API_BASE}/api/after-sales`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input)
  });
  if (!response.ok) {
    throw new Error(`After-sales request failed: ${response.status}`);
  }
  const payload = await response.json();
  return payload.ticket;
}
