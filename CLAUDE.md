# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

PC 外设商城客服 AI Agent. Stack: Python 3.11+ / FastAPI + LangGraph + SQLAlchemy (async) + PostgreSQL + Redis + ChromaDB (backend), React 19 + TypeScript + Vite (frontend). Infrastructure via Podman (`scripts/podman-infra.sh`), not Docker.

## Commands

```bash
# One-command setup (after cp .env.example .env)
make setup-local

# Individual steps
./scripts/podman-infra.sh up          # Start PostgreSQL 16, Redis 7, ChromaDB
cd backend && python3 -m venv .venv   # Create venv
cd backend && .venv/bin/pip install -e ".[dev]"  # Install backend deps
cd backend && .venv/bin/alembic upgrade head      # Run migrations
cd backend && .venv/bin/python -m scripts.seed_demo  # Seed demo data
make dataset                          # Clone docyx/pc-part-dataset to .cache/
make data-import                      # Import real product data into PostgreSQL
make knowledge-sync                   # Sync knowledge_document → ChromaDB

# Run servers
cd backend && .venv/bin/uvicorn app.main:app --reload  # Backend :8000
cd frontend && npm install && npm run dev               # Frontend :5173

# Test & lint
cd backend && .venv/bin/pytest         # Run all tests
cd backend && .venv/bin/ruff check .  # Lint
cd backend && .venv/bin/pytest backend/tests/test_catalog_repository.py  # Single test file

# Podman management
./scripts/podman-infra.sh ps          # Check container status
./scripts/podman-infra.sh down        # Stop containers (keeps volumes)
CONFIRM_RESET=1 ./scripts/podman-infra.sh reset  # Wipe volumes and recreate
```

## Architecture

**Backend** (`backend/app/`):
- `main.py` — FastAPI app, 5 routers under `/api`: health, chat (sync + SSE stream), catalog/search, orders, after-sales
- `core/config.py` — Pydantic Settings from `../.env` (DB, Redis, Chroma, LLM, CORS)
- `core/database.py` — Async SQLAlchemy engine + session factory
- `core/llm.py` — `build_chat_model()` returns `ChatOpenAI` or None (when no API key, fallback path activates)
- `models/` — 3 files (commerce, conversation, support), 15+ tables. Uses EAV pattern for product attributes: `AttributeKey` (with `is_spec`/`is_filter` flags) → `AttributeValue` → `GoodsAttributeRelation`
- `repositories/` — Data access layer. `CatalogRepository.search_products()` has tokenized search, category alias resolution, price filtering, EAV attribute loading, weighted scoring (+8 title, +5 brand, +3 category, +2 spec, +4 filter match, +1 in stock), and multi-sort ranking
- `services/dataset_mapper.py` — Maps pc-part-dataset JSON to `ImportedProduct` dataclasses: category name mapping, brand inference from product name, attribute normalization, spec/filter classification, USD→CNY conversion
- `services/knowledge_rag.py` — `ChromaKnowledgeService` with `LocalHashEmbeddingProvider` for deterministic offline embeddings, `retrieve()` with score threshold, `sync()` from PostgreSQL
- `agent/` — LangGraph `StateGraph`: `load_context → classify_boundary → [auto: route_intent → retrieve → retrieve_knowledge → generate → persist] / [blocked: generate → persist]`. Falls back to template answers when LLM unavailable
- `agent/intent.py` — Rule-based boundary classification (in_scope_auto / human_handoff_required / out_of_scope), Chinese NLP filter extraction (无线→wireless, 红轴→switches=Red, etc.), budget parsing
- `agent/prompts.py` — Chinese system prompt with read-only policy
- `schemas/` — Pydantic v2 request/response models
- `scripts/` — `seed_demo.py` (5 demo products, 1 order, 5 knowledge docs), `import_pc_part_dataset.py` (batch import from JSON/JSONL), `sync_knowledge.py` (PostgreSQL→ChromaDB)

**Frontend** (`frontend/src/`):
- Single-page app, no router, no state library — all state in `App.tsx` (~360 lines)
- 3-column layout: sidebar (brand, metrics, quick prompts), chat center, context panel (boundary status, evidence, products, order, after-sales form)
- `api.ts` — Single `sendChat()` hitting `/api/chat`
- Vite dev server proxies `/api` → `:8000`

**Data pipeline**: `docyx/pc-part-dataset` JSON → `normalize_part_record()` → `ImportedProduct` → `_upsert_product()` (shared with seed_demo) → Category/Brand/Spu/Sku/AttributeKey/AttributeValue/GoodsAttributeRelation

## Conventions

- Python: 4 spaces, type annotations, Ruff (line width 100). Thin routers, repository for DB, service for business logic.
- Tests: `test_*.py` files, `test_*` functions. Integration tests use real PostgreSQL with transaction rollback, mock LLM (empty API key → None → fallback path), fake knowledge service.
- React: PascalCase components, camelCase variables/hooks.
- Commit style: `feat:`, `fix:`, `to:` prefix.
- Feature docs in `/docs` (Chinese).

## Hard Constraints

- Never commit `.env`, API keys, database passwords, or real user data.
- Never write real LLM API keys in code, tests, or snapshots — tests must mock or isolate.
- Never modify merged Alembic migrations — create new ones for schema changes.
- Never add Docker-specific commands — use Podman and `scripts/podman-infra.sh` exclusively.
- When changing ports, CORS, or env vars, update both `.env.example` and README.

## Gotchas

- Backend reads config from repo root `.env`; start from `backend/` for correct relative path resolution.
- DeepSeek config: `LLM_PROVIDER=deepseek` → base URL resolves to `https://api.deepseek.com`.
- `build_chat_model()` returns None when `LLM_API_KEY` is empty — agent falls back to `_generate_fallback()` template answers. Tests rely on this.
- Frontend only reads env from its own directory, not repo root.
- `make setup-local` clones `docyx/pc-part-dataset` into `.cache/` (gitignored) — requires internet.
- Podman volume != Docker volume; if migrating, export/import data manually or re-run seed.
- `podman compose` requires extra provider; project uses shell script instead.
- `CatalogRepository.search_products()` loads candidates from DB then applies in-memory scoring/filtering — very large catalogs may need pagination or query-level filtering.