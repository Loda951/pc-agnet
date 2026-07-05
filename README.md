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

## 本地依赖

本项目本地开发需要先准备：

- Python 3.11+
- Node.js 20+ 和 npm
- Git，可选，仅用于下载旧版外部商品数据集
- Make，用于执行仓库里的初始化命令
- Podman，用于启动 PostgreSQL、Redis、ChromaDB

macOS 首次使用 Podman 时，先执行：

```bash
podman machine init
podman machine start
```

## 快速启动

1. 复制环境变量：

   ```bash
   cp .env.example .env
   ```

2. 一键初始化本地环境：

   ```bash
   make setup-local
   ```

   该命令会完成后端和数据侧初始化：

   - 启动 PostgreSQL、Redis、ChromaDB
   - 安装后端依赖
   - 执行 Alembic migration，在 PostgreSQL 中创建/升级表结构
   - 导入 6 个外设类目的紧凑商品目录
   - 写入 demo 用户、登录凭据、示例订单和知识文档
   - 将 PostgreSQL 里的知识文档同步到 ChromaDB

3. 启动后端：

   ```bash
   cd backend
   .venv/bin/uvicorn app.main:app --reload
   ```

4. 另开一个终端，从仓库根目录启动前端：

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

如果需要分步调试，可以按下面流程手动初始化。注意：PostgreSQL 的表不是由数据导入脚本创建的，而是由 Alembic migration 创建；导入脚本只负责往已经存在的表里写数据。

1. 启动 Podman 本地基础设施：

   ```bash
   ./scripts/podman-infra.sh up
   ```

   这一步会创建或启动 PostgreSQL、Redis、ChromaDB 容器。PostgreSQL 容器启动后只有数据库实例，不会自动创建业务表。

2. 安装后端依赖：

   ```bash
   cd backend
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -e ".[dev]"
   ```

3. 创建或升级 PostgreSQL 表结构：

   ```bash
   alembic upgrade head
   ```

   这一步会执行 `backend/alembic/versions/` 下的 migration，创建/升级 `app_user`、商品、订单、会话、记忆、知识文档、售后、鉴权凭据和 session 等表。重复执行是安全的；已经在最新版本时不会重复建表。

4. 导入紧凑商品目录：

   ```bash
   cd ..
   make data-import
   ```

   这一步会导入 6 个类目：鼠标、键盘、耳机、显示器、音箱、摄像头。默认规模为
   24 个类目-品牌组合、192 个 SPU、2304 个 SKU。

5. 写入本地 demo 数据：

   ```bash
   cd backend
   python -m scripts.seed_demo
   ```

   这一步会写入：

   - 演示用户和登录凭据：`demo@example.com` / `demo-password`
   - 一个示例订单、订单明细和物流轨迹
   - 售后政策、FAQ、店铺规则和外设知识文档

6. 同步知识库 RAG 索引：

   ```bash
   cd backend
   python -m scripts.sync_knowledge
   ```

   这一步会读取 PostgreSQL 的 `knowledge_document`，同步到 ChromaDB collection，供 Agent 检索并输出 evidence。

7. 启动后端：

   ```bash
   uvicorn app.main:app --reload
   ```

8. 另开一个终端，从仓库根目录启动前端：

   ```bash
   cd frontend
   npm install
   npm run dev
   ```

## 数据初始化与同步说明

本地数据初始化分为三类：建表、填 PostgreSQL 数据、同步 ChromaDB 索引。

| 步骤 | 命令 | 写入位置 | 作用 |
| --- | --- | --- | --- |
| 启动基础设施 | `make infra-up` | Podman 容器和 volume | 启动 PostgreSQL、Redis、ChromaDB |
| 创建/升级表 | `make db-migrate` | PostgreSQL schema | 执行 Alembic migration，创建业务表和索引 |
| 导入紧凑商品 | `make data-import` | PostgreSQL rows | 写入 6 个外设类目的受控商品目录 |
| 写 demo 数据 | `make db-seed` | PostgreSQL rows | 写 demo 用户、凭据、订单、物流、知识文档 |
| 下载旧外部数据集 | `make dataset` | `.cache/pc-part-dataset` | clone `docyx/pc-part-dataset` |
| 导入旧外部商品 | `make legacy-data-import` | PostgreSQL rows | 将 GitHub JSON 商品数据导入商品和属性表 |
| 同步知识库 | `make knowledge-sync` | ChromaDB collection | 将 `knowledge_document` 同步为 RAG 向量索引 |

### PostgreSQL 表是怎么建的？

是跑脚本建的，但准确说是跑 Alembic migration：

```bash
cd backend
.venv/bin/alembic upgrade head
```

这一步会读取 `backend/alembic/versions/` 下的 migration 文件并创建表。`scripts.import_compact_catalog`、`scripts.seed_demo`、`scripts.import_pc_part_dataset`、`scripts.sync_knowledge` 都不负责建表；它们假设 migration 已经执行完成。

如果本地数据库是全新的，推荐顺序是：

```bash
./scripts/podman-infra.sh up
cd backend
.venv/bin/alembic upgrade head
cd ..
make data-import
make db-seed
make knowledge-sync
```

如果需要完全重置本地数据，可以删除 Podman volume 后重跑：

```bash
CONFIRM_RESET=1 ./scripts/podman-infra.sh reset
make setup-local
```

### 紧凑商品目录导入

默认 `make data-import` 使用 `backend/scripts/import_compact_catalog.py` 生成并导入受控目录：

- 6 个类目：鼠标、键盘、耳机、显示器、音箱、摄像头
- 每个类目 4 个品牌，合计 24 个类目-品牌组合
- 每个品牌 8 个 SPU
- 每个 SPU 12 个 SKU

```bash
make data-import
```

也可以只做生成预览，不写数据库：

```bash
cd backend
.venv/bin/python -m scripts.import_compact_catalog --dry-run
```

### GitHub 商品数据集导入（可选）

`docyx/pc-part-dataset` 是真实商品种子数据来源。导入适配器会把 JSON 里的 `name`、`price`、`color`、`connection_type`、`max_dpi`、`switches`、`wireless`、`microphone` 等字段映射为本项目的 `spu/sku + attribute_key/value + goods_attribute_relation` 模型。

如果需要旧版外部大数据集，可以手动 clone 到 `.cache/pc-part-dataset`，再导入 Makefile 中配置的 `PART_TYPES`：

```bash
make dataset
make legacy-data-import
```

如果只想导入核心外设数据，可以覆盖 `PART_TYPES`：

```bash
make legacy-data-import PART_TYPES=headphones,keyboard,mouse
```

如果想先小批量导入验证，可以直接调用脚本：

```bash
cd backend
.venv/bin/python -m scripts.import_pc_part_dataset ../.cache/pc-part-dataset/data/json --part-types headphones,keyboard,mouse --limit 500
```

如需使用已有数据集路径：

```bash
make legacy-data-import DATASET_DIR=/path/to/pc-part-dataset
```

该 GitHub 数据集主要提供商品和规格信息，不包含本项目需要的用户、登录凭据、订单、物流、售后记录或中文客服知识。因此这些本地演示数据仍由 `python -m scripts.seed_demo` 写入。

### 知识库 RAG 同步

知识库 RAG 的权威数据源是 PostgreSQL 的 `knowledge_document` 表。`scripts.seed_demo` 会先写入本地 demo 知识文档，然后用下面命令同步到 ChromaDB：

```bash
make knowledge-sync
```

也可以手动执行：

```bash
cd backend
.venv/bin/python -m scripts.sync_knowledge
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
