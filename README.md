# 个人数字分身 · 多 Agent 私有 RAG 系统

[![Python](https://img.shields.io/badge/Python-3.12-blue)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green)](https://fastapi.tiangolo.com)
[![Vue](https://img.shields.io/badge/Vue-3-green)](https://vuejs.org)
[![Docker](https://img.shields.io/badge/Docker-ready-blue)](https://docker.com)

基于 **LangChain + LangGraph** 生态的多智能体私有知识库问答系统，专为 AI Agent/大模型开发岗位面试演示设计。前端 Vue 3 SPA，后端 FastAPI SSE 流式，本地 ChromaDB 向量库，云端 DeepSeek V4 Pro 推理。

---

## 技术栈全景

### AI / LLM

| 组件 | 技术 | 用途 |
|------|------|------|
| 大模型 | DeepSeek V4 Pro | 对话生成、幻觉检测、评测 |
| Agent 框架 | **LangGraph 1.x** | 多智能体状态图编排（路由→检索→ReAct⇄工具→END） |
| LLM 接口 | **LangChain DeepSeek** | ChatDeepSeek 客户端，bind_tools 工具绑定 |
| 工具系统 | **LangChain Core @tool** | 5 个 ReAct 工具装饰器，ToolMessage 消息传递 |
| 消息模型 | **LangChain Core Messages** | SystemMessage / HumanMessage / AIMessage / ToolMessage |
| Embedding | **LangChain Community + DashScope** | text-embedding-v4，1024 维向量 |
| 向量库 | **LangChain Chroma** | ChromaDB PersistentClient 封装，owner_id 元数据过滤 |
| 文本分块 | **LangChain Text Splitters** | RecursiveCharacterTextSplitter，中文句号/问号/感叹号分隔 |
| 关键词检索 | rank-bm25 | BM25 + RRF 融合，与向量检索互补 |

### 后端

| 组件 | 技术 | 用途 |
|------|------|------|
| Web 框架 | FastAPI 0.115 | 6 组 REST 路由 + SSE 流式 |
| 流式协议 | Server-Sent Events | 推理步骤 + 回答文本实时推送 |
| 数据库 | SQLite + sqlite3 | 用户、文件元数据、对话日志、评测报告、Token 统计 |
| 鉴权 | JWT + bcrypt | HS256 签名，480 分钟过期 |
| 文档解析 | PyPDF / python-docx / openpyxl | 15+ 格式，含 OCR 和 ZIP 递归 |
| 重排 | DashScope gte-rerank | 10 选 5 重排，失败降级回原始排序 |
| 日志 | Loguru | 文件轮转 + UTF-8 编码 |

### 前端

| 组件 | 技术 | 用途 |
|------|------|------|
| 框架 | Vue 3 (Composition API) | SPA 三页面 |
| 构建 | Vite 6 | 生产构建产物托管于 FastAPI |
| 路由 | Vue Router | /chat /knowledge /eval |
| 状态 | Pinia | 登录态持久化 (localStorage) |
| HTTP | fetch + ReadableStream | SSE 流式解析，AbortController 中断 |

### 部署

| 组件 | 技术 |
|------|------|
| 容器化 | Docker 多阶段构建（Node 20 + Python 3.12） |
| 编排 | Docker Compose，健康检查 + 数据持久化 |

---

## 快速启动

```bash
# 1. 配置 API Key
# 编辑 config/.env，填入 DeepSeek 和 DashScope 的 API Key

# 2. 安装依赖
pip install -r requirements.txt

# 3. 启动
python main.py
# → 前端 http://localhost:7860
# → Swagger http://localhost:7860/api/docs
```

默认管理员 `admin` / `admin123456`（`.env` 可改）。

## Docker 部署

```bash
docker compose up -d --build
# → http://localhost:7863
```

---

## 项目结构

```
personal_agent/
├── agent/                  # LangGraph 多智能体
│   ├── graph_workflow.py   # 状态图：router → [retrieve → chat ⇄ tools | manage | eval]
│   ├── state.py            # AgentState TypedDict（22 字段）
│   ├── chat_agent.py       # ReAct 问答节点（retrieve + chat_agent + tool_executor）
│   ├── manage_agent.py     # 知识库管理节点（upload/delete/list/rebuild/clear）
│   ├── eval_agent.py       # 自动评测节点（并发 + 幻觉检测）
│   ├── prompts.py          # 三大 Agent 中文系统提示词
│   └── tools.py            # 5 个 ReAct 工具（search / list / summary / context / verify）
│
├── api/                    # FastAPI 接口层
│   ├── main.py             # App 工厂：CORS、中间件、路由注册
│   ├── sse_stream.py       # SSE 双通道（astream_events + ainvoke 降级）
│   └── routes/             # 6 组路由：auth / chat / kb / eval / token / export
│
├── core/                   # 核心模块
│   ├── auth.py             # JWT + bcrypt + FastAPI 鉴权依赖
│   ├── database.py         # SQLite CRUD（5 张表，owner_id 隔离）
│   ├── schemas.py          # Pydantic v2 数据模型
│   └── token_tracker.py    # Token 用量统计 + DeepSeek 费用估算
│
├── rag/                    # RAG 检索增强生成链路
│   ├── document_loader.py  # 15+ 格式解析（PDF/DOCX/XLSX/TXT/图片OCR/ZIP）
│   ├── text_splitter.py    # 语义分块（Markdown → 中文段落 → RecursiveCharacter）
│   ├── vector_store.py     # ChromaDB 封装（懒加载单例，owner 过滤）
│   ├── retriever.py        # 两阶段检索（语义搜索 → gte-rerank 重排）
│   ├── bm25_search.py      # BM25 关键词 + RRF 混合检索，中文分词
│   └── search_cache.py     # 线程安全 TTL 缓存（1h，128 条 FIFO）
│
├── frontend/               # Vue 3 前端
│   └── src/
│       ├── views/          # ChatView（对话+推理时间线）/ EvalView / KnowledgeView
│       ├── api/            # HTTP 客户端（SSE 流式解析、JWT 自动注入、401 拦截）
│       ├── components/     # LoginBar / TokenStats
│       └── stores/         # Pinia 登录态
│
├── config/
│   ├── settings.py         # pydantic-settings 全局配置
│   └── .env                # API Key（不入 git）
│
├── tests/
│   └── test_all.py         # pytest 全链路（7 TestClass，配置→DB→RAG→API→SSE→集成）
│
├── assets/                 # 运行时数据（chroma_db / upload_docs / test_data）
├── logs/                   # Loguru 日志（10MB 轮转，30 天保留）
├── Dockerfile              # 多阶段构建
├── docker-compose.yml      # 一键部署
└── requirements.txt
```

---

## 核心架构

### Agent 工作流（LangGraph 状态图）

```
用户发送消息
      ↓
  router 节点 ──→ agent_mode?
      │
      ├── "chat" → retrieve ─→ chat_agent ─→ 要调工具? ──YES→ tools ─→ chat_agent（最多 5 轮）
      │                                                │
      │                                               NO
      │                                                ↓
      │                                             END（返回最终回答）
      │
      ├── "manage" → manage_agent ─→ END（处理上传/删除/重建）
      │
      └── "eval" → eval_agent ─→ END（批量评测 + 幻觉检测）
```

### ReAct 工具集

| 工具 | 功能 | 调用时机 |
|------|------|---------|
| `search_knowledge_base` | ChromaDB 语义检索 | 用户问任何知识性问题 |
| `get_kb_summary` | 知识库内容全貌 | 宽泛问题（"介绍一下你自己"） |
| `list_my_files` | 已上传文件列表 | 用户问"有哪些资料" |
| `get_chat_context` | 历史对话记录 | 多轮对话上下文 |
| `verify_answer_against_kb` | 自我校验防幻觉 | 不确定时自检 |

### 数据隔离

所有数据层都携带 `owner_id`：
- **ChromaDB**：`metadata={"owner_id": N}` + 查询 `filter={"owner_id": owner_id}`
- **SQLite**：`WHERE owner_id = ?` 全表查询
- **JWT**：payload 含 `sub` → FastAPI `get_current_user` 依赖注入
- 多租户就绪，当前单管理员使用

### 两阶段 RAG 检索

```
用户问题 → ChromaDB embedding 搜索 topK=10
         → [可选] BM25 RRF 混合
         → DashScope gte-rerank 重排 topK=5
         → 注入 System Prompt
         → LLM 生成
```

- Rerank 失败 → 自动回退 ChromaDB 原始排序（优雅降级）
- 检索为空 → 强制回复"知识库中暂无相关信息"（防幻觉）

---

## 自动评测体系

上传 JSON 评测集 → 并发执行问答 + 幻觉检测 → 生成多维报告：

| 指标 | 说明 |
|------|------|
| 准确率 | 非幻觉题 / 总题数 |
| 幻觉率 | LLM 检测出的无中生有比例 |
| 平均匹配度 | 回答与知识库的语义匹配分数 (0-100) |
| 检索质量 | good / partial / poor 三档 |

评测报告自动存入 SQLite，支持导出 Markdown。评测集自带 5 道幻觉检测题（知识库无此信息），验证兜底机制。

---

## 防幻觉策略

1. **System Prompt 约束** — "只能基于知识库内容回答，禁止编造"
2. **检索结果为空兜底** — "抱歉，我的知识库中暂时没有这方面的信息"
3. **verify_answer_against_kb 工具** — 自我校验，匹配度 < 0.5 → 标记不可用
4. **评测集幻觉题** — 故意问知识库没有的问题，验证系统诚实度

---

## 优雅降级

| 场景 | 降级策略 |
|------|---------|
| DashScope Rerank API 失败 | 回退 ChromaDB 原始排序（score=1.0） |
| LangGraph astream_events 不可用 | 回退 ainvoke + 模拟打字机流式 |
| Tesseract OCR 未安装 | 跳过图片 OCR，仅输出警告 |
| JWT Token 无效 | 公开接口降级为 owner_id=0（无知识库权限） |

---

## 本地开发

```bash
# 前端热重载开发
cd frontend && npm run dev

# 后端
python main.py

# 跑测试（跳过需要在线 API 的慢测试）
python -m pytest tests/test_all.py -v

# 全量测试（需要 API Key）
python -m pytest tests/test_all.py -v -k "not slow"
```

---

## 许可证

MIT
