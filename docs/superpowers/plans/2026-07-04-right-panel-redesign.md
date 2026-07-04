# Right Panel Information Architecture & Visual Hierarchy Redesign — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Redesign the right-side ContextPanel from a flat information dump into a three-tier priority layout (Status → Primary → Details) with clear visual hierarchy, without changing colors—only reusing existing PCB design system variables differently.

**Architecture:** Reorder ContextPanel sections from flat listing to priority-based (L1: metrics+boundary → L2: products+order → L3: evidence+context+handoff). Replace BoundaryStatusCard with a compact BoundaryStatusBar. Add visual depth by darkening panel background to `--bg-base`, adding section backgrounds and dividers, and accenting product cards with a copper left border.

**Tech Stack:** React 19, TypeScript, CSS custom properties (existing PCB design system)

## Global Constraints

- React components use PascalCase, variables/hooks use camelCase
- Use existing CSS custom properties (`--copper`, `--bg-card`, `--text-primary`, etc.) — no new color values
- All new CSS goes into `frontend/src/styles.css`
- Commit style: `feat:`, `fix:`, `to:` prefix
- No backend changes needed
- Mobile drawer styles must stay consistent with the new section order

---

## File Structure

### Modified Files
| File | Changes |
|------|---------|
| `frontend/src/components/ContextPanel.tsx` | Reorder sections (L1→L2→L3); replace `BoundaryStatusCard` usage with new `BoundaryStatusBar`; update section ordering |
| `frontend/src/components/Boundary.tsx` | Add `BoundaryStatusBar` component (compact status bar with left border accent) |
| `frontend/src/styles.css` | Panel background to `--bg-base`; `.panel-section` background/divider; product card left border; section title icon background; `.boundary-status-bar` styles; remove `.boundary-card` styles |

---

### Task 1: Add BoundaryStatusBar component

**Files:**
- Modify: `frontend/src/components/Boundary.tsx`
- Modify: `frontend/src/styles.css`

**Interfaces:**
- Consumes: `BoundaryClassification` type from `../types`, `ShieldCheck`/`Headset`/`CircleSlash` icons from `lucide-react` (already imported in Boundary.tsx)
- Produces: `<BoundaryStatusBar>` React component exported from `Boundary.tsx`

The existing `BoundaryStatusCard` renders a full card with icon, title, message, and small text. The new `BoundaryStatusBar` is a compact inline status bar with icon, classification label, and display message on one line.

- [ ] **Step 1: Add BoundaryStatusBar component to Boundary.tsx**

In `frontend/src/components/Boundary.tsx`, add a new exported component after the existing `BoundaryBadge` and `BoundaryStatusCard`:

```tsx
export function BoundaryStatusBar({ boundary }: { boundary: BoundaryClassification }) {
  const config: Record<string, { icon: React.ComponentType<{ size?: number }>; label: string }> = {
    in_scope_auto: { icon: ShieldCheck, label: "自动回答" },
    human_handoff_required: { icon: Headset, label: "人工接管" },
    out_of_scope: { icon: CircleSlash, label: "超出范围" },
  };
  const { icon: Icon, label } = config[boundary.classification] ?? config.in_scope_auto;
  const statusClass = boundary.classification === "in_scope_auto" ? "auto"
    : boundary.classification === "human_handoff_required" ? "handoff"
    : "blocked";

  return (
    <div className={`boundary-status-bar ${statusClass}`}>
      <Icon size={14} />
      <strong>{label}</strong>
      {boundary.display_message && <span>{boundary.display_message}</span>}
    </div>
  );
}
```

- [ ] **Step 2: Add BoundaryStatusBar CSS to styles.css**

Add after the existing `.boundary-badge.compact` styles (around line 754):

```css
/* --- Boundary Status Bar --------------------------------------------------- */
.boundary-status-bar {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-md);
  font-size: 13px;
  font-weight: 600;
  line-height: 1.4;
}

.boundary-status-bar.auto {
  background: var(--green-weak);
  color: var(--green);
  border-left: 3px solid var(--green);
}

.boundary-status-bar.handoff {
  background: var(--amber-weak);
  color: var(--amber);
  border-left: 3px solid var(--amber);
}

.boundary-status-bar.blocked {
  background: var(--crimson-weak);
  color: var(--crimson);
  border-left: 3px solid var(--crimson);
}

.boundary-status-bar strong {
  font-weight: 600;
}

.boundary-status-bar span {
  font-weight: 400;
  color: var(--text-secondary);
  margin-left: var(--space-1);
}
```

- [ ] **Step 3: Verify TypeScript and build**

```bash
cd /Users/loda/Desktop/pc-agent/frontend && npx tsc --noEmit && npm run build
```

Expected: Both pass with no errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/Boundary.tsx frontend/src/styles.css
git commit -m "feat: add BoundaryStatusBar component with compact status bar styles"
```

---

### Task 2: Restructure ContextPanel section order and visual hierarchy

**Files:**
- Modify: `frontend/src/components/ContextPanel.tsx`
- Modify: `frontend/src/styles.css`

**Interfaces:**
- Consumes: `<BoundaryStatusBar>` from Task 1
- Produces: ContextPanel with three-tier layout (L1→L2→L3)

- [ ] **Step 1: Import BoundaryStatusBar in ContextPanel.tsx**

Update the import line in `frontend/src/components/ContextPanel.tsx`:

Change:
```tsx
import { BoundaryBadge, BoundaryStatusCard } from "./Boundary";
```
To:
```tsx
import { BoundaryBadge, BoundaryStatusCard, BoundaryStatusBar } from "./Boundary";
```

- [ ] **Step 2: Reorder ContextPanel sections to L1→L2→L3 priority**

In `ContextPanel.tsx`, the current return JSX order is:
1. metrics-strip
2. boundary (BoundaryStatusCard)
3. handoff (conditional)
4. turns (context timeline)
5. evidence
6. products
7. order
8. after-sales

Reorder to the new three-tier structure. Replace the boundary section with BoundaryStatusBar. The new order is:

1. **L1 Status**: metrics-strip → boundary (BoundaryStatusBar instead of BoundaryStatusCard)
2. **L2 Primary**: products → order
3. **L3 Details**: evidence → turns (context) → handoff → after-sales

Replace the boundary section:
```tsx
{/* L1: Status */}
<div className="metrics-strip">
  <Metric icon={<Boxes size={14} />} label="SKU" value={skuCount} />
  <Metric icon={<Truck size={14} />} label="订单" value={orderCount} />
  <Metric icon={<BookOpenText size={14} />} label="依据" value={evidenceCount} />
</div>

<section className="panel-section">
  <div className="section-title">
    <ShieldCheck size={14} />
    <h2>状态</h2>
  </div>
  {boundary ? (
    <BoundaryStatusBar boundary={boundary} />
  ) : (
    <EmptyState text="等待请求" />
  )}
</section>
```

Then L2 Primary:
```tsx
{/* L2: Primary */}
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
```

Then L3 Details:
```tsx
{/* L3: Details */}
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

{turns.length > 0 && (
  <section className="panel-section">
    <div className="section-title">
      <History size={14} />
      <h2>上下文</h2>
    </div>
    <ConversationTimeline turns={turns} />
  </section>
)}

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
```

- [ ] **Step 3: Update CSS — panel background and visual hierarchy**

In `frontend/src/styles.css`:

**a) Change `.context-panel` background from `--bg-panel` to `--bg-base`:**

Find the `.context-panel` rule and change:
```css
/* FROM: */
background: var(--bg-panel);
/* TO: */
background: var(--bg-base);
```

**b) Add section background, padding, and bottom divider to `.panel-section`:**

Find the `.panel-section` rule (currently just `min-width: 0;`) and replace with:
```css
.panel-section {
  min-width: 0;
  background: var(--bg-panel);
  border-radius: var(--radius-md);
  padding: var(--space-3);
  margin-bottom: var(--space-2);
}
```

**c) Add section title icon background:**

Find `.section-title svg` (currently just `color: var(--copper);`) and replace with:
```css
.section-title svg {
  color: var(--copper);
  background: var(--copper-weak);
  padding: 4px;
  border-radius: var(--radius-sm);
}
```

**d) Add product card left border accent:**

Find `.product-card {` and add `border-left: 3px solid var(--copper);` to it, changing `border: 1px solid var(--border-default)` to `border: 1px solid var(--border-default); border-left: 3px solid var(--copper);`.

**e) Update metrics strip to match new panel background:**

The metrics strip should also use `--bg-panel` background to contrast with the now-darker panel:
```css
.metrics-strip {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: var(--space-2);
  padding: var(--space-3);
  background: var(--bg-panel);
  border-radius: var(--radius-md);
  margin-bottom: var(--space-2);
}
```

Note: The `.metric` items inside should remove their individual borders since they're now inside a container with background.

Find `.metric` and remove `border: 1px solid var(--border-default);` and `border-radius: var(--radius-sm);`.

- [ ] **Step 4: Verify TypeScript and build**

```bash
cd /Users/loda/Desktop/pc-agent/frontend && npx tsc --noEmit && npm run build
```

Expected: Both pass.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/ContextPanel.tsx frontend/src/styles.css
git commit -m "feat: restructure ContextPanel to L1→L2→L3 priority layout with visual hierarchy"
```

---

### Task 3: Final visual polish and verification

**Files:**
- Modify: `frontend/src/styles.css` (responsive adjustments)

**Interfaces:**
- Consumes: All previous task outputs
- Produces: Final polished right panel

- [ ] **Step 1: Update mobile drawer styles for new section order**

In the mobile media query `@media (max-width: 820px)`, verify that `.context-panel.mobile-visible` still works with the restructured sections. The mobile tab filtering (products vs details) already conditions on `mobileTab` prop — no structural changes needed, just ensure the new `.panel-section` backgrounds don't clash.

Add a mobile-specific override inside the `@media (max-width: 820px)` block:
```css
.context-panel.mobile-visible .panel-section {
  border-radius: 0;
  margin-bottom: 0;
  border-bottom: 1px solid var(--border-subtle);
}
```

- [ ] **Step 2: Verify full TypeScript check and build**

```bash
cd /Users/loda/Desktop/pc-agent/frontend && npx tsc --noEmit && npm run build
```

- [ ] **Step 3: Visual verification checklist**

Start the dev server (`cd frontend && npm run dev`) and verify:
1. Right panel background is darker (matches chat area background)
2. Sections have distinct backgrounds and dividers
3. Boundary status shows as a compact status bar (green/amber/red left border)
4. Product cards have a copper left border accent
5. Section title icons have copper circular backgrounds
6. Metrics strip is inside a panel-background container
7. Section order is: metrics → boundary → products → order → evidence → context → handoff → after-sales
8. Mobile drawer still works correctly
9. Collapsible panel toggle still works

- [ ] **Step 4: Commit**

```bash
git add frontend/src/styles.css
git commit -m "feat: add mobile drawer panel-section overrides and final visual polish"
```