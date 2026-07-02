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

export type ChatResponse = {
  conversation_id: number;
  answer: string;
  intent: string;
  products: ProductCard[];
  order?: OrderCard | null;
  suggested_actions: SuggestedAction[];
};

export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
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
