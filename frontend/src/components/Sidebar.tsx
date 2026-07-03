import { BookOpenText, Boxes, Sparkles, Truck, UserRound } from "lucide-react";
import type { ReactNode } from "react";
import type { OperatorProfile } from "../types";

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
  quickPrompts: string[];
  disabled: boolean;
  onPrompt: (prompt: string) => void;
};

export function Sidebar({
  operator,
  skuCount,
  orderCount,
  evidenceCount,
  quickPrompts,
  disabled,
  onPrompt
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
            <dt>权限</dt>
            <dd>{operator.authState === "placeholder" ? "占位" : operator.authState}</dd>
          </div>
        </dl>
      </section>

      <div className="metric-grid">
        {metrics.map((metric) => (
          <Metric key={metric.label} {...metric} />
        ))}
      </div>

      <section className="quick-section">
        <h2>快捷请求</h2>
        <div className="quick-list">
          {quickPrompts.map((prompt) => (
            <button key={prompt} type="button" disabled={disabled} onClick={() => onPrompt(prompt)}>
              {prompt}
            </button>
          ))}
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
