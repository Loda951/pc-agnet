## 1. 当前任务目标（一句话概括）

- 构建 PC 外设商城电商客服 AI Agent 全栈 MVP，支持商品推荐、订单查询、长期记忆、RAG 预留和退换货工单。

## 2. 已排查过的方案（列出方案名称 + 放弃原因）

- 直接读取 Gemini 分享页完整建表内容：放弃原因是页面只稳定暴露标题，核心 SQL 后续改用用户粘贴的文本附件。
- 直接照搬 MySQL DDL：放弃原因是项目本地数据库选 PostgreSQL，需要将 `AUTO_INCREMENT/json/datetime/COMMENT` 等方言迁移为 PostgreSQL 写法。
- 将 `docyx/pc-part-dataset` 当完整商城数据库：放弃原因是该数据集只有商品参数和美元价格，缺少库存、中文详情、订单、物流和售后数据。
- 本地运行 LLM：放弃原因是用户明确选择 LLM 调接口，当前默认使用 DeepSeek。
- 立即用容器编排验证迁移和 seed：放弃原因是当时环境没有可用容器运行时，无法启动 PostgreSQL/Redis/Chroma。
- 使用 `podman compose` 启动本地服务：放弃原因是当前机器没有 compose provider，改用原生 Podman 脚本。
- 未授权安装前端依赖：放弃原因是普通 `npm install` 卡在网络下载阶段，后续用提升权限完成。
- 未授权安装后端依赖：放弃原因是普通 pip 安装因 DNS/网络受限失败，后续用提升权限完成。

## 3. 关键文件和模块关系（仅限本轮修改或引用的文件）

- `compose.yml`：保留本地 PostgreSQL、Redis、ChromaDB 编排参考。
- `scripts/podman-infra.sh`：使用原生 Podman 启停 PostgreSQL、Redis、ChromaDB，不依赖 compose provider。
- `.env.example`：统一配置 `DATABASE_URL`、`REDIS_URL`、`CHROMA_HOST/PORT`、`LLM_PROVIDER`、`LLM_API_KEY`、`LLM_MODEL`。
- `backend/app/main.py`：FastAPI 入口，挂载 health、chat、catalog、orders、after-sales 路由。
- `backend/app/core/config.py`：读取环境配置，供数据库、LLM、API 路由使用。
- `backend/app/core/database.py`：创建 SQLAlchemy async engine/session，被 repositories 和 scripts 复用。
- `backend/app/core/llm.py`：根据 `LLM_PROVIDER` 选择 Qwen 或 DeepSeek OpenAI-compatible base URL。
- `backend/app/models/commerce.py`：实现用户、分类、品牌、EAV 属性、SPU/SKU、订单、物流模型。
- `backend/app/models/conversation.py`：实现会话、消息、Agent run、工具调用、长期记忆、知识文档模型。
- `backend/app/models/support.py`：实现退换货工单和售后事件模型。
- `backend/alembic/versions/0001_initial_schema.py`：PostgreSQL 初始迁移，覆盖核心商城表、Agent 表、售后表。
- `backend/app/repositories/catalog.py`：商品检索接口，被 Agent 和 `/api/catalog/search` 调用。
- `backend/app/repositories/orders.py`：订单/物流查询接口，被 Agent、订单 API、售后校验复用。
- `backend/app/repositories/support.py`：创建退换货工单，依赖订单明细归属校验。
- `backend/app/repositories/conversations.py`：会话、消息、Agent run、工具调用、长期记忆持久化。
- `backend/app/agent/graph.py`：LangGraph 状态机，串起上下文加载、意图识别、工具检索、回复生成、持久化。
- `backend/app/agent/intent.py`：规则意图识别与商品/订单槽位提取。
- `backend/app/services/dataset_mapper.py`：把 pc-part-dataset JSON 记录映射为本地商品导入对象。
- `backend/scripts/seed_demo.py`：生成 demo 用户、商品、属性、订单、物流、知识库文档。
- `backend/scripts/import_pc_part_dataset.py`：从 pc-part-dataset JSON 文件导入商品种子数据。
- `frontend/src/App.tsx`：客服工作台主界面，包含聊天、商品结果、订单上下文、售后工单区域。
- `frontend/src/api.ts`：封装前端调用 `/api/chat` 和 `/api/after-sales`。
- `frontend/src/types.ts`：前端类型定义，与后端响应 schema 对应。
- 本地 Podman 验证：PostgreSQL/Redis/ChromaDB 已启动，`/api/health` 返回 ok，demo seed 已导入。
- 端到端验证：真实 DeepSeek `/api/chat` 商品推荐和订单查询已通过，售后创建 demo 工单 `#1` 已通过。
- `/Users/loda/.codex/attachments/d5037dd7-31fa-43a4-9c7d-0369c6281d43/pasted-text.txt`：用户提供的 MySQL 风格核心建表语句参考。
- `https://github.com/docyx/pc-part-dataset`：已确认可作为商品种子数据来源。

## 4. 已做改动（列出文件路径 + 改动摘要）

- `.gitignore`：忽略 `.env`、Python/Node 构建缓存、虚拟环境、前端 dist、本地数据目录等。
- `.env.example`：新增本地服务、DeepSeek 默认 LLM provider、CORS、默认用户配置模板。
- `README.md`：新增项目说明、MVP 能力、快速启动、数据集和 LLM 配置说明。
- `compose.yml`：新增 PostgreSQL、Redis、ChromaDB 本地编排参考。
- `scripts/podman-infra.sh`：新增原生 Podman 本地基础设施脚本。
- `backend/pyproject.toml`：新增后端依赖、dev 依赖、pytest/ruff/setuptools 配置。
- `backend/alembic.ini`：新增 Alembic 配置。
- `backend/alembic/env.py`：新增异步 SQLAlchemy Alembic 环境。
- `backend/alembic/versions/0001_initial_schema.py`：新增 PostgreSQL 初始 schema 迁移，并拆分 DDL 以兼容 asyncpg。
- `backend/app/core/config.py`：新增 pydantic-settings 配置读取，支持逗号分隔和 JSON 数组格式的 CORS 配置。
- `backend/app/core/database.py`：新增 async SQLAlchemy session 工厂。
- `backend/app/core/llm.py`：新增 Qwen/DeepSeek OpenAI-compatible ChatOpenAI 构建逻辑。
- `backend/app/models/*.py`：新增商城、会话、记忆、售后领域模型。
- `backend/app/schemas/*.py`：新增商品、订单、售后、聊天响应 schema。
- `backend/app/repositories/*.py`：新增商品检索、订单查询、售后工单、会话记忆数据访问模块。
- `backend/app/agent/*.py`：新增 LangGraph Agent 状态、意图识别、提示词、主状态机。
- `backend/app/api/routers/*.py`：新增 health、chat、catalog、orders、after-sales API。
- `backend/app/main.py`：新增 FastAPI 应用入口与 CORS/router 挂载。
- `backend/app/services/dataset_mapper.py`：新增 pc-part-dataset 导入映射逻辑。
- `backend/scripts/seed_demo.py`：新增 demo 数据生成脚本，使用 naive UTC datetime 匹配当前模型列。
- `backend/scripts/import_pc_part_dataset.py`：新增外部 JSON 数据集导入脚本。
- `backend/tests/test_config.py`：新增环境配置解析测试。
- `backend/tests/test_dataset_mapper.py`：新增数据集映射单元测试。
- `frontend/package.json`：新增 React/Vite/TypeScript/lucide 依赖与构建脚本。
- `frontend/package-lock.json`：锁定前端依赖版本。
- `frontend/tsconfig*.json`：新增前端 TypeScript 配置。
- `frontend/vite.config.ts`：新增 Vite React 配置。
- `frontend/index.html`：新增前端 HTML 入口。
- `frontend/src/App.tsx`：新增客服工作台 UI。
- `frontend/src/api.ts`：新增前端 API 调用封装。
- `frontend/src/main.tsx`：新增 React 挂载入口。
- `frontend/src/styles.css`：新增响应式工作台样式。
- `frontend/src/types.ts`：新增前端共享类型。
- `docs/codex-context-主线-1.md`：新增本轮上下文整理文档。

## 5. 未完成事项（列出具体待办项，每项不超过一行）

- 将 ChromaDB RAG 写入和检索节点接入当前 LangGraph。
- 增加更多订单/售后/Agent 集成测试。
- 从 `docyx/pc-part-dataset` 下载并导入 mouse/keyboard/headphones 等真实 JSON 数据。

## 6. 不要重复尝试的方向（列出方向 + 失败原因）

- 再用旧的 `docker compose up` 启动本地服务：失败原因是项目已迁移到 Podman。
- 再依赖 `podman compose` 启动本地服务：失败原因是当前机器没有 compose provider，项目已改用原生 Podman 脚本。
- 再用普通 `npm install` 长时间等待：失败原因是网络下载会卡住，需要提升权限。
- 再用普通 pip 安装后端依赖：失败原因是 DNS/网络受限，需要提升权限。
- 再直接用 MySQL 原始 DDL 跑 PostgreSQL：失败原因是方言不兼容，必须用 Alembic 里的 PostgreSQL 迁移。
- 再把 pc-part-dataset 当订单或售后数据源：失败原因是它只覆盖 PC 商品参数和价格。
- 再从 Gemini 分享页抽完整 SQL：失败原因是页面没有稳定暴露对话正文，已改用用户粘贴附件。

## 7. 下一步建议（优先级排序，每条含执行动作）

- P0：在前端 `http://localhost:5173/` 测试商品推荐、最近订单查询、创建售后工单。
- P1：下载 `docyx/pc-part-dataset` 的 `mouse.json`、`keyboard.json`、`headphones.json`，用 `scripts/import_pc_part_dataset.py` 导入。
- P2：把 `knowledge_document` 写入 ChromaDB，并在 LangGraph 中加入 RAG 检索节点。
- P2：补充 FastAPI 集成测试，覆盖 chat、orders、after-sales 的数据库路径。
