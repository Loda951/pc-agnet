import { ProductCard } from "../types";
import { getCategoryIcon, getCategoryThumbClass } from "../utils/category-icon";

type ProductInlineCardProps = {
  product: ProductCard;
  onClick?: () => void;
};

export function ProductInlineCard({ product, onClick }: ProductInlineCardProps) {
  const IconComponent = getCategoryIcon(product.category);
  const thumbClass = getCategoryThumbClass(product.category);
  const specLine = Object.entries(product.specs)
    .slice(0, 3)
    .map(([key, value]) => `${key}: ${value}`)
    .join(" · ");

  function handleKeyDown(event: React.KeyboardEvent) {
    if (onClick && (event.key === "Enter" || event.key === " ")) {
      event.preventDefault();
      onClick();
    }
  }

  return (
    <article
      className="product-inline-card"
      onClick={onClick}
      onKeyDown={onClick ? handleKeyDown : undefined}
      role={onClick ? "button" : undefined}
      tabIndex={onClick ? 0 : undefined}
    >
      <div className={`product-inline-thumb ${thumbClass}`}>
        {product.image_url ? (
          <img src={product.image_url} alt={product.title} loading="lazy" />
        ) : (
          <IconComponent size={24} />
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