# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Personal digital-twin RAG system for AI Agent job interview demos. A single-admin private knowledge base that ingests résumés, project docs, and tech notes, then answers interview questions as the candidate via a ReAct+LangGraph multi-agent architecture.

**Stack:** FastAPI (server) + Vue 3 (frontend) + LangGraph 1.x (agent orchestration) + SQLAlchemy 2.0 async + aiosqlite (metadata) + ChromaDB (local vector store) + DeepSeek reasoning model (LLM) + Alibaba DashScope (embedding + rerank) + RAGAS (evaluation).

**Deployment:** Alibaba Cloud ECS at http://39.106.191.98:8080, Docker Compose multi-container (app + nginx + redis + prometheus + grafana).

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Start the server
cd personal_agent
python main.py
# → http://localhost:7860 (UI)  |  http://localhost:7860/api/docs (Swagger)

# Build frontend
cd frontend && npm ci && npm run build && cd ..

# Docker build and run
docker compose up -d --build
```

```bash
# Run tests (slow tests make real API calls and are skipped by default)
python -m pytest tests/ -q -m "not slow"
```

**Configuration:** Copy/update `config/.env` with real API keys before starting. `pydantic-settings` auto-loads it. Startup **refuses to boot** with the repo's default secrets unless `ALLOW_INSECURE_DEFAULTS=true` (tests set this in `tests/conftest.py`).

**Test isolation:** `tests/conftest.py` redirects `db_path` / `upload_dir` / `chroma_persist_dir` to a temp dir *before* any app module is imported — tests never touch real `assets/` data.

## Architecture

### Request path (chat agent — the hot path)

```
Browser → Nginx (:8080) → FastAPI SSE → LangGraph ReAct Agent
  → ChromaDB (vector search) + DashScope (rerank)
  → DeepSeek reasoning model (LLM)
  → SSE events: reasoning → answer → done
```

### LangGraph state machine (agent/graph_workflow.py)

Six nodes: `router`, `retrieve`, `chat_agent`, `tools`, `manage_agent`, `eval_agent`.
Chat mode uses a ReAct loop: `retrieve → chat_agent ⇄ tools → END` (max 5 iterations).

### RAG pipeline (rag/)

1. `rag/document_loader.py` — 15 format handlers (PDF, DOCX, XLSX, TXT/MD/code, images via pytesseract OCR, ZIP recursive)
2. `rag/text_splitter.py` — `SemanticTextSplitter` (Markdown headings → Chinese numbered sections → paragraphs → recursive fallback), chunk_size=1000, overlap=200. Toggle via `USE_SEMANTIC_SPLITTER`.
3. `rag/vector_store.py` — ChromaDB PersistentClient, collection `knowledge_base`
4. `rag/retriever.py` — Hybrid (BM25 + vector, RRF fusion) topK=10 → DashScope gte-rerank topK=5, with TTL cache and graceful degradation. Cache invalidation is hooked into `rag/vector_store.py` write ops.

### Three agents (agent/)

| Agent | Mode key | Node | Max iterations |
|-------|----------|------|----------------|
| Chat Q&A | `chat` | `chat_agent_node` | 5 ReAct loops |
| Knowledge Management | `manage` | `manage_agent_node` | linear |
| Evaluation | `eval` | `eval_agent_node` | linear |

## Key patterns

- **Hallucination guard**: Retrieval returns empty → system prompt forces fallback "我的知识库中没有这方面的信息"
- **Lazy singletons**: ChromaDB client, vector store, and LangGraph graph are created on first use, not at import time
- **Graceful degradation**: Rerank API failure → fallback to raw ChromaDB ordering. Missing Tesseract → OCR skipped with warning
- **Multi-tenant data isolation**: Every data layer carries `owner_id` (ChromaDB metadata, SQLite WHERE, JWT payload, file records)
- **`config/settings.py` module-level code**: Creates directories and configures Loguru on import

## Deployment & Sync

When the user asks to upload/sync/deploy to GitHub or the server, follow the deployment workflow below.

### Server info

| Item | Value |
|------|-------|
| IP | 39.106.191.98 |
| User | admin |
| Project path | /opt/personal-agent |
| SSH | `ssh -o StrictHostKeyChecking=no admin@39.106.191.98` |

### Sync workflow

**Step 1: Check what's changed**
```bash
git status --short
```

**Step 2: Commit and push to GitHub**
```bash
git add -A
git commit -m "<message>"
git push origin main
```

**Step 3: Determine what to sync to server**

- **Only static files changed** (README.md, dist/, nginx config, docker-compose.yml): → Step 4a (tar+SSH 秒传)
- **Source code changed** (api/, core/, agent/, rag/, main.py, requirements.txt, frontend/src/): → Step 4b (tar+SSH + 服务器 Docker 构建)

**Step 4a: Static files sync (no rebuild)**
```bash
cd e:/zuoye/jialiangma/personal_agent && tar czf - <file1> <file2> ... | ssh -o StrictHostKeyChecking=no admin@39.106.191.98 "cd /opt/personal-agent && tar xzf - && echo 'sync ok'"
```

**Step 4b: Full source sync + Docker rebuild**
```bash
cd e:/zuoye/jialiangma/personal_agent && tar czf - --exclude='config/.env' --exclude='node_modules' --exclude='__pycache__' --exclude='*.pyc' --exclude='assets' --exclude='logs' --exclude='frontend/node_modules' --exclude='frontend/dist' api/ core/ config/ agent/ rag/ frontend/ scripts/ nginx/ .github/ requirements.txt Dockerfile docker-compose.yml main.py | ssh -o StrictHostKeyChecking=no admin@39.106.191.98 "cd /opt/personal-agent && tar xzf - && docker compose up -d --build app && echo 'deploy ok'"
```

**Step 5: Verify**
```bash
ssh -o StrictHostKeyChecking=no admin@39.106.191.98 "docker compose -f /opt/personal-agent/docker-compose.yml ps"
curl -s -o /dev/null -w "HTTP %{http_code}" http://39.106.191.98:8080/api/health
```

### Important notes

- **Never** include `config/.env` in tar — it contains API keys and is server-specific
- **Never** include `node_modules`, `__pycache__`, `assets/`, `logs/` in tar
- The server's git pull is unreliable from China (GitHub HTTPS timeout), so always use tar+SSH instead
- After Docker rebuild, wait ~30s for health check to pass before verifying
- The server runs as non-root user `admin`, NOT `root`
