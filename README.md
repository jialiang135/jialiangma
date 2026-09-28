# 个人数字分身 · 多 Agent 私有 RAG 系统

[![Python](https://img.shields.io/badge/Python-3.12-blue)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green)](https://fastapi.tiangolo.com)
[![Vue](https://img.shields.io/badge/Vue-3-green)](https://vuejs.org)
[![Docker](https://img.shields.io/badge/Docker-ready-blue)](https://docker.com)

> 基于 LangGraph + DeepSeek 推理模型的多 Agent 私有知识库问答系统

**🌐 在线地址：http://39.106.191.98:8080**

**📦 GitHub：https://github.com/jialiang135/jialiangma**

---

## 一、架构说明

### 整体架构

```
手机 / 桌面浏览器
      ↓
  Nginx (:8080, 反向代理 + 限流 + HTTPS)
      ↓
  FastAPI (SSE 真实流式, 32 个业务接口)
      ↓
  LangGraph StateGraph (6 节点有向图)
      ├── Router 路由节点
      ├── Retrieve 检索节点
      ├── Chat Agent (ReAct 循环, 最多 5 轮)
      ├── Tool Executor (5 个 ReAct 工具)
      ├── Manage Agent (知识库管理)
      └── Eval Agent (RAGAS 自动评测)
      ↓
  ┌───────────┬────────────┬──────────────┐
  ChromaDB    SQLite       Redis          DeepSeek
  (向量存储)  (元数据+审计) (缓存)         (LLM)
  └───────────┴────────────┴──────────────┘
      ↓
  DashScope (Embedding + Rerank)
```

### Agent 工作流（LangGraph 状态图）

```
用户发送消息
      ↓
  Router 节点 ──→ agent_mode?
      │
      ├── "chat" → Retrieve ──→ Chat Agent ──→ needs_tool_call?
      │                                       ├── YES → Tools ──→ Chat Agent (最多 5 轮)
      │                                       └── NO  → END
      │
      ├── "manage" → Manage Agent ──→ END
      │
      └── "eval" → Eval Agent ──→ END
```

**核心设计决策：**
- Router 根据 `agent_mode` 路由到不同 Agent 节点（条件边），未知模式回退 chat
- Chat 模式采用标准 ReAct 循环：思考 → 调工具 → 观察 → 再思考
- `should_continue_chat()` 检查 `needs_tool_call` 和 `iteration_count < 5`，防止无限循环
- 每个节点都是纯函数，状态通过 `AgentState` TypedDict 传递
- **多轮对话记忆**：按 `conversation_id` 取回最近 6 轮问答注入提示词，
  使"那它呢？"这类追问有上下文（默认带轮数上限，避免吃满上下文预算）

### 数据层（SQLAlchemy 2.0 async）

- 6 张表 + 审计表统一为 `DeclarativeBase` 模型，表结构只有一个来源
- `create_async_engine` + `aiosqlite`，**DB 操作不再阻塞事件循环**
- 连接级 PRAGMA：`journal_mode=WAL`（读写不互斥）+ `busy_timeout=5000`
  （写锁冲突时等待而非立刻报错）+ `synchronous=NORMAL`
- 时间统一按 **UTC** 存取（SQLite `CURRENT_TIMESTAMP` 即 UTC，
  混入本地时间会让登录锁定窗口与"今日统计"错 8 小时）
- 无法 async 的阻塞操作（Chroma 查询、DashScope 同步 HTTP、文档解析）
  统一用 `asyncio.to_thread` 移出事件循环

### 数据隔离（多租户就绪）

所有数据层携带 `owner_id`：
- ChromaDB：`metadata={"owner_id": N}` + 查询 `filter={"owner_id": owner_id}`
- SQLite：所有查询 `WHERE owner_id = ?`
- JWT：payload 含 `sub`（owner_id）→ FastAPI `Depends(get_current_user)` 注入

### 两阶段 RAG 检索

```
用户问题
    → 结构感知分块入库（Markdown 标题 / 中文编号章节 / 段落 → 定长兜底）
    → BM25 关键词 + ChromaDB 语义检索，RRF 加权融合 topK=10
    → DashScope gte-rerank 重排 topK=5
    → 注入 System Prompt
    → LLM 生成回答
```

**几个容易忽略的点：**

- **结构感知分块**：简历、项目文档这类有层级的材料，按章节切块能保住
  "这一块在讲什么"，而不是被定长切分拦腰截断（实测同一份文档：
  结构分块 4 块各自带标题 vs 定长分块 1 块）
- **混合检索**：BM25 补足向量检索对专有名词/编号不敏感的问题
- **缓存失效**：BM25 索引与检索结果缓存挂在向量库的**写操作**上统一失效 ——
  上传、删除、清空、重建任何路径都会自动清，不会出现"删了文件还检索得到"
- 优雅降级：Rerank API 失败 → 自动回退原始排序；Embedding 服务熔断 →
  快速失败而不是每个请求都去撞一遍

### 自动评测（RAGAS）

`POST /api/eval/run` 提交后台评测任务，`GET /api/eval/reports/{id}` 轮询进度。

| 指标 | 含义 |
|---|---|
| faithfulness | 回答是否忠于检索到的上下文（防幻觉核心指标） |
| answer_relevancy | 回答与问题的相关度 |
| context_precision | 检索回来的内容里有多少真正有用 |
| context_recall | 期望答案的信息是否被检索到 |
| **honesty_rate**（自建） | 知识库外的问题，是否如实回答"没有相关信息" |

两个设计细节：

- 评测是**后台任务** —— 每条问题要跑一次完整问答，再让 LLM 逐条判定，
  单条约 10~30 秒，放进请求里会把整个进程拖死
- 样本**按分类轮询抽取**：直接取前 N 条会让"幻觉检测"类（排在评测集最后）
  永远测不到，而诚实度恰恰是最该看的指标

---

## 二、关键 Prompt 与 Vibe Coding 思路

### Prompt 工程设计

**Chat Agent System Prompt 核心约束（agent/prompts.py）：**

```
1. 只能基于知识库内容回答 —— 所有回答必须来自检索到的真实信息
2. 禁止编造 —— 绝对不允许编造个人信息、项目经历、技能或观点
3. 引用来源 —— 自然引用知识库来源，让回答有据可查
4. 保持人设 —— 第一人称"我"，语气专业自然，像真实面试候选人在交流
```

**回答风格指导：**
- 技术问题 → 深度、有条理、展示思考过程
- 项目经历 → STAR 法则（情境、任务、行动、结果）
- 不知道的问题 → 诚实说明"知识库中没有相关信息"

**ReAct 推理流程嵌入 Prompt：**
```
1. 分析问题 → 2. 检索知识库 → 3. 评估结果 → 4. 组织回答 → 5. 信息不足则再检索或说明
```

### 四层防幻觉策略

| 层 | 策略 | 位置 |
|---|------|------|
| 1 | System Prompt 硬约束 —— "禁止编造" | `agent/prompts.py` |
| 2 | 检索为空兜底 —— "我的知识库中没有这方面的信息" | `agent/chat_agent.py` |
| 3 | verify_answer_against_kb 工具 —— LLM 自我校验，匹配度 < 0.5 标记不可信 | `agent/tools.py` |
| 4 | **RAGAS 评测 + 诚实度量化** —— 用知识库外的"幻觉检测"类问题验证系统是否如实说不知道 | `core/eval_runner.py` |

第 4 层是**可量化**的：评测集里有 5 道知识库中本就没有答案的题
（"马佳良的期望薪资是多少？"），系统如实回答"没有相关信息"才算通过，
汇总成 `honesty_rate`。指标低于 1 时会直接给出改进建议。

### Vibe Coding / AI 辅助开发实践

本项目全程使用 **Claude Code（AI 辅助编程）** 完成：
- **架构设计**：由 Claude 根据需求生成技术方案，人工 review 后确认
- **代码生成**：Claude 生成主体代码（Vue 3 组件、FastAPI 路由、LangGraph 编排、Docker 配置）
- **迭代优化**：遇到问题 → 描述给 Claude → Claude 分析 → 提出修复 → 验证
- **部署配置**：Dockerfile、docker-compose、Nginx、CI/CD 全部由 Claude 生成

**Vibe Coding 心得：**
- 越具体的需求描述 → Claude 生成的代码越准确
- 分阶段迭代优于一次性全量生成（先跑通核心链路，再逐步加功能）
- 上下文管理很重要：CLAUDE.md 作为项目说明书，每次对话都加载
- AI 擅长生成已知模式（CRUD、中间件、Docker 配置），但业务逻辑需要人工把关

---

## 三、AI 调用逻辑

### SSE 流式对话（POST /api/chat/stream）

```
客户端 fetch → ReadableStream
    ↓
POST /api/chat/stream (Authorization: Bearer <JWT>)
Content-Type: text/event-stream
    ↓
后端起 LangGraph graph.astream_events(state, version="v2")
    ↓
SSE 事件流：
  data: {"type":"reasoning","content":"🔍 调用工具: search_knowledge_base","icon":"🔍"}
  data: {"type":"reasoning_delta","content":"我需要先看看知识库里……"}
  data: {"type":"answer","content":"根据知识库记录，我参与过"}
  data: {"type":"usage","content":"{\"input_tokens\":4083,\"reasoning_tokens\":108,...}"}
  data: {"type":"done","content":"{\"conversation_id\":\"xxx\",\"answer\":\"（权威全文）\"}"}
```

**事件类型：**

| type | 含义 |
|---|---|
| `reasoning` | 一整行"步骤"（工具调用/检索结果），行首带 emoji，另附 `icon` 字段 |
| `reasoning_delta` | 模型**真实思考**的 token 增量（推理模型） |
| `answer` | 答案 token 增量 |
| `usage` | 真实 token 用量（含 reasoning / 缓存命中明细） |
| `done` | 流结束，携带 conversation_id 与**权威全文** |
| `error` | 错误 |

**关键实现细节：**

- **真实流式**：走 `astream_events`。实测首字节 **1.1s**、思考流 **3.0s** 开始，
  总耗时 9~12s —— 换成"先跑完再假打字机"就是全程白屏
- **思考与答案分流**：推理模型先输出 `reasoning_content`（思考）再输出
  `content`（答案），两者必须分开推送，否则思考阶段前端收到的全是空串
- **`done` 带权威全文**：ReAct 循环中间轮次也可能吐正文（"我先查一下……"），
  这些增量已实时下发给前端；结束时以 agent 节点的 `final_answer` 为准替换显示
  并落库，保证"界面看到的" = "存进数据库的"
- `max_tokens` 对推理模型是"思考 + 答案"的**共享预算**：给太小会把答案挤空
  （实测 `max_tokens=32` 时 `content` 直接是空串），因此默认给到 8192
- AbortController 支持客户端中断（点"停止"）；中断时**已生成的内容照样落库**
  —— 取消路径下不能 await，因此在一个新线程+新事件循环里同步完成写入
- 真实 token 用量取自 `usage_metadata`（含 reasoning_tokens 与缓存命中），
  不再靠字符数估算

### Function Calling / 工具绑定

Chat Agent 绑定了 5 个 ReAct 工具：

```python
# agent/tools.py 中通过 @tool 装饰器定义
@tool
def search_knowledge_base(query: str) -> str:
    """搜索个人知识库，获取与 query 最相关的信息片段。"""
    ...


# Chat Agent 节点中绑定
llm_with_tools = llm.bind_tools(
    [
        search_knowledge_base,
        list_my_files,
        get_kb_summary,
        get_chat_context,
        verify_answer_against_kb,
    ]
)
```

**ReAct 循环调用流程：**
```
1. Chat Agent 收到消息 + 检索结果
2. LLM 判断：是否需要调用工具？
   - YES → 返回 tool_calls → Tool Executor 执行 →
     结果注入 messages → 回到 Chat Agent（循环）
   - NO  → 生成最终回答 → END
3. 最多 5 轮循环，防止死循环
4. 每次工具调用都记录在 reasoning log 中，前端可展开查看
```

### OpenAI 兼容接口调用

```python
# 通过 langchain_deepseek 调用（模型/端点/base_url 全部来自 config/.env）
from langchain_deepseek import ChatDeepSeek

llm = ChatDeepSeek(
    model=settings.deepseek_model,  # 推理模型：先输出思考再输出答案
    api_key=settings.deepseek_api_key,
    api_base=settings.deepseek_base_url,
    temperature=0.3,  # 低温度，减少幻觉
    streaming=True,  # 启用流式
    max_tokens=settings.llm_max_tokens,  # 注意：思考与答案**共享**这个预算
)
```

**为什么选 temperature=0.3：** 知识库问答需要事实准确性而非创意性，低温减少编造。

---

## 四、部署步骤（含 DNS / HTTPS）

### 前置条件

- 一台云服务器（阿里云 ECS / 轻量应用服务器，2C4G 起步）
- 一个域名（可选，用于 HTTPS）
- Docker & Docker Compose 已安装

### 第 1 步：克隆项目

```bash
git clone https://github.com/jialiang135/jialiangma.git /opt/personal-agent
cd /opt/personal-agent
```

### 第 2 步：配置 API Key

```bash
# 创建 .env 文件
cat > config/.env << 'EOF'
DEEPSEEK_API_KEY=sk-your-deepseek-api-key-here
DEEPSEEK_BASE_URL=https://api.deepseek.com
DASHSCOPE_API_KEY=sk-your-dashscope-api-key-here
JWT_SECRET_KEY=your-random-secret-key-change-this
EOF
```

### 第 3 步：启动服务

```bash
# 完整栈（含 Redis + Prometheus + Grafana）
docker compose up -d --build

# 或核心服务（节省资源）
docker compose up -d app nginx redis
```

默认访问 http://服务器IP:8080，管理员 `admin` / `admin123456`。

### 第 4 步（可选）：配置 DNS

在域名 DNS 管理中添加 A 记录：
- 主机记录：`@` 或 `www`
- 记录类型：`A`
- 记录值：`39.106.191.98`（你的服务器公网 IP）

### 第 5 步（可选）：配置 HTTPS

```bash
# 1. 安装 certbot
sudo yum install -y certbot  # Alibaba Cloud Linux / CentOS
# 或 sudo apt install -y certbot  # Ubuntu

# 2. 先停掉 Nginx（释放 80 端口）
docker compose stop nginx

# 3. 获取免费证书
sudo certbot certonly --standalone -d your-domain.com

# 4. 证书路径（certbot 默认输出）
# 证书: /etc/letsencrypt/live/your-domain.com/fullchain.pem
# 私钥: /etc/letsencrypt/live/your-domain.com/privkey.pem

# 5. 启用 HTTPS 配置
# 编辑 nginx/nginx.conf，取消 HTTPS server 块的注释
# 填入证书路径和域名

# 6. 更新 docker-compose.yml，挂载证书目录
#    - /etc/letsencrypt:/etc/letsencrypt:ro

# 7. 重启 Nginx
docker compose up -d nginx

# 8. 证书自动续签（crontab）
echo "0 3 * * * certbot renew --quiet && docker compose -f /opt/personal-agent/docker-compose.yml restart nginx" | crontab -
```

HTTPS 配置模板见 [nginx/nginx-ssl.conf](nginx/nginx-ssl.conf)。

### 第 6 步：配置防火墙

阿里云轻量应用服务器 → 实例详情 → 防火墙 → 添加规则：
- 端口：`8080`（HTTP）/ `443`（HTTPS）
- 协议：TCP
- 来源：`0.0.0.0/0`

### 日常运维

```bash
# 查看实时日志
docker logs -f personal-agent

# 查看容器状态
docker compose ps

# 健康巡检
./scripts/health_check.sh

# 回滚到指定版本
./scripts/rollback.sh v1.0.0

# 更新部署
cd /opt/personal-agent
git pull
docker compose up -d --build

# 查看 Swagger 文档
# http://服务器IP:8080/api/docs
```

---

## 本地开发

```bash
# 运行时依赖（生产镜像只装这份）
pip install -r requirements.txt

# 开发依赖（含测试与 lint 工具）
pip install -r requirements-dev.txt

# 质量门禁（与 CI 完全一致）
ruff check .                      # 规则集与豁免项见 pyproject.toml
pytest tests/ -q --cov            # 慢测试默认跳过；覆盖率门槛 55%
cd frontend && npm run build      # 前端构建

# 可选：提交前自动跑 lint（首次运行需从 GitHub 拉钩子仓库）
pip install pre-commit && pre-commit install
```

**几个容易踩的点：**

- **配置是启动时快照**：改了 `config/.env` 必须重启进程才生效
- **测试用独立临时库**：`tests/conftest.py` 在导入应用前把 db/上传目录/向量库
  指向临时目录，跑测试不会碰真实数据
- **依赖全部钉死版本**：这既防"langchain 世代混装导致静默丢功能"，
  也防"本地验证过的栈与容器里的不一致"。升级时整组升并重跑验证
- **运行时与开发依赖已分开**：`ruff`/`pytest`/`pre-commit` 不再进生产镜像
- **推理模型的 `max_tokens` 是思考+答案共享预算**：给太小会把答案挤空

---

## 技术栈

| 层 | 技术 |
|------|------|
| 前端 | Vue 3, Vite, Vue Router, Pinia, 纯 CSS 响应式, DOMPurify（Markdown 净化） |
| 后端 | FastAPI, LangGraph 1.x, LangChain 1.x, SSE 真实流式 |
| AI | DeepSeek（推理模型）, DashScope Embedding + Rerank |
| 数据 | SQLAlchemy 2.0 async + aiosqlite（WAL）, ChromaDB, Redis |
| 检索 | 结构感知分块, BM25 + 向量混合检索（RRF）, Rerank 重排, TTL 缓存 |
| 评测 | RAGAS（faithfulness / relevancy / context precision & recall）+ 自建诚实度 |
| 安全 | JWT + bcrypt, slowapi 限流, 路径穿越防护, 上传校验, 审计日志 |
| 可靠性 | 熔断器（指数退避）, 优雅降级, 有界任务线程池 |
| 运维 | Docker Compose, Nginx, Prometheus, Grafana |

## 项目结构

```
personal_agent/
├── agent/                LangGraph 多智能体（graph_workflow / tools / prompts）
├── api/                  FastAPI 接口层（32 个业务接口 + SSE 流式 + 中间件）
├── core/                 核心模块（auth / audit / circuit_breaker / session / scheduler）
├── rag/                  RAG 检索链路（loader / splitter / vector_store / retriever / bm25）
├── config/               配置（settings + .env + prometheus + grafana）
├── frontend/             Vue 3 前端（Vite 构建，纯 CSS 响应式）
├── scripts/              运维脚本（rollback / health_check）
├── nginx/                Nginx 配置（反向代理 + 限流 + HTTPS 模板）
├── .github/workflows/    CI/CD 流水线（GitHub Actions）
├── assets/               运行时数据（chroma_db / upload_docs / sample_kb）
├── logs/                 日志（Loguru 轮转，30 天保留）
├── Dockerfile            多阶段构建
├── docker-compose.yml    编排文件（app + nginx + redis + prometheus + grafana）
└── requirements.txt
```
