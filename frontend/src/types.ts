export type ProductCard = {
  spu_id: number;
  sku_id: number;
  title: string;
  brand: string;
  category: string;
  price: string;
  stock: number;
  specs: Record<string, string>;
  image_url?: string | null;
};

export type OrderItemCard = {
  id: number;
  sku_id: number;
  sku_name: string;
  sku_specs?: Record<string, unknown> | null;
  price: string;
  quantity: number;
};

export type OrderCard = {
  id: number;
  status: number;
  status_label: string;
  pay_amount: string;
  created_at: string;
  items: OrderItemCard[];
  logistics?: {
    express_company?: string | null;
    logistic_no?: string | null;
    status: number;
    trace: Array<Record<string, string>>;
  } | null;
};

export type SuggestedAction = {
  label: string;
  payload: Record<string, unknown>;
};

export type BoundaryClassificationValue =
  | "in_scope_auto"
  | "human_handoff_required"
  | "out_of_scope";

export type BoundaryClassification = {
  classification: BoundaryClassificationValue;
  reason: string;
  display_message: string;
};

export type EvidenceItem = {
  source_type: "knowledge_document";
  source_id: number;
  title: string;
  document_type: string;
  snippet: string;
  score?: number | null;
  metadata: Record<string, unknown>;
};

export type ChatResponse = {
  conversation_id: number;
  answer: string;
  intent: string;
  boundary: BoundaryClassification;
  evidence: EvidenceItem[];
  products: ProductCard[];
  order?: OrderCard | null;
  suggested_actions: SuggestedAction[];
};

export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  createdAt: string;
  status?: "sent" | "failed" | "received";
  boundary?: BoundaryClassification;
  intent?: string;
  evidenceCount?: number;
  productCount?: number;
  orderId?: number;
  suggestedActions?: SuggestedAction[];
};

export type AfterSalesTicket = {
  id: number;
  order_id: number;
  order_item_id: number;
  ticket_type: string;
  reason: string;
  status: string;
  created_at: string;
};

export type ResponseStatus = "ready" | "loading" | "success" | "handoff" | "blocked" | "error";

export type OperatorProfile = {
  name: string;
  role: string;
  userId: number;
  authState: "placeholder";
  statusLabel: string;
};

export type PendingRequest = {
  message: string;
  conversationId?: number;
  messageId?: string;
};

export type RequestError = {
  message: string;
  retryable: boolean;
  status?: number;
  request?: PendingRequest;
};

export type ConversationTurn = {
  id: string;
  userMessage: string;
  assistantAnswer: string;
  intent: string;
  boundary: BoundaryClassification;
  evidenceCount: number;
  productCount: number;
  orderId?: number;
  suggestedActions: SuggestedAction[];
  createdAt: string;
};

export type HandoffNotice = {
  requested: boolean;
  source: string;
  reason: string;
  orderId?: number;
  updatedAt: string;
};
