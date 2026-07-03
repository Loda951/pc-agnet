import { FormEvent, useState } from "react";
import { ApiError, sendChat } from "./api";
import { ChatPanel } from "./components/ChatPanel";
import { ContextPanel } from "./components/ContextPanel";
import { Sidebar } from "./components/Sidebar";
import type {
  BoundaryClassification,
  ChatMessage,
  ConversationTurn,
  EvidenceItem,
  HandoffNotice,
  OperatorProfile,
  OrderCard,
  PendingRequest,
  ProductCard,
  RequestError,
  ResponseStatus,
  SuggestedAction
} from "./types";

const quickPrompts = [
  "推荐 300 元以内无线鼠标",
  "RGB 红轴键盘怎么选",
  "帮我查最近订单",
  "我要申请退货",
  "推荐一台手机"
];

const ticketTypeLabels: Record<string, string> = {
  return: "退货",
  exchange: "换货",
  refund: "退款",
  repair: "维修"
};

const operatorProfile: OperatorProfile = {
  name: "演示客服",
  role: "PC 外设专员",
  userId: 1,
  authState: "placeholder",
  statusLabel: "登录占位"
};

type SubmitOptions = {
  appendUser?: boolean;
  conversationId?: number;
  messageId?: string;
};

export default function App() {
  const [conversationId, setConversationId] = useState<number | undefined>();
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [responseStatus, setResponseStatus] = useState<ResponseStatus>("ready");
  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      id: "hello",
      role: "assistant",
      content: "今天想看哪类外设？",
      createdAt: new Date().toISOString(),
      status: "received"
    }
  ]);
  const [products, setProducts] = useState<ProductCard[]>([]);
  const [order, setOrder] = useState<OrderCard | null>(null);
  const [boundary, setBoundary] = useState<BoundaryClassification | null>(null);
  const [evidence, setEvidence] = useState<EvidenceItem[]>([]);
  const [suggestedActions, setSuggestedActions] = useState<SuggestedAction[]>([]);
  const [turns, setTurns] = useState<ConversationTurn[]>([]);
  const [handoffNotice, setHandoffNotice] = useState<HandoffNotice | null>(null);
  const [ticketReason, setTicketReason] = useState("商品不符合预期");
  const [ticketType, setTicketType] = useState("return");
  const [error, setError] = useState<RequestError | null>(null);
  const [failedRequest, setFailedRequest] = useState<PendingRequest | null>(null);

  async function submitMessage(message: string, options: SubmitOptions = {}) {
    const trimmed = message.trim();
    if (!trimmed || loading) return;

    const shouldAppendUser = options.appendUser !== false;
    const userMessageId = options.messageId ?? crypto.randomUUID();
    const requestConversationId = options.conversationId ?? conversationId;
    const request: PendingRequest = {
      message: trimmed,
      conversationId: requestConversationId,
      messageId: userMessageId
    };

    setInput("");
    setError(null);
    setFailedRequest(null);
    setLoading(true);
    setResponseStatus("loading");
    setSuggestedActions([]);

    if (shouldAppendUser) {
      setMessages((current) => [
        ...current,
        {
          id: userMessageId,
          role: "user",
          content: trimmed,
          createdAt: new Date().toISOString(),
          status: "sent"
        }
      ]);
    } else {
      markMessageStatus(userMessageId, "sent");
    }

    try {
      const response = await sendChat(trimmed, requestConversationId);
      const receivedAt = new Date().toISOString();
      const assistantMessage: ChatMessage = {
        id: crypto.randomUUID(),
        role: "assistant",
        content: response.answer,
        createdAt: receivedAt,
        status: "received",
        boundary: response.boundary,
        intent: response.intent,
        evidenceCount: response.evidence.length,
        productCount: response.products.length,
        orderId: response.order?.id,
        suggestedActions: response.suggested_actions
      };

      setConversationId(response.conversation_id);
      setBoundary(response.boundary);
      setEvidence(response.boundary.classification === "out_of_scope" ? [] : response.evidence);
      setProducts(response.boundary.classification === "out_of_scope" ? [] : response.products);
      setOrder((current) => {
        if (response.boundary.classification === "out_of_scope") return null;
        return response.order ?? current;
      });
      setSuggestedActions(response.suggested_actions);
      setMessages((current) => [...current, assistantMessage]);
      setTurns((current) => [
        ...current,
        {
          id: crypto.randomUUID(),
          userMessage: trimmed,
          assistantAnswer: response.answer,
          intent: response.intent,
          boundary: response.boundary,
          evidenceCount: response.evidence.length,
          productCount: response.products.length,
          orderId: response.order?.id ?? order?.id,
          suggestedActions: response.suggested_actions,
          createdAt: receivedAt
        }
      ]);

      if (response.boundary.classification === "human_handoff_required") {
        setHandoffNotice({
          requested: false,
          source: "边界分类",
          reason: response.boundary.reason,
          orderId: response.order?.id ?? order?.id,
          updatedAt: receivedAt
        });
      } else {
        setHandoffNotice(null);
      }
      setResponseStatus(statusForBoundary(response.boundary));
    } catch (err) {
      const requestError = toRequestError(err, request);
      setError(requestError);
      setFailedRequest(request);
      setResponseStatus("error");
      setSuggestedActions([]);
      markMessageStatus(userMessageId, "failed");
    } finally {
      setLoading(false);
    }
  }

  function markMessageStatus(messageId: string, status: "sent" | "failed") {
    setMessages((current) =>
      current.map((message) => (message.id === messageId ? { ...message, status } : message))
    );
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void submitMessage(input);
  }

  function handleRetry() {
    if (!failedRequest) return;
    void submitMessage(failedRequest.message, {
      appendUser: false,
      conversationId: failedRequest.conversationId,
      messageId: failedRequest.messageId
    });
  }

  function handleSuggestedAction(action: SuggestedAction) {
    const message = typeof action.payload.message === "string" ? action.payload.message : null;
    if (message) {
      void submitMessage(message);
      return;
    }

    const orderId = numberFromPayload(action.payload.orderId) ?? order?.id;
    if (action.payload.handoff === true || orderId || action.label.includes("人工")) {
      setHandoffNotice({
        requested: true,
        source: action.label,
        reason: boundary?.reason ?? "需要人工确认",
        orderId,
        updatedAt: new Date().toISOString()
      });
      setResponseStatus("handoff");
      setSuggestedActions([]);
    }
  }

  function handleRequestHandoff() {
    const orderPart = order ? `，订单 ${order.id}` : "";
    void submitMessage(`我要申请${ticketTypeLabels[ticketType]}${orderPart}，原因：${ticketReason}`);
  }

  function handleAcknowledgeHandoff() {
    const now = new Date().toISOString();
    setHandoffNotice((current) => ({
      requested: true,
      source: current?.source ?? "人工接管",
      reason: current?.reason ?? boundary?.reason ?? "需要人工确认",
      orderId: current?.orderId ?? order?.id,
      updatedAt: now
    }));
    setResponseStatus("handoff");
  }

  return (
    <div className="shell">
      <Sidebar
        operator={operatorProfile}
        skuCount={products.length}
        orderCount={order ? 1 : 0}
        evidenceCount={evidence.length}
        quickPrompts={quickPrompts}
        disabled={loading}
        onPrompt={(prompt) => void submitMessage(prompt)}
      />

      <ChatPanel
        conversationId={conversationId}
        messages={messages}
        input={input}
        loading={loading}
        responseStatus={responseStatus}
        boundary={boundary}
        suggestedActions={suggestedActions}
        error={error}
        onInputChange={setInput}
        onSubmit={handleSubmit}
        onRetry={handleRetry}
        onSuggestedAction={handleSuggestedAction}
      />

      <ContextPanel
        boundary={boundary}
        evidence={evidence}
        products={products}
        order={order}
        turns={turns}
        handoffNotice={handoffNotice}
        ticketType={ticketType}
        ticketReason={ticketReason}
        loading={loading}
        onTicketTypeChange={setTicketType}
        onTicketReasonChange={setTicketReason}
        onRequestHandoff={handleRequestHandoff}
        onAcknowledgeHandoff={handleAcknowledgeHandoff}
      />
    </div>
  );
}

function statusForBoundary(boundary: BoundaryClassification): ResponseStatus {
  if (boundary.classification === "human_handoff_required") return "handoff";
  if (boundary.classification === "out_of_scope") return "blocked";
  return "success";
}

function toRequestError(error: unknown, request: PendingRequest): RequestError {
  if (error instanceof ApiError) {
    return {
      message: error.message,
      retryable: error.retryable,
      status: error.status,
      request
    };
  }
  if (error instanceof Error) {
    return {
      message: error.message,
      retryable: true,
      request
    };
  }
  return {
    message: "请求失败，请稍后重试。",
    retryable: true,
    request
  };
}

function numberFromPayload(value: unknown) {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string") {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return undefined;
}
