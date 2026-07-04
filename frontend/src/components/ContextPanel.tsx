import {
  BookOpenText,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  ClipboardList,
  Headset,
  History,
  PackageSearch,
  RotateCcw,
  ShieldCheck,
  Truck,
  Boxes
} from "lucide-react";
import { useState } from "react";
import { BoundaryBadge, BoundaryStatusCard } from "./Boundary";
import { EmptyState, displayValue, formatDateTime } from "./common";
import type {
  BoundaryClassification,
  ConversationTurn,
  EvidenceItem,
  HandoffNotice,
  OrderCard,
  ProductCard
} from "../types";

type ContextPanelProps = {
  boundary: BoundaryClassification | null;
  evidence: EvidenceItem[];
  products: ProductCard[];
  order: OrderCard | null;
  turns: ConversationTurn[];
  handoffNotice: HandoffNotice | null;
  ticketType: string;
  ticketReason: string;
  loading: boolean;
  skuCount: number;
  orderCount: number;
  evidenceCount: number;
  highlightedProductId?: number | null;
  onTicketTypeChange: (value: string) => void;
  onTicketReasonChange: (value: string) => void;
  onRequestHandoff: () => void;
  onAcknowledgeHandoff: () => void;
};

export function ContextPanel({
  boundary,
  evidence,
  products,
  order,
  turns,
  handoffNotice,
  ticketType,
  ticketReason,
  loading,
  skuCount,
  orderCount,
  evidenceCount,
  highlightedProductId,
  onTicketTypeChange,
  onTicketReasonChange,
  onRequestHandoff,
  onAcknowledgeHandoff
}: ContextPanelProps) {
  const showAfterSales =
    boundary?.classification === "human_handoff_required" || handoffNotice !== null;

  return (
    <aside className="context-panel">
      <div className="metrics-strip">
        <Metric icon={<Boxes size={14} />} label="SKU" value={skuCount} />
        <Metric icon={<Truck size={14} />} label="订单" value={orderCount} />
        <Metric icon={<BookOpenText size={14} />} label="依据" value={evidenceCount} />
      </div>

      <section className="panel-section">
        <div className="section-title">
          <ShieldCheck size={14} />
          <h2>边界</h2>
        </div>
        {boundary ? (
          <BoundaryStatusCard boundary={boundary} />
        ) : (
          <EmptyState text="等待请求" />
        )}
      </section>

      {(boundary?.classification === "human_handoff_required" || handoffNotice) && (
        <section className="panel-section">
          <div className="section-title">
            <Headset size={14} />
            <h2>接管</h2>
          </div>
          <HandoffPanel
            boundary={boundary}
            notice={handoffNotice}
            order={order}
            onAcknowledge={onAcknowledgeHandoff}
          />
        </section>
      )}

      {turns.length > 0 && (
        <section className="panel-section">
          <div className="section-title">
            <History size={14} />
            <h2>上下文</h2>
          </div>
          <ConversationTimeline turns={turns} />
        </section>
      )}

      {evidence.length > 0 && (
        <section className="panel-section">
          <div className="section-title">
            <BookOpenText size={14} />
            <h2>依据</h2>
          </div>
          <div className="evidence-list">
            {evidence.map((item) => (
              <EvidenceCard key={`${item.source_type}-${item.source_id}`} evidence={item} />
            ))}
          </div>
        </section>
      )}

      {products.length > 0 && (
        <section className="panel-section">
          <div className="section-title">
            <PackageSearch size={14} />
            <h2>商品</h2>
          </div>
          <div className="product-list">
            {products.map((product) => (
              <ProductCardView key={product.sku_id} product={product} highlighted={product.sku_id === highlightedProductId} />
            ))}
          </div>
        </section>
      )}

      {order && (
        <section className="panel-section">
          <div className="section-title">
            <Truck size={14} />
            <h2>订单</h2>
          </div>
          <OrderCardView order={order} />
        </section>
      )}

      {showAfterSales && (
        <section className="panel-section">
          <div className="section-title">
            <RotateCcw size={14} />
            <h2>售后</h2>
          </div>
          <div className="ticket-form">
            <select
              aria-label="售后类型"
              value={ticketType}
              onChange={(event) => onTicketTypeChange(event.target.value)}
              disabled={loading}
            >
              <option value="return">退货</option>
              <option value="exchange">换货</option>
              <option value="refund">退款</option>
              <option value="repair">维修</option>
            </select>
            <input
              aria-label="售后原因"
              value={ticketReason}
              onChange={(event) => onTicketReasonChange(event.target.value)}
              disabled={loading}
              placeholder="描述售后原因"
            />
            <button type="button" onClick={onRequestHandoff} disabled={loading || !ticketReason.trim()}>
              转人工处理
            </button>
          </div>
        </section>
      )}
    </aside>
  );
}

function Metric({
  icon,
  label,
  value
}: {
  icon: React.ReactNode;
  label: string;
  value: number;
}) {
  return (
    <div className="metric">
      {icon}
      <div>
        <strong>{value}</strong>
        <span>{label}</span>
      </div>
    </div>
  );
}

function HandoffPanel({
  boundary,
  notice,
  order,
  onAcknowledge
}: {
  boundary: BoundaryClassification | null;
  notice: HandoffNotice | null;
  order: OrderCard | null;
  onAcknowledge: () => void;
}) {
  const activeBoundary =
    boundary?.classification === "human_handoff_required" ? boundary : null;
  const requested = notice?.requested ?? false;
  const orderId = notice?.orderId ?? order?.id;

  return (
    <article className="handoff-card">
      <div className="handoff-card-head">
        <div>
          <Headset size={16} />
          <strong>人工接管</strong>
        </div>
        <span>{requested ? "已提醒" : "待确认"}</span>
      </div>
      <p>{activeBoundary?.display_message ?? notice?.reason}</p>
      <ul className="handoff-list">
        <li>{orderId ? `订单 #${orderId}` : "订单待确认"}</li>
        <li>{notice?.source ?? "chat"}</li>
        <li>{notice ? formatDateTime(notice.updatedAt) : "刚刚"}</li>
      </ul>
      <button type="button" onClick={onAcknowledge} disabled={requested}>
        <CheckCircle2 size={15} />
        {requested ? "已标记" : "标记已提醒"}
      </button>
    </article>
  );
}

function ConversationTimeline({ turns }: { turns: ConversationTurn[] }) {
  const recentTurns = turns.slice(-5).reverse();

  return (
    <div className="turn-list">
      {recentTurns.map((turn, index) => (
        <article className="turn-card" key={turn.id}>
          <header>
            <strong>#{turns.length - index}</strong>
            <BoundaryBadge boundary={turn.boundary} compact />
          </header>
          <p>{turn.userMessage}</p>
          <div className="turn-facts">
            <span>{turn.intent}</span>
            <span>{turn.evidenceCount} 依据</span>
            <span>{turn.productCount} 商品</span>
            {turn.orderId && <span>订单 #{turn.orderId}</span>}
          </div>
        </article>
      ))}
    </div>
  );
}

function EvidenceCard({ evidence }: { evidence: EvidenceItem }) {
  const metadata = Object.entries(evidence.metadata).slice(0, 3);
  return (
    <article className="evidence-card">
      <div className="evidence-card-head">
        <strong>{evidence.title}</strong>
        <span>{evidence.document_type}</span>
      </div>
      <p>{evidence.snippet}</p>
      {metadata.length > 0 && (
        <div className="metadata-list">
          {metadata.map(([key, value]) => (
            <span key={key}>
              {key}: {displayValue(value)}
            </span>
          ))}
        </div>
      )}
      <small>
        #{evidence.source_id}
        {typeof evidence.score === "number" ? ` · score ${evidence.score.toFixed(2)}` : ""}
      </small>
    </article>
  );
}

function ProductCardView({ product, highlighted }: { product: ProductCard; highlighted: boolean }) {
  const specLine = Object.entries(product.specs)
    .slice(0, 4)
    .map(([key, value]) => `${key}: ${value}`)
    .join(" · ");
  return (
    <article className={`product-card${highlighted ? " highlighted" : ""}`}>
      <div className="thumb">
        <PackageSearch size={20} />
      </div>
      <div>
        <h3>{product.title}</h3>
        <p>
          {product.brand} · {product.category}
        </p>
        <small>{specLine || "规格未标注"}</small>
      </div>
      <div className="product-foot">
        <strong>¥{product.price}</strong>
        <span>库存 {product.stock}</span>
      </div>
    </article>
  );
}

function OrderCardView({ order }: { order: OrderCard }) {
  const [expanded, setExpanded] = useState(false);
  const activeOrderItem = order.items[0];
  const lastTrace = order.logistics?.trace.at(-1);
  return (
    <article className="order-box">
      <button
        type="button"
        className="order-summary-button"
        onClick={() => setExpanded((current) => !current)}
      >
        <span className="order-head">
          <strong>#{order.id}</strong>
          <span>{order.status_label}</span>
        </span>
        {expanded ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
      </button>
      <p>{activeOrderItem?.sku_name ?? "订单商品待确认"}</p>
      <dl className="order-facts">
        <div>
          <dt>金额</dt>
          <dd>¥{order.pay_amount}</dd>
        </div>
        <div>
          <dt>下单</dt>
          <dd>{formatDateTime(order.created_at)}</dd>
        </div>
      </dl>
      <div className="order-logistics">
        <ClipboardList size={15} />
        <span>
          {order.logistics?.express_company ?? "待分配快递"} {order.logistics?.logistic_no ?? ""}
        </span>
      </div>
      {lastTrace && <small>{Object.values(lastTrace).slice(0, 2).join(" · ")}</small>}
      {expanded && (
        <div className="order-detail">
          <div className="order-detail-section">
            <strong>商品明细</strong>
            <div className="order-item-list">
              {order.items.map((item) => (
                <div className="order-item-row" key={item.id}>
                  <span>{item.sku_name}</span>
                  <small>{formatSkuSpecs(item.sku_specs)}</small>
                  <b>
                    ¥{item.price} × {item.quantity}
                  </b>
                </div>
              ))}
            </div>
          </div>
          <div className="order-detail-section">
            <strong>物流轨迹</strong>
            <div className="trace-list">
              {order.logistics?.trace.length ? (
                order.logistics.trace.map((trace, index) => (
                  <div className="trace-row" key={`${order.id}-trace-${index}`}>
                    {Object.values(trace)
                      .slice(0, 3)
                      .map((value) => (
                        <span key={value}>{value}</span>
                      ))}
                  </div>
                ))
              ) : (
                <span className="trace-empty">暂无轨迹</span>
              )}
            </div>
          </div>
        </div>
      )}
    </article>
  );
}

function formatSkuSpecs(specs: Record<string, unknown> | null | undefined) {
  if (!specs || !Object.keys(specs).length) return "规格未标注";
  return Object.entries(specs)
    .slice(0, 3)
    .map(([key, value]) => `${key}: ${displayValue(value)}`)
    .join(" · ");
}