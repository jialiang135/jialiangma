# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Personal digital-twin RAG system for AI Agent job interview demos. A single-admin private knowledge base that ingests résumés, project docs, and tech notes, then answers interview questions as the candidate via a ReAct+LangGraph multi-agent architecture.

**Stack:** FastAPI (server) + Gradio 6.x (UI, mounted at `/`) + LangGraph (agent orchestration) + ChromaDB (local vector store) + SQLite (metadata) + DeepSeek V4 Pro (LLM) + Alibaba DashScope (embedding + rerank).

## Commands

```bash
# Install dependencies (most are pre-installed; only gradio/httpx/dashscope are additions)
pip install gradio httpx dashscope

# Start the server
cd personal_agent
python main.py
# → http://localhost:7860 (UI)  |  http://localhost:7860/api/docs (Swagger)

# Regenerate the LangGraph graph after code changes (auto on restart; manual reset via:)
python -c "from agent.graph_workflow import reset_agent_graph; reset_agent_graph()"
```

**Configuration:** Copy/update `config/.env` with real API keys before starting. `pydantic-settings` auto-loads it. Default admin credentials are in `.env`; on first startup the user is created in SQLite with bcrypt-hashed password.

## Architecture

### Request path (chat agent — the hot path)

```
Browser → Gradio chat tab → POST /api/chat/stream (SSE)
  → sse_simple_generator() builds AgentState
  → LangGraph graph.ainvoke(initial_state)
    → router node → conditional edge by agent_mode
    → "chat" path:
      → retrieve_before_chat node (ChromaDB search with owner_id filter → DashScope rerank)
      → chat_agent node (LLM with bound tools, decides: tool-call or final-answer)
      → [if tool_calls] tools node → back to chat_agent (max 5 iterations)
      → [if final_answer] → END
  → SSE events streamed back: reasoning → answer chunks → done
```

### LangGraph state machine (agent/graph_workflow.py)

Seven nodes: `router`, `retrieve`, `chat_agent`, `tools`, `manage_agent`, `eval_agent`.

The graph is a singleton (`get_agent_graph()` in `graph_workflow.py`). Chat mode uses a ReAct loop: `retrieve → chat_agent ⇄ tools → END`. Manage and eval modes are linear. `AgentState` (in `agent/state.py`) is a TypedDict — every field is optional at the type level but the initial-state builders in `api/sse_stream.py` populate all fields.

### Two SSE generators (api/sse_stream.py)

| Generator | Strategy | Used when |
|-----------|----------|-----------|
| `sse_chat_generator` | `graph.astream_events(version="v2")` — true per-token streaming | Primary path; requires LangGraph event support |
| `sse_simple_generator` | `graph.ainvoke()` then simulate word-by-word output | Fallback; currently the default in routes |

Both emit the same SSE wire format: `data: {"type": "reasoning|answer|done|error", "content": "..."}\n\n`.

### Data isolation (multi-tenant ready, single-admin in practice)

Every data layer carries `owner_id`:
- **ChromaDB**: metadata `{"owner_id": N}` on every vector; queries use `filter={"owner_id": owner_id}`
- **SQLite**: `WHERE owner_id = ?` on all queries in `core/database.py`
- **JWT**: payload carries `sub` (owner_id); `get_current_user` FastAPI dependency extracts it
- **File system**: uploaded files stored flat in `assets/upload_docs/`; file records in SQLite track ownership

### RAG pipeline (rag/)

1. `rag/document_loader.py` — 15 format handlers (PDF, DOCX, XLSX, TXT/MD/code, images via pytesseract OCR, ZIP recursive). Unified entry: `load_documents_from_paths()`.
2. `rag/text_splitter.py` — `RecursiveCharacterTextSplitter` with Chinese-aware separators (句号/问号/感叹号), chunk_size=1000, overlap=200. Deduplication by exact content match.
3. `rag/vector_store.py` — ChromaDB PersistentClient, collection `knowledge_base`. Key constraint: `search_by_owner()` always filters by owner_id.
4. `rag/retriever.py` — Two-stage: (1) ChromaDB semantic search topK=10, (2) DashScope `gte-rerank` API re-ranks to topK=5. **Graceful degradation**: if the rerank API fails, falls back to original ChromaDB ordering with score=1.0.

### Three agents (agent/)

| Agent | Mode key | Node | Max iterations |
|-------|----------|------|----------------|
| Chat Q&A | `chat` | `chat_agent_node` | 5 ReAct loops |
| Knowledge Management | `manage` | `manage_agent_node` | linear |
| Evaluation | `eval` | `eval_agent_node` | linear |

The chat agent binds two tools from `agent/tools.py`: `search_knowledge_base` and `list_my_files`. Both tools accept `owner_id` injected automatically by `tool_executor_node`.

### Gradio UI (ui/gradio_app.py)

Four tabs: Login → Chat → Knowledge Base → Evaluation. Tab visibility is gated on login state via `gr.State`. The chat tab calls the SSE endpoint through `httpx` async streaming and updates a `gr.Chatbot` and a reasoning `gr.Textbox` in real time. Knowledge-base operations (upload, delete, clear, rebuild) call the FastAPI REST endpoints with the JWT bearer token.

## Key patterns

- **Hallucination guard**: Retrieval returns empty → system prompt forces fallback "我的知识库中没有这方面的信息". No answer is fabricated.
- **Lazy singletons**: ChromaDB client, vector store, and LangGraph graph are created on first use, not at import time. `reset_*()` functions exist for rebuild scenarios.
- **Graceful degradation**: Rerank API failure → fallback to raw ChromaDB ordering. Missing Tesseract → OCR skipped with warning. `astream_events` not working → falls back to `ainvoke` + simulated streaming.
- **`config/settings.py` module-level code**: Creates directories and configures Loguru on import. This means `import config.settings` has side effects — intentional, since every module depends on it.
- **Windows caveat**: Loguru console output may show garbled Chinese due to GBK encoding; file logs (UTF-8) are correct. This does not affect functionality.
