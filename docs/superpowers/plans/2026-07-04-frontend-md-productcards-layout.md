# Frontend MD Rendering, Product Cards & Layout Optimization — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Markdown rendering to assistant chat messages, render inline product cards with image placeholders, and optimize the overall three-panel layout including a collapsible right panel and mobile drawer.

**Architecture:** Extend the existing `ChatMessage` type to carry `products`, install `react-markdown` + `remark-gfm` for MD rendering, create new `MarkdownContent` and `ProductInlineCard` components, and update CSS for layout adjustments. No backend changes needed — all product data already flows through SSE `context` events.

**Tech Stack:** React 19, TypeScript, react-markdown, remark-gfm, lucide-react (icons), CSS custom properties (existing PCB design system)

## Global Constraints

- Python 4 spaces, TypeScript 2 spaces, Ruff line width 100
- Never commit `.env`, API keys, database passwords, or real user data
- React components use PascalCase, variables/hooks use camelCase
- Use existing CSS custom properties (`--copper`, `--bg-card`, `--text-primary`, etc.) — no new color values
- All new CSS goes into `styles.css` or component-specific CSS files, no inline styles
- Commit style: `feat:`, `fix:`, `to:` prefix
- No backend changes — all data already available via SSE events

---

## File Structure

### New Files
| File | Responsibility |
|------|---------------|
| `frontend/src/components/MarkdownContent.tsx` | React component that renders MD string using react-markdown + remark-gfm with dark-theme custom renderers |
| `frontend/src/components/ProductInlineCard.tsx` | Chat-bubble inline product card component (image/icon + title + specs + price) |
| `frontend/src/components/MobileTabBar.tsx` | Bottom tab bar for mobile `<820px` view (chat / products / details) |
| `frontend/src/components/markdown.css` | MD rendering styles (prose-like overrides matching PCB design system) |

### Modified Files
| File | Changes |
|------|---------|
| `frontend/src/types.ts` | Add `products?: ProductCard[]` to `ChatMessage` |
| `frontend/src/App.tsx` | Associate SSE products with streaming message; add `contextPanelCollapsed` and `activeMobileTab` state; pass new props |
| `frontend/src/components/ChatPanel.tsx` | Use `MarkdownContent` instead of `<p>`; render `<ProductCardRow>` for messages with products; receive and forward collapse toggle |
| `frontend/src/components/ContextPanel.tsx` | Increase thumb to 56px; add category icon placeholder; accept collapsed/drawer mode props |
| `frontend/src/components/Sidebar.tsx` | Group conversations by date (today/yesterday/earlier); slide-in delete button |
| `frontend/src/styles.css` | Grid ratio change; MD bubble styles; product-card-row; mobile drawer; sidebar group styles; collapse button; markdown.css import |

---

### Task 1: Install react-markdown and remark-gfm

**Files:**
- Modify: `frontend/package.json`

**Interfaces:**
- Produces: `react-markdown` and `remark-gfm` available as imports

- [ ] **Step 1: Install dependencies**

```bash
cd /Users/loda/Desktop/pc-agent/frontend && npm install react-markdown remark-gfm
```

- [ ] **Step 2: Verify installation**

```bash
cd /Users/loda/Desktop/pc-agent/frontend && npm ls react-markdown remark-gfm
```

Expected: Both packages listed with versions, no errors.

- [ ] **Step 3: Verify TypeScript compilation**

```bash
cd /Users/loda/Desktop/pc-agent/frontend && npx tsc --noEmit
```

Expected: No type errors related to the new packages.

- [ ] **Step 4: Commit**

```bash
git add frontend/package.json frontend/package-lock.json
git commit -m "feat: add react-markdown and remark-gfm dependencies"
```

---

### Task 2: Create MarkdownContent component and styles

**Files:**
- Create: `frontend/src/components/MarkdownContent.tsx`
- Create: `frontend/src/components/markdown.css`
- Modify: `frontend/src/styles.css` (add `@import` for markdown.css)

**Interfaces:**
- Consumes: `children: string` prop (the raw MD content)
- Produces: `<MarkdownContent>` React component exported from `MarkdownContent.tsx`

- [ ] **Step 1: Create markdown.css**

Create `frontend/src/components/markdown.css` with MD rendering styles that match the PCB dark design system:

```css
/* ==========================================================================
   Markdown Content — Dark PCB Theme
   Styles for react-markdown rendered content inside assistant bubbles.
   ========================================================================== */

.markdown-content {
  line-height: 1.65;
  overflow-wrap: anywhere;
}

.markdown-content > *:first-child {
  margin-top: 0;
}

.markdown-content > *:last-child {
  margin-bottom: 0;
}

/* Headings */
.markdown-content h1,
.markdown-content h2,
.markdown-content h3,
.markdown-content h4 {
  margin: var(--space-4) 0 var(--space-2);
  color: var(--text-primary);
  line-height: 1.3;
}

.markdown-content h1 { font-size: 18px; font-weight: 700; }
.markdown-content h2 { font-size: 16px; font-weight: 700; }
.markdown-content h3 { font-size: 15px; font-weight: 600; }
.markdown-content h4 { font-size: 14px; font-weight: 600; }

/* Paragraphs */
.markdown-content p {
  margin: var(--space-2) 0;
  line-height: 1.65;
}

/* Strong / Emphasis */
.markdown-content strong {
  color: var(--text-primary);
  font-weight: 700;
}

.markdown-content em {
  color: var(--text-secondary);
  font-style: italic;
}

/* Links */
.markdown-content a {
  color: var(--copper);
  text-decoration: underline;
  text-underline-offset: 2px;
  transition: color var(--duration-fast) var(--ease-out);
}

.markdown-content a:hover {
  color: var(--text-primary);
}

/* Lists */
.markdown-content ul,
.markdown-content ol {
  margin: var(--space-2) 0;
  padding-left: var(--space-5);
}

.markdown-content li {
  margin: var(--space-1) 0;
  line-height: 1.55;
}

.markdown-content li > ul,
.markdown-content li > ol {
  margin: var(--space-1) 0;
}

/* Inline code */
.markdown-content code {
  font-family: var(--font-mono);
  font-size: 0.9em;
  background: var(--bg-input);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  padding: 1px 5px;
  color: var(--copper);
}

/* Code blocks */
.markdown-content pre {
  margin: var(--space-3) 0;
  background: var(--bg-card);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  padding: var(--space-3);
  overflow-x: auto;
}

.markdown-content pre code {
  background: none;
  border: none;
  padding: 0;
  color: var(--text-primary);
  font-size: 13px;
  line-height: 1.5;
}

/* Tables */
.markdown-content table {
  width: 100%;
  border-collapse: collapse;
  margin: var(--space-3) 0;
  font-size: 13px;
}

.markdown-content th {
  background: var(--bg-card);
  color: var(--text-primary);
  font-weight: 600;
  text-align: left;
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--border-default);
}

.markdown-content td {
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--border-default);
  color: var(--text-secondary);
}

.markdown-content tr:nth-child(even) td {
  background: var(--bg-elevated);
}

/* Blockquotes */
.markdown-content blockquote {
  margin: var(--space-3) 0;
  padding: var(--space-2) var(--space-4);
  border-left: 3px solid var(--copper-dim);
  background: var(--copper-weak);
  color: var(--text-secondary);
  border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
}

.markdown-content blockquote p {
  margin: var(--space-1) 0;
}

/* Horizontal rule */
.markdown-content hr {
  border: none;
  border-top: 1px solid var(--border-default);
  margin: var(--space-4) 0;
}

/* Images inside markdown (e.g., product images from agent) */
.markdown-content img {
  max-width: 100%;
  border-radius: var(--radius-md);
  margin: var(--space-2) 0;
}
```

- [ ] **Step 2: Add markdown.css import to styles.css**

Add at the end of `frontend/src/styles.css`:

```css
/* --- Markdown Content ----------------------------------------------------- */
@import url('./components/markdown.css');
```

- [ ] **Step 3: Create MarkdownContent.tsx**

Create `frontend/src/components/MarkdownContent.tsx`:

```tsx
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Components } from "react-markdown";

type MarkdownContentProps = {
  children: string;
};

const components: Components = {
  // Open links in new tab for security
  a: ({ href, children }) => (
    <a href={href} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  ),
};

export function MarkdownContent({ children }: MarkdownContentProps) {
  if (!children) return null;

  return (
    <div className="markdown-content">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {children}
      </ReactMarkdown>
    </div>
  );
}
```

- [ ] **Step 4: Verify TypeScript compilation**

```bash
cd /Users/loda/Desktop/pc-agent/frontend && npx tsc --noEmit
```

Expected: No type errors.

- [ ] **Step 5: Verify Vite build**

```bash
cd /Users/loda/Desktop/pc-agent/frontend && npm run build
```

Expected: Build succeeds with no errors.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/MarkdownContent.tsx frontend/src/components/markdown.css frontend/src/styles.css
git commit -m "feat: add MarkdownContent component with dark PCB theme styles"
```

---

### Task 3: Integrate MarkdownContent into ChatPanel

**Files:**
- Modify: `frontend/src/components/ChatPanel.tsx`
- Modify: `frontend/src/styles.css` (adjust `.message p` → `.markdown-content` and user message styles)

**Interfaces:**
- Consumes: `<MarkdownContent>` from Task 2
- Produces: Assistant messages rendered with MD; user messages still plain text

- [ ] **Step 1: Update MessageRow to use MarkdownContent for assistant messages**

In `frontend/src/components/ChatPanel.tsx`, add the import and modify the `MessageRow` component:

Add import at top:
```tsx
import { MarkdownContent } from "./MarkdownContent";
```

Replace the `<p>{bubbleContent}</p>` block in `MessageRow` (lines 144-148) with:

```tsx
{message.role === "assistant" ? (
  <MarkdownContent>
    {bubbleContent}
  </MarkdownContent>
) : (
  <p>
    {message.status === "streaming" && !message.content && (
      <Loader2 size={15} className="spin inline-icon" />
    )}
    {bubbleContent}
  </p>
)}
```

- [ ] **Step 2: Update CSS — adjust message bubble styles**

In `frontend/src/styles.css`, modify the `.message p` rule (around line 814). Remove `white-space: pre-wrap` from the base `.message p` and add it specifically for user messages. Add the markdown-content bubble styling:

Find this block (lines 814-825):
```css
.message p {
  margin: 0;
  max-width: 100%;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  line-height: 1.6;
  background: var(--bg-card);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  padding: var(--space-3) 14px;
  font-size: 14px;
}
```

Replace with:
```css
.message p,
.message .markdown-content {
  margin: 0;
  max-width: 100%;
  overflow-wrap: anywhere;
  line-height: 1.6;
  background: var(--bg-card);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  padding: var(--space-3) 14px;
  font-size: 14px;
}

.message p {
  white-space: pre-wrap;
}
```

- [ ] **Step 3: Verify the app renders assistant messages with MD**

```bash
cd /Users/loda/Desktop/pc-agent/frontend && npm run build
```

Expected: Build succeeds.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/ChatPanel.tsx frontend/src/styles.css
git commit -m "feat: render assistant messages with MarkdownContent, keep user messages as plain text"
```

---

### Task 4: Extend ChatMessage type and App state for per-message products

**Files:**
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/App.tsx`

**Interfaces:**
- Consumes: `ProductCard` type from `types.ts`
- Produces: `ChatMessage.products?: ProductCard[]` — new field; App.tsx populates it from SSE context events

- [ ] **Step 1: Add `products` field to ChatMessage type**

In `frontend/src/types.ts`, find the `ChatMessage` type (around line 97) and add the `products` field:

```ts
export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  createdAt: string;
  status?: "sent" | "failed" | "received" | "streaming" | "cancelled";
  streamStage?: string;
  boundary?: BoundaryClassification;
  intent?: string;
  evidenceCount?: number;
  productCount?: number;
  orderId?: number;
  suggestedActions?: SuggestedAction[];
  products?: ProductCard[];
};
```

- [ ] **Step 2: Update SSE context event handler to set products on the streaming message**

In `frontend/src/App.tsx`, locate the `handleStreamEvent` function. In the `case "context":` block, after the existing `productCount` assignment, add `products` to the same `setMessages` call.

Find the pattern inside `handleStreamEvent` for `"context"` type where `setMessages` is called with `prev.map(msg => ...)`. It currently updates `productCount` and `streamStage`. Add `products` to the updated message object:

```ts
products: msg.id === streamingMsgId ? (event.boundary?.classification === "out_of_scope" ? [] : event.products) : msg.products,
```

This should be added alongside the existing `productCount` assignment in the same `setMessages` call.

- [ ] **Step 3: Update the `done` event / `applyFinalResponse` to preserve products on the message**

In `App.tsx`, find `applyFinalResponse` or the `"done"` case handler. When it updates the final message state, ensure `products` is preserved from the message object (it was set during the `context` event and should remain). No special action needed if the `setMessages` call spreads existing message fields — but verify the `done` handler doesn't strip `products`.

- [ ] **Step 4: Update conversation history restore to include products**

Find `applyConversationDetail` in `App.tsx`. When restoring messages from history, if the API returns products data in message metadata, extract and attach it. Currently the conversation detail endpoint may not return per-message products — if so, leave `products` as `undefined` for restored messages (they'll show without inline cards, which is acceptable).

- [ ] **Step 5: Verify TypeScript compilation**

```bash
cd /Users/loda/Desktop/pc-agent/frontend && npx tsc --noEmit
```

Expected: No type errors related to `ChatMessage.products`.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/types.ts frontend/src/App.tsx
git commit -m "feat: add products field to ChatMessage, populate from SSE context events"
```

---

### Task 5: Create ProductInlineCard component

**Files:**
- Create: `frontend/src/components/ProductInlineCard.tsx`
- Modify: `frontend/src/styles.css` (add product-inline-card and product-card-row styles)

**Interfaces:**
- Consumes: `ProductCard` type from `types.ts`, lucide-react icons (`Mouse`, `Keyboard`, `Headphones`, `Package`)
- Produces: `<ProductInlineCard>` and `<ProductCardRow>` React components

- [ ] **Step 1: Create ProductInlineCard.tsx**

Create `frontend/src/components/ProductInlineCard.tsx`:

```tsx
import { Headphones, Keyboard, Mouse, Package } from "lucide-react";
import type { ProductCard } from "../types";

type ProductInlineCardProps = {
  product: ProductCard;
  onClick?: () => void;
};

const CATEGORY_ICONS: Record<string, { icon: React.ComponentType<{ size?: number }>; gradient: string }> = {
  mouse: { icon: Mouse, gradient: "linear-gradient(135deg, rgba(200,149,108,0.2) 0%, rgba(154,112,79,0.1) 100%)" },
  keyboard: { icon: Keyboard, gradient: "linear-gradient(135deg, rgba(61,171,106,0.15) 0%, rgba(45,128,80,0.08) 100%)" },
  headphone: { icon: Headphones, gradient: "linear-gradient(135deg, rgba(91,156,245,0.15) 0%, rgba(61,123,217,0.08) 100%)" },
  headset: { icon: Headphones, gradient: "linear-gradient(135deg, rgba(91,156,245,0.15) 0%, rgba(61,123,217,0.08) 100%)" },
};

const DEFAULT_ICON = { icon: Package, gradient: "linear-gradient(135deg, rgba(212,160,54,0.15) 0%, rgba(166,124,40,0.08) 100%)" };

function getCategoryStyle(category: string) {
  const key = category.toLowerCase().replace(/[市售品]/g, "").trim();
  for (const [catKey, style] of Object.entries(CATEGORY_ICONS)) {
    if (key.includes(catKey) || catKey.includes(key)) return style;
  }
  return DEFAULT_ICON;
}

export function ProductInlineCard({ product, onClick }: ProductInlineCardProps) {
  const { icon: Icon, gradient } = getCategoryStyle(product.category);
  const specLine = Object.entries(product.specs)
    .slice(0, 3)
    .map(([key, value]) => `${key}: ${value}`)
    .join(" · ");

  return (
    <article
      className="product-inline-card"
      onClick={onClick}
      role={onClick ? "button" : undefined}
      tabIndex={onClick ? 0 : undefined}
    >
      <div className="product-inline-thumb" style={{ background: gradient }}>
        {product.image_url ? (
          <img src={product.image_url} alt={product.title} loading="lazy" />
        ) : (
          <Icon size={24} />
        )}
      </div>
      <div className="product-inline-info">
        <h4>{product.title}</h4>
        <small>{product.brand} · {product.category}</small>
        {specLine && <small className="specs">{specLine}</small>}
      </div>
      <div className="product-inline-price">
        <strong>¥{product.price}</strong>
        <span>{product.stock > 0 ? `库存 ${product.stock}` : "缺货"}</span>
      </div>
    </article>
  );
}

type ProductCardRowProps = {
  products: ProductCard[];
  onProductClick?: (product: ProductCard) => void;
};

export function ProductCardRow({ products, onProductClick }: ProductCardRowProps) {
  if (products.length === 0) return null;

  return (
    <div className="product-card-row">
      {products.map((product) => (
        <ProductInlineCard
          key={product.sku_id}
          product={product}
          onClick={onProductClick ? () => onProductClick(product) : undefined}
        />
      ))}
    </div>
  );
}
```

- [ ] **Step 2: Add product-inline-card and product-card-row styles**

Add to `frontend/src/styles.css` (after the `.product-card` section, around line 1240):

```css
/* --- Product Inline Card (Chat Bubble) ----------------------------------- */
.product-card-row {
  display: flex;
  gap: var(--space-2);
  overflow-x: auto;
  scroll-snap-type: x mandatory;
  padding: var(--space-1) 0;
  margin-top: var(--space-2);
  -webkit-overflow-scrolling: touch;
}

.product-card-row::-webkit-scrollbar {
  height: 4px;
}

.product-card-row::-webkit-scrollbar-track {
  background: transparent;
}

.product-card-row::-webkit-scrollbar-thumb {
  background: var(--border-default);
  border-radius: var(--radius-pill);
}

.product-inline-card {
  flex: 0 0 220px;
  scroll-snap-align: start;
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  background: var(--bg-elevated);
  padding: var(--space-2);
  display: grid;
  grid-template-columns: 56px minmax(0, 1fr);
  grid-template-rows: auto auto;
  gap: var(--space-2);
  transition: border-color var(--duration-fast) var(--ease-out);
}

.product-inline-card:hover {
  border-color: var(--copper-dim);
}

.product-inline-card[role="button"] {
  cursor: pointer;
}

.product-inline-card[role="button"]:hover {
  background: var(--bg-card-hover);
}

.product-inline-thumb {
  grid-row: 1 / 3;
  width: 56px;
  height: 56px;
  border-radius: var(--radius-sm);
  display: grid;
  place-items: center;
  overflow: hidden;
  color: var(--copper);
}

.product-inline-thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.product-inline-info {
  min-width: 0;
}

.product-inline-info h4 {
  margin: 0;
  font-size: 13px;
  font-weight: 600;
  line-height: 1.3;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.product-inline-info small {
  display: block;
  font-size: 11px;
  color: var(--text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.product-inline-info .specs {
  color: var(--text-tertiary);
  margin-top: 1px;
}

.product-inline-price {
  grid-column: 1 / -1;
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding-top: var(--space-1);
  border-top: 1px solid var(--border-subtle);
}

.product-inline-price strong {
  color: var(--copper);
  font-family: var(--font-mono);
  font-size: 14px;
}

.product-inline-price span {
  color: var(--green);
  font-size: 11px;
  font-family: var(--font-mono);
}
```

- [ ] **Step 3: Verify build**

```bash
cd /Users/loda/Desktop/pc-agent/frontend && npm run build
```

Expected: Build succeeds.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/ProductInlineCard.tsx frontend/src/styles.css
git commit -m "feat: add ProductInlineCard and ProductCardRow components with styles"
```

---

### Task 6: Integrate ProductCardRow into ChatPanel

**Files:**
- Modify: `frontend/src/components/ChatPanel.tsx`
- Modify: `frontend/src/App.tsx` (pass products down to ChatPanel, add scroll-to-product handler)

**Interfaces:**
- Consumes: `<ProductCardRow>` from Task 5, `ChatMessage.products` from Task 4
- Produces: Assistant messages show inline product cards below MD content; clicking a card highlights it in ContextPanel

- [ ] **Step 1: Update ChatPanel to accept products and render ProductCardRow**

In `frontend/src/components/ChatPanel.tsx`, add imports:

```tsx
import { ProductCardRow } from "./ProductInlineCard";
import type { ProductCard } from "../types";
```

Add `onProductClick` prop to `ChatPanelProps`:

```tsx
type ChatPanelProps = {
  conversationId?: number;
  messages: ChatMessage[];
  input: string;
  loading: boolean;
  responseStatus: ResponseStatus;
  boundary: BoundaryClassification | null;
  suggestedActions: SuggestedAction[];
  error: RequestError | null;
  onInputChange: (value: string) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  onCancel: () => void;
  onRetry: () => void;
  onSuggestedAction: (action: SuggestedAction) => void;
  onProductClick?: (product: ProductCard) => void;
};
```

Destructure `onProductClick` in the component function params.

- [ ] **Step 2: Update MessageRow to render ProductCardRow**

In the `MessageRow` component, after the `<MarkdownContent>` / `<p>` block (but still inside `.bubble-stack`), add:

```tsx
{message.role === "assistant" && message.products && message.products.length > 0 && (
  <ProductCardRow products={message.products} onProductClick={onProductClick} />
)}
```

Update `MessageRow` props type to include `onProductClick`:

```tsx
function MessageRow({ message, onProductClick }: { message: ChatMessage; onProductClick?: (product: ProductCard) => void }) {
```

Update the `messages.map` call in `ChatPanel` to pass `onProductClick`:

```tsx
<MessageRow key={message.id} message={message} onProductClick={onProductClick} />
```

- [ ] **Step 3: Add product click handler in App.tsx**

In `frontend/src/App.tsx`, add a handler that scrolls to and highlights the product in the context panel:

```tsx
const [highlightedProductId, setHighlightedProductId] = useState<number | null>(null);

const handleProductClick = useCallback((product: ProductCard) => {
  setHighlightedProductId(product.sku_id);
  setTimeout(() => setHighlightedProductId(null), 2000);
}, []);
```

Pass `highlightedProductId` to `ContextPanel` and `onProductClick={handleProductClick}` to `ChatPanel`.

- [ ] **Step 4: Update ContextPanel to highlight the clicked product**

In `frontend/src/components/ContextPanel.tsx`, add `highlightedProductId` prop:

```tsx
type ContextPanelProps = {
  // ...existing props...
  highlightedProductId?: number | null;
};
```

In the `ProductCardView` component, add a highlight class:

```tsx
function ProductCardView({ product, highlighted }: { product: ProductCard; highlighted: boolean }) {
  // ...existing code...
  return (
    <article className={`product-card ${highlighted ? "highlighted" : ""}`}>
      // ...existing content...
    </article>
  );
}
```

Update the `products.map` call to pass `highlighted`:

```tsx
{products.map((product) => (
  <ProductCardView key={product.sku_id} product={product} highlighted={product.sku_id === highlightedProductId} />
))}
```

- [ ] **Step 5: Add highlight animation CSS**

Add to `frontend/src/styles.css` after `.product-card:hover`:

```css
.product-card.highlighted {
  border-color: var(--copper);
  animation: highlight-pulse 2s ease-out;
}

@keyframes highlight-pulse {
  0% { box-shadow: 0 0 0 2px var(--copper-glow); }
  50% { box-shadow: 0 0 8px 4px var(--copper-glow); }
  100% { box-shadow: none; }
}
```

- [ ] **Step 6: Verify build**

```bash
cd /Users/loda/Desktop/pc-agent/frontend && npm run build
```

Expected: Build succeeds.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/components/ChatPanel.tsx frontend/src/App.tsx frontend/src/components/ContextPanel.tsx frontend/src/styles.css
git commit -m "feat: render product cards inline in chat bubbles with click-to-highlight"
```

---

### Task 7: Update ContextPanel product cards with larger thumbs and icon placeholders

**Files:**
- Modify: `frontend/src/components/ContextPanel.tsx` (ProductCardView thumb)
- Modify: `frontend/src/styles.css` (`.thumb` and `.product-card` grid)

**Interfaces:**
- Consumes: lucide-react icons (`Mouse`, `Keyboard`, `Headphones`, `Package`) for category-based placeholders
- Produces: ContextPanel product cards with 56px thumbs and icon placeholders when no image

- [ ] **Step 1: Import icons and update ProductCardView in ContextPanel.tsx**

Add imports at top of `ContextPanel.tsx`:

```tsx
import { Headphones, Keyboard, Mouse, Package as PackageIcon } from "lucide-react";
```

Update `ProductCardView` to use icon placeholders for missing images:

```tsx
function getCategoryIcon(category: string) {
  const lower = category.toLowerCase();
  if (lower.includes("mouse") || lower.includes("鼠标")) return Mouse;
  if (lower.includes("keyboard") || lower.includes("键盘")) return Keyboard;
  if (lower.includes("headphone") || lower.includes("headset") || lower.includes("耳机")) return Headphones;
  return PackageIcon;
}

function ProductCardView({ product, highlighted }: { product: ProductCard; highlighted: boolean }) {
  const IconComponent = getCategoryIcon(product.category);
  const specLine = Object.entries(product.specs)
    .slice(0, 4)
    .map(([key, value]) => `${key}: ${value}`)
    .join(" · ");

  return (
    <article className={`product-card ${highlighted ? "highlighted" : ""}`}>
      <div className="thumb">
        {product.image_url ? (
          <img src={product.image_url} alt={product.title} loading="lazy" />
        ) : (
          <IconComponent size={20} />
        )}
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
```

- [ ] **Step 2: Update .thumb CSS to support images**

In `frontend/src/styles.css`, update the `.thumb` rule (around line 1188):

```css
.thumb {
  width: 56px;
  height: 56px;
  border-radius: var(--radius-sm);
  display: grid;
  place-items: center;
  background: var(--copper-weak);
  color: var(--copper);
  overflow: hidden;
}

.thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}
```

Update `.product-card` grid to match the new thumb size:

```css
.product-card {
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  background: var(--bg-card);
  padding: var(--space-3);
  display: grid;
  grid-template-columns: 56px minmax(0, 1fr);
  gap: var(--space-2);
  transition: border-color var(--duration-fast) var(--ease-out);
}
```

- [ ] **Step 3: Remove old PackageSearch import from ContextPanel since ProductCardView no longer uses it**

In `ContextPanel.tsx`, remove `PackageSearch` from the lucide-react import (it's now only used by the old thumb). Add `Package as PackageIcon` to the import if not already there, and remove `PackageSearch` if unused elsewhere. Check if `PackageSearch` is still used — it was used in the old `ProductCardView` thumb. Replace with the dynamic icon.

- [ ] **Step 4: Verify build**

```bash
cd /Users/loda/Desktop/pc-agent/frontend && npm run build
```

Expected: Build succeeds.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/ContextPanel.tsx frontend/src/styles.css
git commit -m "feat: enlarge product card thumbs, add category icon placeholders for missing images"
```

---

### Task 8: Sidebar — conversation grouping and slide-in delete button

**Files:**
- Modify: `frontend/src/components/Sidebar.tsx`
- Modify: `frontend/src/styles.css`

**Interfaces:**
- Consumes: `ConversationSummary` type with `last_message_at` field
- Produces: Grouped conversation list (today/yesterday/earlier); slide-in delete animation

- [ ] **Step 1: Add conversation grouping logic to Sidebar**

In `frontend/src/components/Sidebar.tsx`, add a helper function before the `Sidebar` component:

```tsx
function groupConversations(conversations: ConversationSummary[]) {
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const yesterday = new Date(today.getTime() - 86400000);

  const groups: { label: string; conversations: ConversationSummary[] }[] = [
    { label: "今天", conversations: [] },
    { label: "昨天", conversations: [] },
    { label: "更早", conversations: [] },
  ];

  for (const conv of conversations) {
    const date = new Date(conv.last_message_at ?? conv.created_at);
    if (date >= today) {
      groups[0].conversations.push(conv);
    } else if (date >= yesterday) {
      groups[1].conversations.push(conv);
    } else {
      groups[2].conversations.push(conv);
    }
  }

  return groups.filter((group) => group.conversations.length > 0);
}
```

Update the conversation section in `Sidebar` to render groups:

Replace the flat `conversations.map(...)` inside `.conversation-list` with:

```tsx
<div className="conversation-list">
  {groupConversations(conversations).map((group) => (
    <div key={group.label} className="conversation-group">
      <h3 className="conversation-group-label">{group.label}</h3>
      {group.conversations.map((conversation) => (
        <ConversationRow
          key={conversation.id}
          conversation={conversation}
          active={conversation.id === activeConversationId}
          disabled={disabled}
          onSelect={onSelectConversation}
          onDelete={onDeleteConversation}
        />
      ))}
    </div>
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
```

- [ ] **Step 2: Add group label and slide-in delete CSS**

Add to `frontend/src/styles.css` after `.conversation-list`:

```css
.conversation-group {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.conversation-group-label {
  margin: var(--space-2) 0 var(--space-1);
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--text-muted);
  font-weight: 600;
  padding-left: var(--space-1);
}
```

Update `.conversation-delete` to use slide-in animation:

Replace the existing `.conversation-delete` opacity-based hover with a slide-in transform:

```css
.conversation-delete {
  position: absolute;
  right: 6px;
  top: 50%;
  transform: translateY(-50%) translateX(100%);
  width: 24px;
  height: 24px;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-muted);
  display: grid;
  place-items: center;
  opacity: 0;
  transition: transform var(--duration-med) var(--ease-out),
              opacity var(--duration-fast) var(--ease-out),
              color var(--duration-fast) var(--ease-out),
              background-color var(--duration-fast) var(--ease-out);
}

.conversation-item:hover .conversation-delete {
  opacity: 1;
  transform: translateY(-50%) translateX(0);
}

.conversation-delete:hover {
  color: var(--crimson);
  background: var(--crimson-weak);
}
```

- [ ] **Step 3: Verify build**

```bash
cd /Users/loda/Desktop/pc-agent/frontend && npm run build
```

Expected: Build succeeds.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/Sidebar.tsx frontend/src/styles.css
git commit -m "feat: group conversations by date, slide-in delete button animation"
```

---

### Task 9: Layout — collapsible right panel and grid ratio adjustment

**Files:**
- Modify: `frontend/src/App.tsx` (add `contextPanelCollapsed` state, toggle button)
- Modify: `frontend/src/styles.css` (grid ratio, collapse styles)

**Interfaces:**
- Consumes: `PanelRightClose` / `PanelRightOpen` from lucide-react
- Produces: Collapsible right panel; adjusted grid ratio `256px minmax(0, 1fr) 360px`

- [ ] **Step 1: Add collapse state and toggle button to App.tsx**

In `frontend/src/App.tsx`, add import:

```tsx
import { PanelRightClose, PanelRightOpen } from "lucide-react";
```

Add state:

```tsx
const [contextPanelCollapsed, setContextPanelCollapsed] = useState(false);
```

Update the `<main>` shell grid to conditionally include the context panel:

```tsx
<div className={`shell ${contextPanelCollapsed ? "shell-collapsed" : ""}`}>
  <Sidebar ... />
  <ChatPanel ... onProductClick={handleProductClick} />
  {!contextPanelCollapsed && (
    <ContextPanel ... highlightedProductId={highlightedProductId} />
  )}
  <button
    type="button"
    className="panel-toggle"
    onClick={() => setContextPanelCollapsed((c) => !c)}
    title={contextPanelCollapsed ? "展开详情" : "收起详情"}
  >
    {contextPanelCollapsed ? <PanelRightOpen size={18} /> : <PanelRightClose size={18} />}
  </button>
</div>
```

- [ ] **Step 2: Update grid ratio and add collapse CSS**

In `frontend/src/styles.css`, update the `.shell` grid:

```css
.shell {
  min-height: 100vh;
  display: grid;
  grid-template-columns: 256px minmax(0, 1fr) 360px;
  background: var(--bg-base);
  position: relative;
}

.shell.shell-collapsed {
  grid-template-columns: 256px minmax(0, 1fr) 0px;
}
```

Add panel toggle button styles:

```css
.panel-toggle {
  position: fixed;
  right: 368px;
  top: 50%;
  transform: translateY(-50%);
  width: 28px;
  height: 48px;
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  background: var(--bg-panel);
  color: var(--text-secondary);
  display: grid;
  place-items: center;
  z-index: 10;
  transition: right var(--duration-med) var(--ease-out),
              color var(--duration-fast) var(--ease-out),
              background-color var(--duration-fast) var(--ease-out);
}

.shell.shell-collapsed .panel-toggle {
  right: 8px;
}

.panel-toggle:hover {
  color: var(--copper);
  background: var(--bg-card);
}
```

- [ ] **Step 3: Verify build**

```bash
cd /Users/loda/Desktop/pc-agent/frontend && npm run build
```

Expected: Build succeeds.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/App.tsx frontend/src/styles.css
git commit -m "feat: collapsible right panel with toggle button, adjust grid ratio"
```

---

### Task 10: Mobile drawer for context panel

**Files:**
- Create: `frontend/src/components/MobileTabBar.tsx`
- Modify: `frontend/src/App.tsx` (add `activeMobileTab` state, conditional rendering)
- Modify: `frontend/src/styles.css` (mobile drawer styles, tab bar styles)

**Interfaces:**
- Consumes: `MessageSquare`, `PackageSearch`, `BookOpenText` icons from lucide-react
- Produces: `<820px` bottom tab bar; context panel renders as bottom drawer

- [ ] **Step 1: Create MobileTabBar.tsx**

Create `frontend/src/components/MobileTabBar.tsx`:

```tsx
import { BookOpenText, MessageSquare, PackageSearch } from "lucide-react";

export type MobileTab = "chat" | "products" | "details";

type MobileTabBarProps = {
  activeTab: MobileTab;
  onTabChange: (tab: MobileTab) => void;
  productCount: number;
  evidenceCount: number;
};

export function MobileTabBar({ activeTab, onTabChange, productCount, evidenceCount }: MobileTabBarProps) {
  return (
    <nav className="mobile-tab-bar">
      <button
        type="button"
        className={`mobile-tab ${activeTab === "chat" ? "active" : ""}`}
        onClick={() => onTabChange("chat")}
      >
        <MessageSquare size={18} />
        <span>对话</span>
      </button>
      <button
        type="button"
        className={`mobile-tab ${activeTab === "products" ? "active" : ""}`}
        onClick={() => onTabChange("products")}
      >
        <PackageSearch size={18} />
        <span>商品{productCount > 0 ? ` (${productCount})` : ""}</span>
      </button>
      <button
        type="button"
        className={`mobile-tab ${activeTab === "details" ? "active" : ""}`}
        onClick={() => onTabChange("details")}
      >
        <BookOpenText size={18} />
        <span>详情{evidenceCount > 0 ? ` (${evidenceCount})` : ""}</span>
      </button>
    </nav>
  );
}
```

- [ ] **Step 2: Add mobile tab state and drawer rendering to App.tsx**

In `frontend/src/App.tsx`, add imports:

```tsx
import { MobileTabBar } from "./components/MobileTabBar";
import type { MobileTab } from "./components/MobileTabBar";
```

Add state:

```tsx
const [activeMobileTab, setActiveMobileTab] = useState<MobileTab>("chat");
```

Update the render to show mobile drawer below 820px. The approach: always render both panels, but use CSS to show/hide based on viewport and tab:

```tsx
{/* Desktop: three-column shell */}
{/* Mobile: chat panel + bottom tab bar + drawer */}
<div className={`shell ${contextPanelCollapsed ? "shell-collapsed" : ""}`}>
  <Sidebar ... />
  <div className="chat-area">
    <ChatPanel ... onProductClick={handleProductClick} />
    <MobileTabBar
      activeTab={activeMobileTab}
      onTabChange={setActiveMobileTab}
      productCount={products.length}
      evidenceCount={evidence.length}
    />
  </div>
  <ContextPanel
    ...
    highlightedProductId={highlightedProductId}
    mobileTab={activeMobileTab}
  />
  <button
    type="button"
    className="panel-toggle"
    onClick={() => setContextPanelCollapsed((c) => !c)}
    title={contextPanelCollapsed ? "展开详情" : "收起详情"}
  >
    {contextPanelCollapsed ? <PanelRightOpen size={18} /> : <PanelRightClose size={18} />}
  </button>
</div>
```

- [ ] **Step 3: Add mobile drawer and tab bar CSS**

Add to `frontend/src/styles.css`:

```css
/* --- Mobile Tab Bar -------------------------------------------------------- */
.mobile-tab-bar {
  display: none;
  border-top: 1px solid var(--border-default);
  background: var(--bg-panel);
  padding: var(--space-1) 0;
  position: sticky;
  bottom: 0;
  z-index: 10;
}

.mobile-tab-bar {
  display: none;
}

.chat-area {
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

/* --- Panel Toggle Desktop Position ----------------------------------------- */
@media (min-width: 821px) {
  .mobile-tab-bar {
    display: none !important;
  }
}

@media (max-width: 820px) {
  .shell {
    grid-template-columns: 1fr;
    grid-template-rows: auto 1fr;
  }

  .sidebar {
    height: auto;
    border-width: 0 0 1px;
  }

  .chat-area {
    height: 100vh;
    height: 100dvh;
    max-height: 100vh;
    max-height: 100dvh;
  }

  .mobile-tab-bar {
    display: flex;
    justify-content: space-around;
    align-items: center;
  }

  .mobile-tab {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 2px;
    padding: var(--space-2);
    border: 0;
    background: transparent;
    color: var(--text-muted);
    font-size: 11px;
    transition: color var(--duration-fast) var(--ease-out);
  }

  .mobile-tab.active {
    color: var(--copper);
  }

  .mobile-tab:hover {
    color: var(--text-primary);
  }

  .context-panel {
    display: none;
    position: fixed;
    bottom: 52px;
    left: 0;
    right: 0;
    max-height: 60vh;
    border-radius: var(--radius-lg) var(--radius-lg) 0 0;
    border-width: 1px 0 0;
    box-shadow: 0 -4px 16px rgba(0, 0, 0, 0.3);
    z-index: 20;
    overflow-y: auto;
  }

  .context-panel.mobile-visible {
    display: flex;
    animation: slide-up var(--duration-med) var(--ease-out);
  }

  .panel-toggle {
    display: none;
  }

  .conversation-section {
    max-height: 200px;
  }

  .chat-panel {
    height: 78vh;
    max-height: none;
  }

  .topbar {
    align-items: flex-start;
    flex-direction: column;
  }

  .status-stack {
    justify-content: flex-start;
  }

  .message,
  .message.user,
  .error-card {
    max-width: 100%;
  }

  .metrics-strip {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}

@keyframes slide-up {
  from {
    transform: translateY(100%);
  }
  to {
    transform: translateY(0);
  }
}
```

- [ ] **Step 4: Update ContextPanel to accept mobileTab prop and conditionally show**

In `frontend/src/components/ContextPanel.tsx`, add `mobileTab` prop:

```tsx
type ContextPanelProps = {
  // ...existing props...
  mobileTab?: "chat" | "products" | "details";
};
```

In the component, when `mobileTab` is provided (mobile view), filter sections:

```tsx
const showProducts = mobileTab === "products";
const showDetails = mobileTab === "details";

// In the return JSX, add className for mobile visibility:
<aside className={`context-panel ${mobileTab === "products" || mobileTab === "details" ? "mobile-visible" : ""}`}>
```

For mobile, only show the relevant section:
- `mobileTab === "products"` → show metrics + products section
- `mobileTab === "details"` → show boundary + evidence + order + handoff sections
- `mobileTab === "chat"` or `mobileTab === undefined` (desktop) → show everything

- [ ] **Step 5: Verify build**

```bash
cd /Users/loda/Desktop/pc-agent/frontend && npm run build
```

Expected: Build succeeds.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/MobileTabBar.tsx frontend/src/App.tsx frontend/src/components/ContextPanel.tsx frontend/src/styles.css
git commit -m "feat: mobile bottom tab bar and context panel drawer for small screens"
```

---

### Task 11: Final integration, visual verification, and cleanup

**Files:**
- All modified files from previous tasks
- `frontend/src/styles.css` (responsive adjustments, final polish)

**Interfaces:**
- Consumes: All previous task outputs
- Produces: Complete working frontend with MD rendering, product cards, layout optimization

- [ ] **Step 1: Run full TypeScript check**

```bash
cd /Users/loda/Desktop/pc-agent/frontend && npx tsc --noEmit
```

Expected: No type errors.

- [ ] **Step 2: Run full Vite build**

```bash
cd /Users/loda/Desktop/pc-agent/frontend && npm run build
```

Expected: Build succeeds with no warnings.

- [ ] **Step 3: Start dev server and visually verify**

```bash
cd /Users/loda/Desktop/pc-agent/frontend && npm run dev
```

Open browser to `http://localhost:5173`. Verify:
1. Assistant messages render with Markdown (bold, lists, tables)
2. Product cards appear below assistant messages that have products
3. Product cards show category icon placeholders when no image_url
4. Context panel product cards have 56px thumbs with icon placeholders
5. Sidebar conversations grouped by date
6. Sidebar delete button slides in on hover
7. Right panel collapse toggle works
8. Mobile view (<820px) shows bottom tab bar and drawer
9. Clicking a product card in chat highlights it in the context panel

- [ ] **Step 4: Test edge cases**

1. Send a message that triggers out-of-scope boundary → no product cards should appear
2. Send a message with streaming response → MD should render incrementally
3. Resize browser from desktop to mobile width → tab bar should appear
4. Click context panel toggle → panel should collapse/expand
5. Empty product array → ProductCardRow should render nothing

- [ ] **Step 5: Remove unused import (PackageSearch in ChatPanel if applicable)**

Check `ChatPanel.tsx` for any unused imports after the refactoring. Remove any icons that are no longer used.

- [ ] **Step 6: Final commit**

```bash
git add -A
git commit -m "feat: final integration polish for MD rendering, product cards, and layout optimization"
```