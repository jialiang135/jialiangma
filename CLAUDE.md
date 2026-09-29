# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Personal digital-twin RAG system for AI Agent job interview demos. A single-admin private knowledge base that ingests résumés, project docs, and tech notes, then answers interview questions as the candidate via LangGraph orchestration: **one real ReAct agent (chat Q&A) plus two deterministic workflow nodes (knowledge-base management, evaluation)**. The manage/eval nodes are deliberately deterministic (if/elif dispatch and keyword matching, no LLM decision) — for deterministic operations an LLM's "intent understanding" is less reliable (same input can yield different judgments), so this is a trade-off, not unfinished work.

**Stack:** FastAPI (server) + Vue 3 (frontend) + LangGraph 1.x (agent orchestration) + SQLAlchemy 2.0 async + aiosqlite (metadata) + ChromaDB (local vector store) + DeepSeek reasoning model (LLM) + Alibaba DashScope (embedding + rerank) + RAGAS (evaluation).

**Deployment:** Tencent Cloud Lighthouse at http://193.112.29.164:8080 (nginx also maps :80), Docker Compose multi-container (app + nginx + redis, plus optional prometheus + grafana under the `monitoring` profile).

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
Browser → Nginx (:80 / :8080) → FastAPI SSE → LangGraph ReAct Agent
  → ChromaDB (vector search) + DashScope (rerank)
  → DeepSeek reasoning model (LLM)
  → SSE events: reasoning → answer → done
```

### LangGraph state machine (agent/graph_workflow.py)

Six nodes: `router`, `retrieve`, `chat_agent`, `tools`, `manage_agent`, `eval_agent`.
`router` / `route_to_agent` does **not** recognize intent — it only reads the caller-supplied
`state["agent_mode"]` (`graph_workflow.py:24`) and falls back to `chat` on unknown values.
Chat mode uses a ReAct loop: `retrieve → chat_agent ⇄ tools → END` (max 5 iterations).
`manage_agent` / `eval_agent` are deterministic (no tools, no LLM decision).

### RAG pipeline (rag/)

1. `rag/document_loader.py` — 15 format handlers (PDF, DOCX, XLSX, TXT/MD/code, images via pytesseract OCR, ZIP recursive)
2. `rag/text_splitter.py` — `SemanticTextSplitter` (Markdown headings → Chinese numbered sections → paragraphs → recursive fallback), chunk_size=1000, overlap=200. Toggle via `USE_SEMANTIC_SPLITTER`.
3. `rag/vector_store.py` — ChromaDB PersistentClient, collection `knowledge_base`
4. `rag/retriever.py` — Hybrid (BM25 + vector, RRF fusion) topK=10 → DashScope gte-rerank topK=5, with TTL cache and graceful degradation. Cache invalidation is hooked into `rag/vector_store.py` write ops.

### Agent vs. deterministic workflows (agent/)

The graph has **one real ReAct agent and two deterministic workflow nodes**. Only
`chat_agent_node` binds tools and loops; `manage_agent_node` / `eval_agent_node` make no
LLM calls — they are `if/elif` and keyword dispatch.

| Node | Mode key | Kind | Max iterations |
|------|----------|------|----------------|
| Chat Q&A | `chat` | real ReAct agent (`agent/chat_agent.py`, `llm.bind_tools` + tool loop) | 5 ReAct loops |
| Knowledge Management | `manage` | deterministic workflow — `if/elif` dispatch, no tools/LLM (`agent/manage_agent.py:50`) | linear |
| Evaluation | `eval` | deterministic workflow — 6 keyword `in` matches (`agent/eval_agent.py:31`) | linear |

In the shipped product the frontend chat always sends `agent_mode='chat'`
(`frontend/src/api/chat.js`); KB management and evaluation are driven by dedicated REST
routes (`api/routes/kb_routes.py`, `api/routes/eval_routes.py`) that share the same
implementation modules (`core/kb_tasks.py`, `core/eval_runner.py`) as these two nodes.

## Key patterns

- **Hallucination guard**: Retrieval returns empty → system prompt forces fallback "我的知识库中没有这方面的信息"
- **Lazy singletons**: ChromaDB client, vector store, and LangGraph graph are created on first use, not at import time
- **Graceful degradation**: Rerank API failure → fallback to raw ChromaDB ordering. Missing Tesseract → OCR skipped with warning
- **Multi-tenant data isolation**: Every data layer carries `owner_id` (ChromaDB metadata, SQLite WHERE, JWT payload, file records)
- **`config/settings.py` module-level code**: Creates directories and configures Loguru on import

## Deployment & Sync

When the user asks to upload/sync/deploy to GitHub or the server, follow the workflow below.
Full deploy guide + the pitfalls hit during the Tencent Cloud deploy live in **[docs/DEPLOY.md](docs/DEPLOY.md)**.

### Server info

| Item | Value |
|------|-------|
| Provider | Tencent Cloud Lighthouse (轻量应用服务器) |
| IP | 193.112.29.164 |
| User | `ubuntu` (passwordless sudo) |
| Project path | /opt/personal-agent |
| SSH | `ssh ubuntu@193.112.29.164` (public key already installed — **no password**) |
| Web | http://193.112.29.164:8080 (and http://193.112.29.164:80) — Docker nginx maps both |

> The old Alibaba Cloud ECS `39.106.191.98` (user `admin`) is **expired and released** — do not use it.
> The app container runs as non-root user `appuser` (**uid 999**); nginx listens on 80 inside the
> container, mapped to host 80 and 8080.

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
cd e:/zuoye/jialiangma/personal_agent && tar czf - <file1> <file2> ... | ssh ubuntu@193.112.29.164 "cd /opt/personal-agent && tar xzf - && echo 'sync ok'"
```

**Step 4b: Full source sync + Docker rebuild**
```bash
cd e:/zuoye/jialiangma/personal_agent && tar czf - --exclude='config/.env' --exclude='node_modules' --exclude='__pycache__' --exclude='*.pyc' --exclude='assets' --exclude='logs' --exclude='frontend/node_modules' --exclude='frontend/dist' api/ core/ config/ agent/ rag/ frontend/ scripts/ nginx/ .github/ requirements.txt Dockerfile docker-compose.yml main.py | ssh ubuntu@193.112.29.164 "cd /opt/personal-agent && tar xzf - && docker compose up -d --build app && echo 'deploy ok'"
```

**Step 5: Verify**
```bash
ssh ubuntu@193.112.29.164 "docker compose -f /opt/personal-agent/docker-compose.yml ps"
curl -s -o /dev/null -w "HTTP %{http_code}" http://193.112.29.164:8080/api/health
```

### Deploy pitfalls (learned the hard way on Tencent Cloud)

Full rationale in **[docs/DEPLOY.md](docs/DEPLOY.md)**; the short version:

1. **pip MUST use the Tencent Cloud mirror** (`mirrors.cloud.tencent.com`). Aliyun's PyPI throttles
   **HTTP/1.1 to 34 kB/s** (and pip only speaks HTTP/1.1), while the Tencent mirror hits 152 MB/s on the
   same machine — build time drops from 20+ min to seconds. Do **not** switch back to `mirrors.aliyun.com`.
2. **Bind-mounted dirs must be owned by the container user.** The app runs as `appuser` (uid 999);
   if `./logs` / `./assets` are owned by the host user it crashes at startup with
   `PermissionError: /app/logs/app_*.log`. Fix: `sudo chown -R 999:999 /opt/personal-agent/{logs,assets}`.
3. **Run only ONE build at a time.** Two concurrent `docker compose ... --build` fight over the same
   BuildKit cache lock and both just wait (load ~0.07, nothing happening).
4. **pip uses a BuildKit cache mount, not `--no-cache-dir`** (see Dockerfile) — the image-layer cache is
   only kept if that layer succeeds, but a cache mount survives a failed build.
5. **Pinned deps that matter**: `prometheus-fastapi-instrumentator==7.1.0` (8.x needs `starlette>=1.0.0`,
   mutually exclusive with `fastapi 0.115.6` → `ResolutionImpossible`, the whole requirements file fails);
   `ragas` lives in `requirements-eval.txt` (its `instructor` needs `jiter<0.15`, deadlock with `openai`'s `>=0.16`).
6. **`config/.env` is maintained on the server only** (real keys, server-specific). The **JWT secret is
   regenerated on deploy and differs from local**; think about `ALLOW_REGISTRATION` before exposing publicly.

### Important notes

- **Never** include `config/.env` in tar — it contains API keys and is server-specific
- **Never** include `node_modules`, `__pycache__`, `assets/`, `logs/` in tar
- The server's git pull is unreliable from China (GitHub HTTPS timeout), so always use tar+SSH instead
- After Docker rebuild, wait ~30s for health check to pass before verifying
- The server user is `ubuntu` (with passwordless sudo), **not** `root` and **not** the old `admin`
