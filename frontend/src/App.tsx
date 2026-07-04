import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  clearAuthSession,
  deleteConversation as deleteConversationApi,
  getConversation,
  listConversations,
  login,
  logout,
  restoreSession,
  sendChatStream
} from "./api";
import { ChatPanel } from "./components/ChatPanel";
import { ContextPanel } from "./components/ContextPanel";
import { LoginPage } from "./components/LoginPage";
import { Sidebar } from "./components/Sidebar";
import type {
  AuthSession,
  BoundaryClassification,
  ChatStreamEvent,
  ChatMessage,
  ConversationDetail,
  ConversationSummary,
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

const ticketTypeLabels: Record<string, string> = {
  return: "退货",
  exchange: "换货",
  refund: "退款",
  repair: "维修"
};

type SubmitOptions = {
  appendUser?: boolean;
  conversationId?: number;
  messageId?: string;
};

export default function App() {
  const streamAbortRef = useRef<AbortController | null>(null);
  const [authSession, setAuthSession] = useState<AuthSession | null>(null);
  const [authStatus, setAuthStatus] = useState<"restoring" | "ready" | "submitting">(
    "restoring"
  );
  const [authError, setAuthError] = useState<string | null>(null);
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [conversationsLoading, setConversationsLoading] = useState(false);
  const [conversationId, setConversationId] = useState<number | undefined>();
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [responseStatus, setResponseStatus] = useState<ResponseStatus>("ready");
  const [messages, setMessages] = useState<ChatMessage[]>([
    initialAssistantMessage()
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
  const [highlightedProductId, setHighlightedProductId] = useState<number | null>(null);

  const handleProductClick = useCallback((product: ProductCard) => {
    setHighlightedProductId(product.sku_id);
    setTimeout(() => setHighlightedProductId(null), 2000);
  }, []);

  useEffect(() => {
    let cancelled = false;
    restoreSession()
      .then((session) => {
        if (cancelled) return;
        setAuthSession(session);
        setAuthError(null);
      })
      .catch((err) => {
        if (cancelled) return;
        setAuthSession(null);
        setAuthError(err instanceof Error ? err.message : "会话恢复失败，请重新登录。");
      })
      .finally(() => {
        if (!cancelled) setAuthStatus("ready");
      });

    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!authSession) return;
    void refreshConversations();
  }, [authSession?.user.id]);

  async function handleLogin(loginIdentifier: string, password: string) {
    setAuthStatus("submitting");
    setAuthError(null);
    try {
      const session = await login(loginIdentifier, password);
      setAuthSession(session);
      resetWorkspace();
    } catch (err) {
      setAuthSession(null);
      setAuthError(err instanceof Error ? err.message : "登录失败，请稍后重试。");
    } finally {
      setAuthStatus("ready");
    }
  }

  async function handleLogout() {
    await logout();
    setAuthSession(null);
    setAuthError(null);
    resetWorkspace();
  }

  function handleAuthExpired() {
    clearAuthSession();
    setAuthSession(null);
    setAuthError("登录已过期，请重新登录。");
    resetWorkspace();
  }

  function resetWorkspace() {
    streamAbortRef.current?.abort();
    streamAbortRef.current = null;
    setConversations([]);
    setConversationsLoading(false);
    resetActiveConversation();
  }

  function resetActiveConversation() {
    setConversationId(undefined);
    setInput("");
    setLoading(false);
    setResponseStatus("ready");
    setMessages([initialAssistantMessage()]);
    setProducts([]);
    setOrder(null);
    setBoundary(null);
    setEvidence([]);
    setSuggestedActions([]);
    setTurns([]);
    setHandoffNotice(null);
    setError(null);
    setFailedRequest(null);
  }

  async function refreshConversations() {
    setConversationsLoading(true);
    try {
      setConversations(await listConversations());
    } catch (err) {
      const requestError = toRequestError(err, { message: "load conversations" });
      if (requestError.status === 401 || requestError.status === 403) {
        handleAuthExpired();
      }
    } finally {
      setConversationsLoading(false);
    }
  }

  function handleNewConversation() {
    if (loading) return;
    resetActiveConversation();
  }

  async function handleDeleteConversation(targetId: number) {
    try {
      await deleteConversationApi(targetId);
      if (targetId === conversationId) {
        resetActiveConversation();
      }
      await refreshConversations();
    } catch (err) {
      const requestError = toRequestError(err, { message: "delete conversation" });
      if (requestError.status === 401 || requestError.status === 403) {
        handleAuthExpired();
      }
    }
  }

  async function handleSelectConversation(nextConversationId: number) {
    if (loading || nextConversationId === conversationId) return;
    setError(null);
    setFailedRequest(null);
    setConversationsLoading(true);
    try {
      const detail = await getConversation(nextConversationId);
      applyConversationDetail(detail);
    } catch (err) {
      const requestError = toRequestError(err, { message: "load conversation" });
      if (requestError.status === 401 || requestError.status === 403) {
        handleAuthExpired();
        return;
      }
      setError({
        ...requestError,
        message: "会话加载失败，请稍后重试。"
      });
      setResponseStatus("error");
    } finally {
      setConversationsLoading(false);
    }
  }

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

    const assistantMessageId = crypto.randomUUID();
    setMessages((current) => [
      ...current,
      {
        id: assistantMessageId,
        role: "assistant",
        content: "",
        createdAt: new Date().toISOString(),
        status: "streaming",
        streamStage: "正在判断边界"
      }
    ]);

    const abortController = new AbortController();
    streamAbortRef.current = abortController;

    try {
      await sendChatStream(trimmed, requestConversationId, {
        signal: abortController.signal,
        timeoutMs: 60000,
        onEvent: (event) => handleStreamEvent(event, assistantMessageId, trimmed)
      });
      await refreshConversations();
    } catch (err) {
      const requestError = toRequestError(err, request);
      if (requestError.status === 401 || requestError.status === 403) {
        handleAuthExpired();
        return;
      }
      if (requestError.status === 499) {
        setResponseStatus("cancelled");
        setSuggestedActions([]);
        updateMessage(assistantMessageId, (message) => ({
          ...message,
          status: "cancelled",
          streamStage: "已取消"
        }));
        return;
      }
      setError(requestError);
      setFailedRequest(request);
      setResponseStatus("error");
      setSuggestedActions([]);
      markMessageStatus(userMessageId, "failed");
      updateMessage(assistantMessageId, (message) => ({
        ...message,
        status: "failed",
        content: message.content || requestError.message,
        streamStage: undefined
      }));
    } finally {
      setLoading(false);
      if (streamAbortRef.current === abortController) {
        streamAbortRef.current = null;
      }
    }
  }

  function handleStreamEvent(
    event: ChatStreamEvent,
    assistantMessageId: string,
    userMessage: string
  ) {
    if ("conversation_id" in event && typeof event.conversation_id === "number") {
      setConversationId(event.conversation_id);
    }

    if (event.type === "run_started") {
      updateMessage(assistantMessageId, (message) => ({
        ...message,
        streamStage: "正在判断边界"
      }));
      return;
    }

    if (event.type === "boundary") {
      setBoundary(event.boundary);
      if (event.boundary.classification === "out_of_scope") {
        setProducts([]);
        setOrder(null);
        setEvidence([]);
      }
      updateMessage(assistantMessageId, (message) => ({
        ...message,
        boundary: event.boundary,
        streamStage:
          event.boundary.classification === "in_scope_auto" ? "边界通过，正在检索" : "正在生成说明"
      }));
      return;
    }

    if (event.type === "tool_call") {
      updateMessage(assistantMessageId, (message) => ({
        ...message,
        streamStage: toolCallStage(event.tool_name, event.status)
      }));
      return;
    }

    if (event.type === "context") {
      if (event.boundary) {
        setBoundary(event.boundary);
      }
      setEvidence(event.boundary?.classification === "out_of_scope" ? [] : event.evidence);
      setProducts(event.boundary?.classification === "out_of_scope" ? [] : event.products);
      setOrder((current) => {
        if (event.boundary?.classification === "out_of_scope") return null;
        return event.order ?? current;
      });
      updateMessage(assistantMessageId, (message) => ({
        ...message,
        intent: event.intent ?? message.intent,
        evidenceCount: event.evidence.length,
        productCount: event.products.length,
        orderId: event.order?.id ?? message.orderId,
        products: event.boundary?.classification === "out_of_scope" ? [] : event.products,
        streamStage: "上下文已更新"
      }));
      return;
    }

    if (event.type === "delta") {
      setResponseStatus("streaming");
      updateMessage(assistantMessageId, (message) => ({
        ...message,
        content: `${message.content}${event.delta}`,
        streamStage: "正在生成回答"
      }));
      return;
    }

    if (event.type === "done") {
      applyFinalResponse(event.response, assistantMessageId, userMessage);
      return;
    }

    if (event.type === "error") {
      updateMessage(assistantMessageId, (message) => ({
        ...message,
        status: "failed",
        content: message.content || event.message,
        streamStage: undefined
      }));
    }
  }

  function applyFinalResponse(
    response: {
      conversation_id: number;
      answer: string;
      intent: string;
      boundary: BoundaryClassification;
      evidence: EvidenceItem[];
      products: ProductCard[];
      order?: OrderCard | null;
      suggested_actions: SuggestedAction[];
    },
    assistantMessageId: string,
    userMessage: string
  ) {
    const receivedAt = new Date().toISOString();
    const orderId = response.order?.id ?? order?.id;
    setConversationId(response.conversation_id);
    setBoundary(response.boundary);
    setEvidence(response.boundary.classification === "out_of_scope" ? [] : response.evidence);
    setProducts(response.boundary.classification === "out_of_scope" ? [] : response.products);
    setOrder((current) => {
      if (response.boundary.classification === "out_of_scope") return null;
      return response.order ?? current;
    });
    setSuggestedActions(response.suggested_actions);
    updateMessage(assistantMessageId, (message) => ({
      ...message,
      content: response.answer,
      createdAt: receivedAt,
      status: "received",
      streamStage: undefined,
      boundary: response.boundary,
      intent: response.intent,
      evidenceCount: response.evidence.length,
      productCount: response.products.length,
      orderId,
      suggestedActions: response.suggested_actions,
      products: response.boundary.classification === "out_of_scope" ? [] : response.products
    }));
    setTurns((current) => [
      ...current,
      {
        id: crypto.randomUUID(),
        userMessage,
        assistantAnswer: response.answer,
        intent: response.intent,
        boundary: response.boundary,
        evidenceCount: response.evidence.length,
        productCount: response.products.length,
        orderId,
        suggestedActions: response.suggested_actions,
        createdAt: receivedAt
      }
    ]);

    if (response.boundary.classification === "human_handoff_required") {
      setHandoffNotice({
        requested: false,
        source: "边界分类",
        reason: response.boundary.reason,
        orderId,
        updatedAt: receivedAt
      });
    } else {
      setHandoffNotice(null);
    }
    setResponseStatus(statusForBoundary(response.boundary));
  }

  function updateMessage(messageId: string, updater: (message: ChatMessage) => ChatMessage) {
    setMessages((current) =>
      current.map((message) => (message.id === messageId ? updater(message) : message))
    );
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

  function handleCancelStream() {
    streamAbortRef.current?.abort();
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

  function applyConversationDetail(detail: ConversationDetail) {
    const restoredMessages = detail.messages.map(messageFromHistory);
    const lastAssistant = [...detail.messages]
      .reverse()
      .find((message) => message.role === "assistant" && message.metadata);
    const metadata = lastAssistant?.metadata ?? {};
    const restoredBoundary = boundaryFromMetadata(metadata);
    const restoredEvidence = listFromMetadata<EvidenceItem>(metadata.evidence);
    const restoredProducts = listFromMetadata<ProductCard>(metadata.products);
    const restoredOrder = orderFromMetadata(metadata.order);

    setConversationId(detail.id);
    setInput("");
    setMessages(restoredMessages.length ? restoredMessages : [initialAssistantMessage()]);
    setBoundary(restoredBoundary);
    setEvidence(restoredBoundary?.classification === "out_of_scope" ? [] : restoredEvidence);
    setProducts(restoredBoundary?.classification === "out_of_scope" ? [] : restoredProducts);
    setOrder(restoredBoundary?.classification === "out_of_scope" ? null : restoredOrder);
    setSuggestedActions([]);
    setTurns(turnsFromHistory(detail));
    setHandoffNotice(null);
    setResponseStatus(restoredBoundary ? statusForBoundary(restoredBoundary) : "ready");
  }

  if (authStatus === "restoring" || !authSession) {
    return (
      <LoginPage
        loading={authStatus === "restoring" || authStatus === "submitting"}
        error={authError}
        onLogin={handleLogin}
      />
    );
  }

  const operatorProfile: OperatorProfile = {
    name: authSession.user.display_name,
    role: "PC 外设专员",
    userId: authSession.user.id,
    loginIdentifier: authSession.user.login_identifier,
    authState: "authenticated",
    statusLabel: "已登录"
  };

  return (
    <div className="shell">
      <Sidebar
        operator={operatorProfile}
        conversations={conversations}
        activeConversationId={conversationId}
        conversationsLoading={conversationsLoading}
        disabled={loading}
        onLogout={() => void handleLogout()}
        onNewConversation={handleNewConversation}
        onSelectConversation={(id) => void handleSelectConversation(id)}
        onDeleteConversation={handleDeleteConversation}
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
        onCancel={handleCancelStream}
        onRetry={handleRetry}
        onSuggestedAction={handleSuggestedAction}
        onProductClick={handleProductClick}
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
        skuCount={products.length}
        orderCount={order ? 1 : 0}
        evidenceCount={evidence.length}
        highlightedProductId={highlightedProductId}
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

function initialAssistantMessage(): ChatMessage {
  return {
    id: "hello",
    role: "assistant",
    content: "今天想看哪类外设？",
    createdAt: new Date().toISOString(),
    status: "received"
  };
}

function messageFromHistory(message: ConversationDetail["messages"][number]): ChatMessage {
  const metadata = message.metadata ?? {};
  const boundary = boundaryFromMetadata(metadata);
  const evidence = listFromMetadata<EvidenceItem>(metadata.evidence);
  const products = listFromMetadata<ProductCard>(metadata.products);
  const order = orderFromMetadata(metadata.order);
  return {
    id: String(message.id),
    role: message.role,
    content: message.content,
    createdAt: message.created_at,
    status: "received",
    boundary: boundary ?? undefined,
    intent: typeof metadata.intent === "string" ? metadata.intent : undefined,
    evidenceCount: evidence.length || undefined,
    productCount: products.length || undefined,
    orderId: order?.id
  };
}

function turnsFromHistory(detail: ConversationDetail): ConversationTurn[] {
  const turns: ConversationTurn[] = [];
  for (let index = 0; index < detail.messages.length; index += 1) {
    const user = detail.messages[index];
    const assistant = detail.messages[index + 1];
    if (user?.role !== "user" || assistant?.role !== "assistant") continue;
    const metadata = assistant.metadata ?? {};
    const boundary = boundaryFromMetadata(metadata);
    if (!boundary) continue;
    const evidence = listFromMetadata<EvidenceItem>(metadata.evidence);
    const products = listFromMetadata<ProductCard>(metadata.products);
    const order = orderFromMetadata(metadata.order);
    turns.push({
      id: `${user.id}-${assistant.id}`,
      userMessage: user.content,
      assistantAnswer: assistant.content,
      intent: typeof metadata.intent === "string" ? metadata.intent : "unknown",
      boundary,
      evidenceCount: evidence.length,
      productCount: products.length,
      orderId: order?.id,
      suggestedActions: [],
      createdAt: assistant.created_at
    });
  }
  return turns;
}

function boundaryFromMetadata(metadata: Record<string, unknown>): BoundaryClassification | null {
  const boundary = metadata.boundary;
  if (!isRecord(boundary)) return null;
  const classification = boundary.classification;
  if (
    classification !== "in_scope_auto" &&
    classification !== "human_handoff_required" &&
    classification !== "out_of_scope"
  ) {
    return null;
  }
  return {
    classification,
    reason: typeof boundary.reason === "string" ? boundary.reason : "",
    display_message:
      typeof boundary.display_message === "string" ? boundary.display_message : ""
  };
}

function listFromMetadata<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : [];
}

function orderFromMetadata(value: unknown): OrderCard | null {
  return isRecord(value) && typeof value.id === "number" ? (value as OrderCard) : null;
}

function toolCallStage(toolName: string, status: "started" | "completed" | "error") {
  const verb = status === "started" ? "正在" : status === "completed" ? "已完成" : "检索失败";
  const label = toolName.includes("catalog")
    ? "检索商品"
    : toolName.includes("order")
      ? "查询订单"
      : toolName.includes("knowledge")
        ? "检索依据"
        : "识别意图";
  return `${verb}${label}`;
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

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
