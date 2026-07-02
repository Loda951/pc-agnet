import {
  Ban,
  Bot,
  Boxes,
  CheckCircle2,
  Headset,
  Loader2,
  PackageSearch,
  RotateCcw,
  Send,
  ShieldCheck,
  Sparkles,
  Truck,
  UserRound
} from "lucide-react";
import { FormEvent, useMemo, useState } from "react";
import { sendChat } from "./api";
import type { BoundaryClassification, ChatMessage, OrderCard, ProductCard } from "./types";

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

export default function App() {
  const [conversationId, setConversationId] = useState<number | undefined>();
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [messages, setMessages] = useState<ChatMessage[]>([
    { id: "hello", role: "assistant", content: "今天想看哪类外设？" }
  ]);
  const [products, setProducts] = useState<ProductCard[]>([]);
  const [order, setOrder] = useState<OrderCard | null>(null);
  const [boundary, setBoundary] = useState<BoundaryClassification | null>(null);
  const [ticketReason, setTicketReason] = useState("商品不符合预期");
  const [ticketType, setTicketType] = useState("return");
  const [error, setError] = useState<string | null>(null);

  async function submitMessage(message: string) {
    const trimmed = message.trim();
    if (!trimmed || loading) return;

    setInput("");
    setError(null);
    setLoading(true);
    setMessages((current) => [
      ...current,
      { id: crypto.randomUUID(), role: "user", content: trimmed }
    ]);

    try {
      const response = await sendChat(trimmed, conversationId);
      setConversationId(response.conversation_id);
      setBoundary(response.boundary);
      setMessages((current) => [
        ...current,
        {
          id: crypto.randomUUID(),
          role: "assistant",
          content: response.answer,
          boundary: response.boundary
        }
      ]);
      if (response.boundary.classification === "out_of_scope") {
        setProducts([]);
        setOrder(null);
      } else {
        if (response.products.length) setProducts(response.products);
        if (response.order) setOrder(response.order);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "请求失败");
    } finally {
      setLoading(false);
    }
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await submitMessage(input);
  }

  async function handleRequestHandoff() {
    const orderPart = order ? `，订单 ${order.id}` : "";
    await submitMessage(`我要申请${ticketTypeLabels[ticketType]}${orderPart}，原因：${ticketReason}`);
  }

  const activeOrderItem = useMemo(() => order?.items[0], [order]);

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand-row">
          <span className="brand-mark">
            <Sparkles size={18} />
          </span>
          <div>
            <strong>PC Agent</strong>
            <small>single user</small>
          </div>
        </div>

        <div className="metric-grid">
          <Metric icon={<Boxes size={18} />} label="SKU" value={products.length || 5} />
          <Metric icon={<Truck size={18} />} label="订单" value={order ? "1" : "0"} />
        </div>

        <div className="quick-list">
          {quickPrompts.map((prompt) => (
            <button key={prompt} type="button" onClick={() => submitMessage(prompt)}>
              {prompt}
            </button>
          ))}
        </div>
      </aside>

      <main className="chat-panel">
        <header className="topbar">
          <div>
            <h1>客服会话</h1>
            <span>{conversationId ? `#${conversationId}` : "ready"}</span>
          </div>
          <div className="status-stack">
            {boundary && <BoundaryBadge boundary={boundary} />}
            <span className={loading ? "status-pill busy" : "status-pill"}>
              {loading ? <Loader2 size={14} className="spin" /> : <CheckCircle2 size={14} />}
              {loading ? "thinking" : "online"}
            </span>
          </div>
        </header>

        <section className="messages" aria-live="polite">
          {messages.map((message) => (
            <article key={message.id} className={`message ${message.role}`}>
              <span className="avatar">
                {message.role === "assistant" ? <Bot size={17} /> : <UserRound size={17} />}
              </span>
              <div className="bubble-stack">
                {message.boundary && <BoundaryBadge boundary={message.boundary} compact />}
                <p>{message.content}</p>
              </div>
            </article>
          ))}
          {error && <div className="error-line">{error}</div>}
        </section>

        <form className="composer" onSubmit={handleSubmit}>
          <input
            value={input}
            onChange={(event) => setInput(event.target.value)}
            placeholder="输入预算、用途、订单号或售后诉求"
          />
          <button type="submit" disabled={loading || !input.trim()} title="发送">
            {loading ? <Loader2 size={18} className="spin" /> : <Send size={18} />}
          </button>
        </form>
      </main>

      <aside className="context-panel">
        <section className="panel-section">
          <div className="section-title">
            <ShieldCheck size={18} />
            <h2>边界</h2>
          </div>
          {boundary ? <BoundaryStatusCard boundary={boundary} /> : <EmptyState text="等待请求" />}
        </section>

        <section className="panel-section">
          <div className="section-title">
            <PackageSearch size={18} />
            <h2>商品</h2>
          </div>
          <div className="product-list">
            {(products.length ? products : []).map((product) => (
              <ProductCardView key={product.sku_id} product={product} />
            ))}
            {!products.length && <EmptyState text="暂无检索结果" />}
          </div>
        </section>

        <section className="panel-section">
          <div className="section-title">
            <Truck size={18} />
            <h2>订单</h2>
          </div>
          {order ? (
            <div className="order-box">
              <div className="order-head">
                <strong>#{order.id}</strong>
                <span>{order.status_label}</span>
              </div>
              <p>{activeOrderItem?.sku_name}</p>
              <small>{order.logistics?.express_company ?? "待分配快递"} {order.logistics?.logistic_no ?? ""}</small>
            </div>
          ) : (
            <EmptyState text="暂无订单上下文" />
          )}
        </section>

        <section className="panel-section">
          <div className="section-title">
            <RotateCcw size={18} />
            <h2>售后</h2>
          </div>
          <div className="ticket-form">
            <select value={ticketType} onChange={(event) => setTicketType(event.target.value)}>
              <option value="return">退货</option>
              <option value="exchange">换货</option>
              <option value="refund">退款</option>
              <option value="repair">维修</option>
            </select>
            <input
              value={ticketReason}
              onChange={(event) => setTicketReason(event.target.value)}
            />
            <button type="button" onClick={handleRequestHandoff} disabled={loading || !ticketReason.trim()}>
              转人工处理
            </button>
          </div>
          <div className="handoff-note">
            <strong>{order ? `#${order.id}` : "待补订单"}</strong>
            <span>人工确认</span>
          </div>
        </section>
      </aside>
    </div>
  );
}

function Metric({ icon, label, value }: { icon: React.ReactNode; label: string; value: string | number }) {
  return (
    <div className="metric">
      {icon}
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function BoundaryBadge({
  boundary,
  compact = false
}: {
  boundary: BoundaryClassification;
  compact?: boolean;
}) {
  const meta = boundaryMeta(boundary);
  return (
    <span className={`boundary-badge ${meta.className} ${compact ? "compact" : ""}`}>
      {meta.icon}
      {meta.label}
    </span>
  );
}

function BoundaryStatusCard({ boundary }: { boundary: BoundaryClassification }) {
  const meta = boundaryMeta(boundary);
  return (
    <div className={`boundary-card ${meta.className}`}>
      <div className="boundary-card-head">
        {meta.icon}
        <strong>{meta.label}</strong>
      </div>
      <p>{boundary.reason}</p>
    </div>
  );
}

function boundaryMeta(boundary: BoundaryClassification) {
  if (boundary.classification === "human_handoff_required") {
    return {
      className: "handoff",
      icon: <Headset size={14} />,
      label: "人工接管"
    };
  }
  if (boundary.classification === "out_of_scope") {
    return {
      className: "blocked",
      icon: <Ban size={14} />,
      label: "拒答"
    };
  }
  return {
    className: "auto",
    icon: <ShieldCheck size={14} />,
    label: "自动回答"
  };
}

function ProductCardView({ product }: { product: ProductCard }) {
  const specLine = Object.entries(product.specs)
    .slice(0, 3)
    .map(([key, value]) => `${key}: ${value}`)
    .join(" · ");
  return (
    <article className="product-card">
      <div className="thumb">
        <PackageSearch size={22} />
      </div>
      <div>
        <h3>{product.title}</h3>
        <p>{product.brand} · {product.category}</p>
        <small>{specLine || "规格未标注"}</small>
      </div>
      <div className="product-foot">
        <strong>¥{product.price}</strong>
        <span>库存 {product.stock}</span>
      </div>
    </article>
  );
}

function EmptyState({ text }: { text: string }) {
  return <div className="empty-state">{text}</div>;
}
