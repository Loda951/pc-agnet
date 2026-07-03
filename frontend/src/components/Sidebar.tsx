import {
  BookOpenText,
  Boxes,
  Loader2,
  LogOut,
  MessageSquarePlus,
  MessagesSquare,
  Sparkles,
  Truck,
  UserRound
} from "lucide-react";
import type { ReactNode } from "react";
import { formatClock } from "./common";
import type { ConversationSummary, OperatorProfile } from "../types";

type SidebarMetric = {
  icon: ReactNode;
  label: string;
  value: string | number;
};

type SidebarProps = {
  operator: OperatorProfile;
  skuCount: number;
  orderCount: number;
  evidenceCount: number;
  conversations: ConversationSummary[];
  activeConversationId?: number;
  conversationsLoading: boolean;
  disabled: boolean;
  onLogout: () => void;
  onNewConversation: () => void;
  onSelectConversation: (conversationId: number) => void;
};

export function Sidebar({
  operator,
  skuCount,
  orderCount,
  evidenceCount,
  conversations,
  activeConversationId,
  conversationsLoading,
  disabled,
  onLogout,
  onNewConversation,
  onSelectConversation
}: SidebarProps) {
  const metrics: SidebarMetric[] = [
    { icon: <Boxes size={18} />, label: "SKU", value: skuCount },
    { icon: <Truck size={18} />, label: "订单", value: orderCount },
    { icon: <BookOpenText size={18} />, label: "依据", value: evidenceCount }
  ];

  return (
    <aside className="sidebar">
      <div className="brand-row">
        <span className="brand-mark">
          <Sparkles size={18} />
        </span>
        <div>
          <strong>PC Agent</strong>
          <small>客服工作台</small>
        </div>
      </div>

      <section className="operator-card">
        <div className="operator-profile">
          <span className="operator-avatar">
            <UserRound size={18} />
          </span>
          <div className="operator-copy">
            <strong>{operator.name}</strong>
            <span>{operator.role}</span>
          </div>
          <span className="auth-chip">{operator.statusLabel}</span>
        </div>
        <dl className="operator-facts">
          <div>
            <dt>User ID</dt>
            <dd>#{operator.userId}</dd>
          </div>
          <div>
            <dt>登录</dt>
            <dd>{operator.loginIdentifier}</dd>
          </div>
        </dl>
        <button type="button" className="logout-button" onClick={onLogout} disabled={disabled}>
          <LogOut size={15} />
          退出登录
        </button>
      </section>

      <div className="metric-grid">
        {metrics.map((metric) => (
          <Metric key={metric.label} {...metric} />
        ))}
      </div>

      <section className="conversation-section">
        <div className="conversation-section-head">
          <h2>会话</h2>
          <button type="button" onClick={onNewConversation} disabled={disabled} title="新建会话">
            <MessageSquarePlus size={16} />
          </button>
        </div>
        <div className="conversation-list">
          {conversations.map((conversation) => (
            <button
              key={conversation.id}
              type="button"
              className={conversation.id === activeConversationId ? "active" : ""}
              disabled={disabled}
              onClick={() => onSelectConversation(conversation.id)}
            >
              <MessagesSquare size={16} />
              <span>
                <strong>{conversation.title}</strong>
                <small>
                  {conversation.last_message ?? "暂无消息"}
                  {conversation.last_message_at ? ` · ${formatClock(conversation.last_message_at)}` : ""}
                </small>
              </span>
            </button>
          ))}
          {conversationsLoading && (
            <div className="conversation-loading">
              <Loader2 size={15} className="spin" />
              <span>加载中</span>
            </div>
          )}
          {!conversations.length && !conversationsLoading && (
            <div className="conversation-empty">暂无历史会话</div>
          )}
        </div>
      </section>
    </aside>
  );
}

function Metric({ icon, label, value }: SidebarMetric) {
  return (
    <div className="metric">
      {icon}
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}
