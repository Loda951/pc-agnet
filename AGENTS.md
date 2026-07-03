# 仓库指南

## 1. 项目概览

- 这是一个 PC 外设商城客服 AI Agent，面向商品推荐、订单查询、售后工单等场景。
- 后端技术栈：Python 3.11 + FastAPI + LangGraph + SQLAlchemy + PostgreSQL + Redis + ChromaDB。
- 前端技术栈：React 19 + TypeScript + Vite；本地基础设施通过 `scripts/podman-infra.sh` + Podman 启动。

## 2. Commands

- 启动基础设施：`./scripts/podman-infra.sh up`
- 查看服务状态：`./scripts/podman-infra.sh ps`
- 一键初始化本地环境：`make setup-local`
- 安装后端依赖：`cd backend && python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"`
- 初始化数据库：`cd backend && alembic upgrade head && python -m scripts.seed_demo`
- 下载真实商品数据集：`make dataset`
- 导入真实商品数据：`make data-import`
- 同步知识库 RAG：`make knowledge-sync`
- 启动后端：`cd backend && uvicorn app.main:app --reload`
- 启动前端：`cd frontend && npm install && npm run dev`
- 后端测试与 Lint：`cd backend && pytest && ruff check .`
- 前端构建检查：`cd frontend && npm run build`

## 3. Architecture

- 后端入口在 `backend/app/main.py`，API 路由在 `backend/app/api/routers/`。
- 数据模型在 `backend/app/models/`，数据访问在 `backend/app/repositories/`，业务逻辑在 `backend/app/services/`。
- Agent 状态、意图识别、提示词和图流程在 `backend/app/agent/`；数据库迁移在 `backend/alembic/`。
- 前端页面与样式集中在 `frontend/src/`，接口封装在 `frontend/src/api.ts`。

## 4. Conventions

- Python 使用 4 空格缩进、类型标注和 Ruff 规则；行宽上限为 100。
- FastAPI router 保持薄层，数据库读写放 repository，业务编排放 service。
- 测试文件命名为 `test_*.py`，测试函数命名为 `test_*`。
- React 组件用 PascalCase，变量、hook、状态名用 camelCase。
- 提交信息保持简短的祈使句或摘要，例如 `Build PC ecommerce agent MVP`，且遵循`to:`,`fix:`,`feat:`这样的风格。
- 项目主线进程和每个feature的开发在`/docs`文件夹下

## 5. Hard Constraints

- 不要提交 `.env`、`.env.*`、API key、数据库密码或真实用户数据。
- 不要把 DeepSeek key 写进代码、README、AGENTS.md、测试快照或提交记录。
- 不要修改已经合入的 Alembic migration；需要结构变更时新增 migration。
- 不要让测试依赖真实 LLM API key；能 mock 或隔离的逻辑必须隔离。
- 不要新增 Docker 专属命令；本地容器统一使用 Podman 和 `scripts/podman-infra.sh`。
- 修改端口、CORS、环境变量名时，同步更新 `.env.example` 和 README。

## 6. Gotchas

- 后端配置从仓库根目录的 `.env` 读取；按 README 方式从 `backend/` 启动最稳。
- DeepSeek 配置使用 `LLM_PROVIDER=deepseek`，默认 base URL 会解析为 `https://api.deepseek.com`。
- 前端默认请求 `http://localhost:8000`；如需覆盖，注意 Vite 只读取前端目录下的环境文件。
- 初始化演示数据前必须先用 Podman 启动 PostgreSQL、Redis、ChromaDB，并执行 `alembic upgrade head`。
- `make setup-local` 会把 `docyx/pc-part-dataset` clone 到 `.cache/pc-part-dataset`，该目录不应提交。
- Docker volume 不会自动迁移到 Podman volume；如已有 Docker 数据，需要单独导出/导入，demo 数据可直接重跑 seed。
- `podman compose` 依赖额外 compose provider；本项目默认使用原生 Podman 脚本，避免该依赖。
