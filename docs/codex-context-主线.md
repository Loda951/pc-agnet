# PC Agent 项目主线文档

## 项目定位与核心价值主张

- 本项目是面向 PC 外设电商场景的客服 AI Agent，核心价值是用结构化商品、订单、物流、售后政策和知识数据，回答高频低风险客服问题。
- 当前 MVP 已从“需求设想”推进到“demo 技术闭环”：本地 Podman 基础设施、PostgreSQL 数据模型、FastAPI API、LangGraph Agent、DeepSeek 调用、React 工作台均已跑通。
- `design.md` 对当前阶段的产品边界定义更保守：定位为 read-only 智能问答客服，不应执行真实业务写操作；当前 MVP 为验证闭环已经实现 demo 售后工单创建，这是一个需要后续收敛的范围差异。
- 项目应优先服务三类价值：降低商品咨询和订单履约查询的人力负担，保证回答基于可追溯业务数据，明确阻断越权、写操作假执行和高风险售后结论。

## 已完成MVP功能清单

### 本地基础设施

- 已完成：通过 `scripts/podman-infra.sh` 使用原生 Podman 启动 PostgreSQL、Redis、ChromaDB。
- 已完成：`/api/health` 可检查 PostgreSQL、Redis、ChromaDB，Chroma 优先使用 `/api/v2/heartbeat`。
- 已完成：`compose.yml` 保留为 Compose 编排参考，但默认不依赖 `podman compose`。

### 后端 API

- 已完成：`/api/chat` 和 `/api/chat/stream`，由 `AgentRuntime` 驱动 LangGraph 流程。
- 已完成：`/api/catalog/search`，支持商品关键词、分类、价格、规格过滤。
- 已完成：`/api/orders/latest` 和 `/api/orders/{order_id}`，按用户 ID 查询订单与物流。
- 已完成：`/api/after-sales`，可创建 demo 售后工单。
- 部分完成：API 使用 `DEFAULT_USER_ID` 和可选 `user_id` 查询参数，没有真实鉴权。

### Agent 能力

- 已完成：基于规则的意图识别，覆盖 `product_recommendation`、`order_status`、`after_sales`、`general`。
- 已完成：商品推荐、最近订单查询、售后意图回复可通过 DeepSeek 生成中文回答。
- 已完成：会话、消息、agent run、工具调用和简单长期记忆持久化。
- 部分完成：多轮上下文依赖 `conversation_id` 和有限偏好记忆，没有完整指代消解。
- 未完成：`design.md` 要求的 `in_scope_auto` / `human_handoff_required` / `out_of_scope` 三态边界分类尚未实现。

### 数据与种子

- 已完成：Alembic 初始 PostgreSQL schema，覆盖用户、商品 EAV、SKU/SPU、订单、物流、会话、工具调用、记忆、知识文档、售后表。
- 已完成：`scripts.seed_demo` 导入 demo 用户、5 个 SKU、1 个订单、2 条知识文档。
- 已完成：`import_pc_part_dataset.py` 和 `dataset_mapper.py` 可将 `docyx/pc-part-dataset` JSON 映射到本地商品模型。
- 未完成：真实 mouse、keyboard、headphones 等数据集尚未导入。

### 前端工作台

- 已完成：React/Vite 单页客服工作台，包含聊天区、商品结果区、订单上下文区、售后工单区。
- 已完成：快捷 prompt、消息输入、商品卡、订单卡、售后创建表单。
- 部分完成：前端 build 通过；浏览器中完整手点 MVP 主流程仍需人工验证。

### 验证

- 已完成：Podman 服务启动、数据库迁移、demo seed、`/api/health`、真实 DeepSeek `/api/chat`、订单查询、售后创建均已验证通过。
- 已完成：`pytest backend/tests` 通过，当前包含配置解析和数据集映射测试。
- 已完成：`ruff check backend` 和 `npm run build` 通过。
- 未完成：缺少覆盖 chat、orders、after-sales 的数据库集成测试。

## 技术架构概览

### 技术栈

- 后端：Python 3.11+、FastAPI、LangGraph、LangChain、LangChain OpenAI-compatible client、SQLAlchemy asyncio、Pydantic v2、Alembic、asyncpg、httpx、redis。
- 前端：React 19、TypeScript、Vite、lucide-react。
- 数据与基础设施：PostgreSQL、Redis、ChromaDB、Podman、本地 `.env` 配置。
- LLM：默认 DeepSeek，`LLM_BASE_URL` 留空时由 `backend/app/core/llm.py` 解析为 `https://api.deepseek.com`；仍保留 Qwen 分支。

### 目录结构

- `backend/app/main.py`：FastAPI 应用入口，挂载 health、chat、catalog、orders、after-sales 路由。
- `backend/app/api/routers/`：HTTP API 层，负责请求/响应和依赖注入。
- `backend/app/agent/`：Agent 状态、意图识别、系统提示词、LangGraph 状态机。
- `backend/app/repositories/`：数据库读取与写入封装，包括商品、订单、售后、会话记忆。
- `backend/app/models/`：SQLAlchemy ORM 模型，按 commerce、conversation、support 拆分。
- `backend/app/schemas/`：Pydantic 请求与响应模型。
- `backend/scripts/`：demo seed 和外部商品数据集导入脚本。
- `frontend/src/`：工作台 UI、API 调用和前端类型定义。
- `scripts/podman-infra.sh`：原生 Podman 本地服务管理脚本。
- `docs/`：阶段上下文和本主线文档。

### 关键依赖和配置

- `.env.example`：展示本地数据库、Redis、Chroma、CORS、DeepSeek/Qwen 配置。
- `.env`：本地真实配置文件，包含 DeepSeek key；必须保持 git ignored。
- `backend/pyproject.toml`：后端依赖、pytest、Ruff 规则；Ruff 行宽 100，启用 `E/F/I/UP/B`。
- `frontend/package.json`：前端 dev/build/preview 命令。

### 数据流

- 前端调用 `frontend/src/api.ts`，默认请求 `http://localhost:8000`。
- `/api/chat` 接收用户问题后进入 `AgentRuntime`。
- LangGraph 流程为 `load_context -> route_intent -> retrieve -> generate -> persist`。
- `route_intent` 使用规则识别意图；`retrieve` 根据意图调用商品或订单 repository。
- `generate` 在有 LLM key 时调用 DeepSeek，否则使用后端 fallback 文案。
- `persist` 写入用户消息、助手消息、agent run、工具调用和简单记忆。
- 售后创建当前不经过 Agent 自动执行，而由前端表单调用 `/api/after-sales` 写入工单。

## 已知问题与遗留债务

- 产品边界债务：`design.md` 明确当前阶段 read-only，但 MVP 已提供 demo 售后工单创建，需要决定保留为 demo-only、改为人工接管入口，还是推进到正式写操作能力。
- 边界分类缺口：尚未实现 `in_scope_auto`、`human_handoff_required`、`out_of_scope` 的统一分类和可观测输出。
- 鉴权缺口：订单和售后只依赖默认用户或 query `user_id`，没有登录态、权限校验、租户隔离或敏感信息保护。
- RAG 缺口：PostgreSQL 有 `knowledge_document`，ChromaDB 已启动，但知识写入、向量检索和 Agent 节点尚未接入。
- Evidence 缺口：LLM 回答接收结构化上下文，但没有显式引用来源、证据 ID 或规则出处。
- 多轮能力有限：当前只保存会话和简单偏好记忆，缺少稳健指代消解、上一款/这个商品承接、多子任务拆分。
- 数据质量有限：demo seed 数据量小，推荐结果可能为空；真实商品数据集尚未批量导入。
- 测试覆盖不足：当前测试主要覆盖配置解析和 dataset mapper，缺少 API 集成测试、Agent 状态机测试、权限/边界测试。
- 错误处理较薄：LLM 超时、DeepSeek 错误、数据库异常、外部服务降级尚未形成统一错误码和用户可读策略。
- Frontend 仍是 demo 工作台：单用户、无登录、无路由、缺少完整 loading/error/retry 体验和浏览器端验收记录。

## 设计目标与当前实现的差距分析（对照 design.md）

### 产品范围

- 设计目标：当前阶段只做 PC 外设电商 read-only 智能问答。
- 当前实现：商品和订单查询基本符合 read-only；售后模块已能创建工单，超出 `design.md` 当前阶段范围。
- 差距判断：售后写操作是最大范围偏差，应优先明确产品决策。

### Must Have 能力

- 商品咨询、推荐、价格、库存问答：部分实现，基于 SKU/SPU/属性检索和 LLM 生成；对比和兼容性问答仍较弱。
- 订单状态、订单内容、物流查询：已实现 demo 路径，能读取最新订单和指定订单。
- 售后政策与流程说明：部分实现，demo 知识文档已入库但未接入 RAG；Agent 对售后更多是提示创建工单。
- FAQ 与店铺知识问答：数据表和 demo 文档存在，但自动检索未实现。
- 多轮上下文承接：部分实现，支持 conversation_id 和偏好记忆；缺少完整指代消解。
- 信息不足时澄清：部分实现，fallback 和 LLM 可能追问，但没有显式澄清状态和规则。
- 三态边界分类：未实现，当前只有业务意图分类。

### Should Have 能力

- 一轮多子任务拆分：未实现专门规划逻辑。
- 图片、PDF、docx 辅助信息提取：未实现。
- 关键事实 evidence 约束：未实现显式 evidence 输出。
- mixed-intent 分段回复：依赖 LLM 自然生成，没有结构化保障。

### Safety 和质量要求

- 设计目标：不假装执行真实操作，不承诺退款/赔付/责任结论，不暴露无权限订单信息。
- 当前实现：系统提示词限制编造事实，但没有统一安全策略；售后工单创建为真实数据库写入；订单权限仅靠默认用户。
- 差距判断：需要在正式扩展前补边界分类、鉴权和写操作确认/人工接管策略。

## 代码约定与开发规范

- Python 使用 4 空格缩进，公共函数尽量保留类型标注。
- 后端遵循 router / schema / repository / service / agent 分层；router 保持薄层，数据库访问放 repository，业务编排放 service 或 agent。
- 新增数据库结构不要修改已合入 migration；应新增 Alembic migration。
- 后端提交前运行 `cd backend && pytest && ruff check .`。
- 前端提交前运行 `cd frontend && npm run build`。
- 本地容器统一使用 `./scripts/podman-infra.sh`，不要新增 Docker 专属命令，也不要默认依赖 `podman compose`。
- 不提交 `.env`、`.env.*`、API key、数据库密码或真实用户数据。
- DeepSeek key 只放本地 `.env` 的 `LLM_API_KEY`；`LLM_BASE_URL` 默认留空即可。
- React 组件使用 PascalCase，变量、hook、状态名使用 camelCase。
- 测试文件命名 `test_*.py`，测试函数命名 `test_*`。

## Post-MVP 功能开发方向建议

### P0：收敛 read-only 边界与人工接管策略

- 目标：实现 `in_scope_auto` / `human_handoff_required` / `out_of_scope` 统一边界分类，并决定售后创建是降级为人工接管入口还是升级为正式可写 workflow。
- 对核心价值的影响：直接决定系统可信度和安全边界，是从 demo 走向业务可用的前置条件。
- 技术复杂度评估：中等；需要新增边界分类模块、响应 schema、Agent 路由分支、前端展示和测试。
- 与现有架构的衔接方式：扩展 `backend/app/agent/intent.py` 或新增 boundary classifier，在 `route_intent` 前后加入边界节点；前端根据分类显示自动回答、人工接管或拒答说明。

### P0：接入知识库 RAG 与 evidence 输出

- 目标：把 `knowledge_document` 写入 ChromaDB，新增检索节点，让售后政策、FAQ、店铺规则和外设知识回答带可追溯依据。
- 对核心价值的影响：显著提升事实可信度，补齐 `design.md` 中政策/FAQ/知识问答和 evidence 质量要求。
- 技术复杂度评估：中等偏高；需要 embeddings/provider 决策、Chroma collection 管理、检索结果 schema、prompt 注入和回归测试。
- 与现有架构的衔接方式：在 LangGraph 的 `retrieve` 与 `generate` 之间加入 `retrieve_knowledge` 节点，复用 PostgreSQL `knowledge_document` 作为元数据源，Chroma 保存向量索引。

### P1：导入真实商品数据并强化推荐/对比/兼容性

- 目标：从 `docyx/pc-part-dataset` 导入 mouse、keyboard、headphones 等真实数据，增强筛选、排序、对比和兼容性解释。
- 对核心价值的影响：直接改善商品推荐质量，减少 demo 数据过少导致的空结果。
- 技术复杂度评估：中等；mapper 已有基础，但需要数据清洗、分类属性规范、搜索排序和验收样例。
- 与现有架构的衔接方式：扩展 `dataset_mapper.py`、`import_pc_part_dataset.py` 和 `CatalogRepository`，必要时新增属性标准化表或搜索权重逻辑。

### P1：补齐集成测试与可回归验收集

- 目标：覆盖 `/api/chat`、`/api/orders`、`/api/after-sales`、边界分类、RAG 检索和关键 prompt 行为。
- 对核心价值的影响：降低后续改 Agent 和 schema 时的回归风险。
- 技术复杂度评估：中等；需要测试数据库策略、LLM mock、固定 seed fixture 和 API client。
- 与现有架构的衔接方式：在 `backend/tests/` 新增 FastAPI async client 测试，mock `build_chat_model`，复用 Alembic/seed 初始化测试数据。

### P2：前端工作台产品化

- 目标：把 demo UI 打磨为可操作的客服工作台，补登录态占位、错误重试、证据展示、人工接管提示和多轮上下文可视化。
- 对核心价值的影响：提升验收和演示质量，但依赖后端边界、RAG、数据质量先稳定。
- 技术复杂度评估：中等；主要是状态管理、交互设计和 API 契约调整。
- 与现有架构的衔接方式：在 `frontend/src/App.tsx` 拆分组件，引入更明确的响应状态和 evidence/boundary 字段展示。

### 最优先推荐

- 第一优先：收敛 read-only 边界与人工接管策略。理由是它直接修正 `design.md` 与当前 MVP 最大偏差，避免继续在不清晰边界上扩展写操作。
- 第二优先：接入知识库 RAG 与 evidence 输出。理由是它补齐问答产品的可信度基础，并能支撑售后政策、FAQ、店铺规则等高频客服场景。
