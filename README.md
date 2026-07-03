# PC Agent

PC 外设商城电商客服 AI Agent。后端使用 FastAPI + LangGraph + LangChain + PostgreSQL + ChromaDB + Redis，前端使用 React + Vite。

## MVP 能力

- 商品推荐与参数问答：鼠标、键盘、耳机、显示器等 PC 外设。
- 商品对比：基于 SKU 规格、筛选属性和价格。
- 订单查询：订单、订单明细、物流状态。
- 多用户鉴权：登录、会话恢复、刷新、登出，并按当前认证用户隔离订单、会话和记忆。
- 售后说明：退换货、维修、保修等 read-only 政策问答，办理类请求转人工。
- 知识库 RAG：从 PostgreSQL `knowledge_document` 同步到 ChromaDB，并在回答中输出依据。
- 长期记忆：记录用户偏好，后续推荐时可复用。
- 本地数据服务：PostgreSQL、Redis、ChromaDB 通过 Podman 在本地跑；LLM 默认调 DeepSeek 的 OpenAI-compatible 接口。

## 快速启动

1. 复制环境变量：

   ```bash
   cp .env.example .env
   ```

2. 一键初始化本地环境：

   ```bash
   make setup-local
   ```

   该命令会启动 Podman 基础设施、安装后端依赖、执行 Alembic migration、写入 demo 数据、下载 `docyx/pc-part-dataset` 到 `.cache/pc-part-dataset`、导入真实商品数据到 PostgreSQL，并同步知识库 RAG 到 ChromaDB。

   macOS 首次使用 Podman 时，先执行 `podman machine init` 和 `podman machine start`。

3. 启动后端：

   ```bash
   cd backend
   .venv/bin/uvicorn app.main:app --reload
   ```

4. 启动前端：

   ```bash
   cd frontend
   npm install
   npm run dev
   ```

5. 使用本地演示账号登录：

   ```text
   demo@example.com
   demo-password
   ```

## 手动初始化

如果需要分步调试，可以按下面流程手动初始化。

1. 启动 Podman 本地基础设施：

   ```bash
   ./scripts/podman-infra.sh up
   ```

2. 初始化后端：

   ```bash
   cd backend
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -e ".[dev]"
   alembic upgrade head
   python -m scripts.seed_demo
   python -m scripts.sync_knowledge
   uvicorn app.main:app --reload
   ```

3. 启动前端：

   ```bash
   cd frontend
   npm install
   npm run dev
   ```

## 数据集

`docyx/pc-part-dataset` 可作为商品种子数据来源。导入适配器会把 JSON 里的 `name/price/color/connection_type/max_dpi/switches/wireless/microphone` 等字段映射为本项目的 `spu/sku + attribute_key/value + goods_attribute_relation` 模型。该数据集缺少库存、订单、售后和中文详情，因此本项目会生成本地 demo 库存与客服数据。

默认一键初始化会把数据集 clone 到 `.cache/pc-part-dataset`：

```bash
make dataset
make data-import
```

如需使用已有数据集路径：

```bash
make data-import DATASET_DIR=/path/to/pc-part-dataset
```

知识库 RAG 数据源仍是 PostgreSQL 的 `knowledge_document`，同步到 ChromaDB：

```bash
make knowledge-sync
```

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

## Auth 配置

本地 demo 会由 `python -m scripts.seed_demo` 写入演示账号。生产或共享环境务必覆盖：

```env
AUTH_TOKEN_SECRET=replace-with-a-random-secret
AUTH_ACCESS_TOKEN_MINUTES=30
AUTH_REFRESH_TOKEN_DAYS=14
```
