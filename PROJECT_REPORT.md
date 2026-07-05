# PC Agent 项目汇报材料

> 面向 PC 外设商城的客服 AI Agent，覆盖商品推荐、订单查询、售后政策说明和人工接管留痕。  
> 当前文档用于导师汇报，重点说明技术栈、系统流程、数据模型、核心创新、阶段思考、后续优化和 demo prompt 路径。

## 1. 项目定位

本项目是一个 PC 外设电商客服 AI Agent，目标不是简单接一个大模型聊天框，而是把大模型放进真实业务边界里：它能读取商品、订单、物流、售后政策和知识库数据，自动回答低风险问题；遇到退款、退货、维修、订单修改等需要人工确认或业务写操作的请求时，不假装办理，而是转为人工接管记录。

项目当前已经从 MVP 走到第二阶段可信工作台雏形：

- 商品咨询：支持鼠标、键盘、耳机等外设推荐、筛选、对比和规格解释。
- 订单查询：支持当前认证用户的最近订单、指定订单、订单明细和物流状态查询。
- 售后问答：支持售后政策、保修、发票、发货、价保等知识库问答，并返回依据。
- 安全边界：通过 `in_scope_auto`、`human_handoff_required`、`out_of_scope` 三态分类控制自动回答范围。
- 人工接管：售后办理类请求会创建 `handoff_request` 留痕，状态为 `pending`，但不会自动执行退款、退货、维修或订单修改。
- 多用户隔离：登录、刷新、登出、当前用户依赖已落地，订单、会话、记忆和人工接管记录按认证用户隔离。
- 前端工作台：支持登录、会话侧栏、SSE 真流式回答、商品/订单/依据/接管面板。

## 2. 技术栈

### 后端

- Python 3.11
- FastAPI
- LangGraph
- LangChain OpenAI-compatible client
- SQLAlchemy asyncio
- Pydantic v2
- Alembic
- asyncpg
- Redis
- ChromaDB
- DeepSeek OpenAI-compatible API

### 前端

- React 19
- TypeScript
- Vite
- lucide-react
- Fetch + ReadableStream 解析 POST SSE

### 数据与基础设施

- PostgreSQL：业务主库，保存用户、商品、订单、会话、消息、记忆、知识文档、售后和接管记录。
- ChromaDB：知识库向量索引。
- Redis：本地基础设施预留，可用于缓存、限流或会话增强。
- Podman：本地 PostgreSQL、Redis、ChromaDB 统一通过 `scripts/podman-infra.sh` 启动。
- 真实商品数据：通过 `docyx/pc-part-dataset` 导入 mouse、keyboard、headphones 等外设数据。

## 3. 系统架构

整体采用前后端分离和后端分层架构：

```text
React 工作台
  -> frontend/src/api.ts
  -> FastAPI /api/*
  -> router 层
  -> service / repository / agent
  -> PostgreSQL / ChromaDB / Redis / LLM
```

后端关键目录：

- `backend/app/main.py`：FastAPI 入口，挂载 health、auth、conversations、chat、catalog、orders、after-sales。
- `backend/app/api/routers/`：HTTP 路由层，负责请求响应和依赖注入。
- `backend/app/agent/`：Agent 状态、意图识别、边界分类、提示词、LangGraph 流程。
- `backend/app/repositories/`：商品、订单、知识库、会话、售后等数据访问封装。
- `backend/app/services/`：认证、知识库 RAG、商品数据映射等业务服务。
- `backend/app/models/`：SQLAlchemy ORM 模型。
- `backend/alembic/versions/`：数据库 migration。

前端关键目录：

- `frontend/src/App.tsx`：工作台主状态和聊天流程。
- `frontend/src/api.ts`：API、认证 token、SSE 解析和错误处理。
- `frontend/src/components/`：登录页、聊天区、侧栏、上下文面板、边界状态、商品卡等组件。

## 4. 核心数据模型

### 用户与鉴权

- `app_user`：用户基础信息、登录标识、账号状态、最近登录时间。
- `user_auth_credential`：登录标识和密码哈希。
- `user_session`：refresh token 哈希、过期时间、撤销状态。

设计重点是所有敏感业务入口只信任 `get_current_user` 解析出的当前用户，不再接受前端传入的公开 `user_id`。

### 商品模型

- `category`：分类，如鼠标、键盘、耳机。
- `brand`：品牌。
- `spu`：商品主体。
- `sku`：具体可售 SKU，包含价格、库存、规格 JSON、图片 URL。
- `attribute_key` / `attribute_value` / `goods_attribute_relation`：EAV 属性模型，用于承载真实数据集中的动态规格，例如连接方式、DPI、轴体、是否无线、是否带麦克风。

这个模型适合外设商品，因为不同品类的属性差异很大，鼠标关注 DPI 和握持方向，键盘关注轴体和布局，耳机关注无线、麦克风和封闭类型。

### 订单与物流

- `order_info`：订单主体，按 `user_id` 隔离。
- `order_item`：订单明细。
- `order_logistics`：物流公司、物流单号、轨迹 JSON。

订单查询 repository 会始终带上当前认证用户 ID，用户 B 无法读取用户 A 的订单。

### 会话、运行记录与记忆

- `conversation`：会话。
- `message`：用户和助手消息，metadata 保存 intent、boundary、evidence、products、order 等上下文。
- `agent_run`：每次 Agent 运行状态和最终 state。
- `tool_call`：商品检索、订单查询、知识检索等工具调用记录。
- `memory_fact`：简单长期偏好记忆，例如偏好无线设备、偏好游戏场景。

### 知识库

- `knowledge_document`：售后政策、FAQ、店铺规则、外设知识文档。
- ChromaDB collection：保存 `knowledge_document` 的向量索引。
- `EvidenceItem`：当前 evidence 来源类型为 `knowledge_document`，包含来源 ID、标题、文档类型、片段、分数和元数据。

### 售后与人工接管

- `after_sales_ticket` / `after_sales_event`：保留旧售后工单模型和事件表。
- `handoff_request`：当前阶段真正用于演示的人工接管留痕表，记录用户、会话、订单、诉求类型、原因、边界分类和状态。

当前系统的售后策略是 read-only 优先：政策说明可以自动回答，办理动作必须人工确认。

## 5. Agent 主流程

非流式接口 `/api/chat` 使用 LangGraph，主链路如下：

```text
load_context
  -> classify_boundary
  -> route_intent
  -> retrieve
  -> retrieve_knowledge
  -> generate
  -> persist
```

流式接口 `/api/chat/stream` 复用同一组节点逻辑，但逐步发送 SSE 事件：

```text
run_started
boundary
tool_call
context
delta
done
error
```

关键流程说明：

1. `load_context` 创建或恢复会话，写入用户消息，启动 `agent_run`，读取长期记忆。
2. `classify_boundary` 先做安全边界分类，优先于业务意图识别。
3. 只有 `in_scope_auto` 会进入自动检索流程。
4. `route_intent` 识别商品推荐、订单查询、售后政策、下单流程、通用问题等意图。
5. `retrieve` 根据意图调用商品 repository 或订单 repository。
6. `retrieve_knowledge` 从 PostgreSQL 同步知识文档到 ChromaDB 并返回 evidence。
7. `generate` 有 LLM key 时调用 DeepSeek；没有 key 时使用稳定 fallback 文案，保证本地 demo 和测试可跑。
8. `persist` 保存助手消息、工具调用、运行状态和简单偏好记忆。

## 6. 售后安全闭环

本项目特意把“能回答”和“能办理”分开：

- 用户问“退货政策怎么走”：这是 read-only 政策咨询，系统可以自动回答，并给出知识库依据。
- 用户说“我要申请退货”：这是业务写操作或人工确认请求，系统不会承诺退款或创建真实退货单，而是返回 `human_handoff_required`。
- 前端展示人工接管状态，用户点击记录后调用 `POST /api/after-sales`。
- 后端创建 `handoff_request`，返回 `202 Accepted` 和 `pending` 状态。
- 用户可以通过 `GET /api/after-sales/handoff-requests/{request_id}` 查询记录。

这个设计避免了客服 Agent 常见风险：模型语言上说“已为你办理”，但实际系统没有权限或没有真实执行。

## 7. 项目亮点与创新

### 1. 边界分类先于 LLM 生成

系统先判断请求是否属于自动回答范围，再进入意图识别和检索。这样可以把退款、退货、维修、改单、代下单、代支付等高风险动作挡在自动流程外。

### 2. read-only 智能客服思路

当前阶段不追求“一切自动化”，而是先把商品、订单、知识库这些低风险查询做好，把真实写操作交给人工接管。这更符合电商客服从 demo 到业务可用的演进路径。

### 3. RAG evidence 可追溯回答

售后政策、FAQ、店铺规则和外设知识来自 `knowledge_document`，通过 ChromaDB 检索后以 evidence 展示。回答不是纯模型记忆，而是带来源片段。

### 4. 多用户可信隔离

登录态、refresh session、当前用户依赖、订单查询隔离、会话隔离、记忆隔离、人工接管记录隔离都已落地，避免用 query `user_id` 模拟真实用户身份。

### 5. 真 SSE 流式体验

前端不是等完整回答后一次性展示，而是能看到边界判断、工具调用、上下文更新和逐 chunk 回答。对于演示 Agent 的推理过程很直观。

### 6. 真实商品数据导入

通过 PCPartPicker 数据集导入鼠标、键盘、耳机等真实外设，并映射到本地 SPU/SKU/EAV 模型，让推荐和对比不只依赖 5 条 seed 数据。

### 7. 可测试、可降级

LLM、Chroma、数据库路径都有隔离策略。测试不依赖真实 LLM key；没有 LLM key 时系统仍能用 fallback 回答完成 demo。

## 8. 阶段性思考

### 技术选择上的思考

- FastAPI + SQLAlchemy 适合快速搭建清晰 API 和可测试数据访问层。
- LangGraph 适合把 Agent 拆成可观测节点，而不是把所有逻辑塞进一个 prompt。
- ChromaDB 适合作为本地 RAG 索引，配合 PostgreSQL 作为权威知识文档来源。
- EAV 商品属性模型适合外设品类，因为不同商品类型的规格字段差异明显。
- POST SSE 需要使用 fetch reader，而不是 EventSource，因为请求必须携带 body 和 Authorization header。

### 产品边界上的思考

客服 Agent 最容易出问题的地方不是“不会聊天”，而是“过度承诺”。所以项目把安全边界作为第一优先级：

- 可以推荐商品，但不能编造库存和价格。
- 可以查询订单，但不能暴露其他用户订单。
- 可以说明售后政策，但不能承诺退款、赔付或维修结论。
- 可以记录人工接管诉求，但不能假装已经办理业务。

### 工程演进上的思考

项目的主线不是一次性做大，而是按可信闭环推进：

1. 跑通商品、订单、售后 demo。
2. 收敛 read-only 边界。
3. 接入 RAG 和 evidence。
4. 导入真实商品数据。
5. 补多用户鉴权和隔离。
6. 做 SSE 流式体验和会话侧栏。
7. 做人工接管留痕。

这种路线让每一步都能演示、能测试、能解释。

## 9. 当前不足

- Evidence 目前主要覆盖知识文档，商品、订单、物流事实还没有统一 evidence schema。
- 多轮上下文仍较轻，只保存简单偏好记忆，缺少会话级工作记忆和稳定指代消解。
- 推荐排序仍是轻量规则，没有离线评测集、点击反馈或学习排序。
- 人工接管目前只是留痕，不包含客服处理后台、状态流转接口或真实通知。
- RAG embedding 当前使用本地 hash provider，适合 demo 和测试，不等同于生产语义向量。
- 商品图片源治理还没完成，真实图片、版权、缓存和降级策略需要后续补齐。
- 边界分类当前主要是规则，需要生产语料评测、误判样例和可观测指标。

## 10. 后续跟进与优化方向

### P0：统一 evidence

把 `EvidenceItem.source_type` 从 `knowledge_document` 扩展到 `product`、`sku`、`order`、`order_logistics`，让价格、库存、订单状态和物流节点也可追溯。

### P0：工作记忆

新增会话级 `working_memory`，保存最近商品候选、当前筛选条件、最近订单 ID、未解决槽位和人工接管状态，支持“这款”“上一单”“第二个”这类多轮指代。

### P1：人工接管队列产品化

为 `handoff_request` 增加状态流转、客服备注、处理人、处理时间、取消和补充说明入口，形成真正可操作的人工队列。

### P1：推荐与对比增强

建立小型评测集，覆盖无线鼠标、红轴键盘、带麦耳机、预算约束、品牌偏好、多商品对比和兼容性问题。

### P1：RAG 生产化

增加生产 embedding provider、增量同步、低分过滤评测、Chroma 异常观测和知识文档版本管理。

### P1：前端工作台继续产品化

补齐会话重命名、删除、置顶、搜索；优化移动端；完善错误重试、取消生成、复制回答、依据折叠和人工接管状态展示。

### P2：MCP 或外部系统试点

不急于把内部 repository MCP 化。更适合优先试点外部只读能力，例如物流只读查询、厂商知识库、商品图片资料或客服系统接管适配器。

## 11. 本地启动与演示准备

推荐初始化：

```bash
make setup-local
```

分步启动：

```bash
./scripts/podman-infra.sh up
cd backend
alembic upgrade head
python -m scripts.seed_demo
cd ..
make dataset
make data-import
make knowledge-sync
```

启动后端：

```bash
cd backend
.venv/bin/uvicorn app.main:app --reload
```

启动前端：

```bash
cd frontend
npm run dev
```

演示账号：

```text
demo@example.com
demo-password
```

## 12. Demo Prompt 路径

下面是一条能覆盖 Agent 主要能力的演示路径。建议在前端工作台里新建会话后按顺序输入。

### Step 0：登录与会话

操作：

```text
登录 demo@example.com / demo-password
新建会话
```

展示点：

- 登录态和当前用户。
- 左侧会话列表。
- 后续请求都基于当前认证用户，不需要也不能传 `user_id`。

### Step 1：商品推荐与流式检索

Prompt：

```text
我主要玩 FPS，预算 300 元以内，想买无线鼠标，推荐几款
```

预期展示：

- SSE 先显示边界判断为自动回答。
- 商品检索工具调用开始和完成。
- 右侧商品面板出现候选 SKU、价格、库存、关键规格。
- 回答说明推荐理由和适配提示。
- 系统可能写入偏好记忆，例如无线设备、游戏场景。

汇报口径：

```text
这里展示的是结构化商品库 + Agent 的推荐，不是模型凭空编商品。
```

### Step 2：商品对比能力

Prompt：

```text
G502 和 Viper V3 Pro 对比一下，哪个更适合 FPS？
```

预期展示：

- 商品搜索会按 token 命中多个商品。
- 右侧商品面板展示多个候选。
- 回答围绕连接方式、DPI、价格、库存和使用场景解释。

汇报口径：

```text
真实商品数据被映射成 SPU/SKU 和属性关系，因此 Agent 可以围绕规格做解释和对比。
```

### Step 3：知识库 RAG 与 evidence

Prompt：

```text
退货政策怎么走？需要满足什么条件？
```

预期展示：

- 边界仍是自动回答，因为这是政策说明，不是办理动作。
- 知识库检索工具调用 `knowledge.retrieve`。
- 右侧依据面板展示 `七天无理由退货政策` 等 evidence。
- 回答引用政策依据，并提醒办理需要人工确认。

汇报口径：

```text
这一步展示 RAG 和 evidence，政策回答来自知识库，而不是 LLM 自己记忆。
```

### Step 4：下单流程 read-only 说明

Prompt：

```text
怎么下单购买键盘？支持哪些支付方式？
```

预期展示：

- 系统说明下单步骤和注意事项。
- 明确不会在聊天里替用户提交订单或完成支付。

汇报口径：

```text
下单流程是 read-only 指导，可以自动回答；代下单、代支付则会进入人工接管。
```

### Step 5：订单与物流查询

Prompt：

```text
帮我查最近订单物流
```

预期展示：

- 订单工具调用。
- 右侧订单面板出现订单号、订单状态、实付金额、明细和物流轨迹。
- 用户只能看到当前登录账号的订单。

可以继续追问：

```text
这个订单里买了什么？
```

汇报口径：

```text
订单查询通过当前认证用户过滤，避免越权访问。
```

### Step 6：售后办理触发人工接管

Prompt：

```text
我要申请退货
```

预期展示：

- 边界分类为 `human_handoff_required`。
- 不进入自动商品/订单/知识办理流程。
- 回答提示需要人工客服确认。
- 右侧接管/售后面板出现记录入口。

汇报口径：

```text
这是系统安全边界的重点：它不会说“已为你退款”或“已创建退货单”，而是转人工。
```

### Step 7：创建人工接管留痕

操作：

```text
在右侧售后区域选择“退货”
原因填写：商品不符合预期，想让人工确认七天无理由退货
点击记录人工确认诉求
```

预期展示：

- 后端返回 `202 Accepted`。
- 生成 `request_id`。
- 状态为 `pending`。
- 文案明确：当前系统不会自动办理退款、退货、维修或订单修改等业务操作。

继续操作：

```text
用生成的 request_id 查询接管记录
```

汇报口径：

```text
这一步展示从“需要人工”到“后端可追踪记录”的最小闭环。
```

### Step 8：越界请求拒答

Prompt：

```text
推荐一台手机
```

预期展示：

- 边界分类为 `out_of_scope`。
- 系统拒答手机推荐。
- 右侧上下文清空或不展示旧商品，避免误导。
- 建议动作引导回外设推荐。

汇报口径：

```text
系统不只会回答，也知道哪些问题不该回答。
```

### Step 9：会话恢复与上下文查看

操作：

```text
刷新页面或切换左侧历史会话
```

预期展示：

- 会话列表恢复历史对话。
- 消息 metadata 恢复商品、订单、依据和边界上下文。

汇报口径：

```text
这不是一次性聊天 demo，而是有会话持久化和上下文恢复的客服工作台。
```

## 13. 一分钟汇报总结

可以用下面这段话作为汇报开场或结尾：

```text
这个项目做的是一个面向 PC 外设电商场景的客服 Agent。它不是单纯接入大模型，而是把大模型放进商品、订单、售后和知识库这些业务数据里，通过 FastAPI、LangGraph、PostgreSQL、ChromaDB 和 React 工作台形成完整闭环。当前系统可以做商品推荐、订单物流查询、售后政策问答和知识库 evidence 展示；同时通过三态边界分类控制风险，遇到退货、退款、维修、订单修改等写操作不会假装办理，而是生成可追踪的人工接管记录。后续我会重点补统一 evidence、工作记忆、人工接管队列产品化和推荐评测，让它从可演示继续向可信可用演进。
```
