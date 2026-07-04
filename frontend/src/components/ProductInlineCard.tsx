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