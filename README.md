# PC Agent

PC 外设商城电商客服 AI Agent。后端使用 FastAPI + LangGraph + LangChain + PostgreSQL + ChromaDB + Redis，前端使用 React + Vite。

## MVP 能力

- 商品推荐与参数问答：鼠标、键盘、耳机、显示器等 PC 外设。
- 商品对比：基于 SKU 规格、筛选属性和价格。
- 订单查询：订单、订单明细、物流状态。
- 退换货工单：基于订单明细创建退货、换货、维修、退款工单。
- 长期记忆：记录用户偏好，后续推荐时可复用。
- 本地数据服务：PostgreSQL、Redis、ChromaDB 通过 Podman 在本地跑；LLM 默认调 DeepSeek 的 OpenAI-compatible 接口。

## 快速启动

1. 复制环境变量：

   ```bash
   cp .env.example .env
   ```

2. 启动 Podman 本地基础设施：

   ```bash
   ./scripts/podman-infra.sh up
   ```

   macOS 首次使用 Podman 时，先执行 `podman machine init` 和 `podman machine start`。

3. 初始化后端：

   ```bash
   cd backend
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -e ".[dev]"
   alembic upgrade head
   python -m scripts.seed_demo
   uvicorn app.main:app --reload
   ```

4. 启动前端：

   ```bash
   cd frontend
   npm install
   npm run dev
   ```

## 数据集

`docyx/pc-part-dataset` 可作为商品种子数据来源。导入适配器会把 JSON 里的 `name/price/color/connection_type/max_dpi/switches` 等字段映射为本项目的 `spu/sku + attribute_key/value + goods_attribute_relation` 模型。该数据集缺少库存、订单、售后和中文详情，因此本项目会生成本地 demo 库存与客服数据。

## Podman 常用命令

```bash
./scripts/podman-infra.sh ps
./scripts/podman-infra.sh logs postgres
./scripts/podman-infra.sh down
```

`down` 会删除容器但保留本地数据卷。需要重置数据时，执行 `CONFIRM_RESET=1 ./scripts/podman-infra.sh reset`。

## LLM 配置

DeepSeek 示例：

```env
LLM_PROVIDER=deepseek
LLM_API_KEY=sk-...
LLM_MODEL=deepseek-chat
```

Qwen 示例：

```env
LLM_PROVIDER=qwen
LLM_API_KEY=sk-...
LLM_MODEL=qwen-plus
```
