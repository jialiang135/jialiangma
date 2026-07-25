# 个人数字分身 · 多 Agent 私有 RAG 系统

[![Python](https://img.shields.io/badge/Python-3.12-blue)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green)](https://fastapi.tiangolo.com)
[![Vue](https://img.shields.io/badge/Vue-3-green)](https://vuejs.org)
[![Docker](https://img.shields.io/badge/Docker-ready-blue)](https://docker.com)

> 基于 LangGraph + DeepSeek V4 Pro 的企业级多 Agent 私有知识库问答系统

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
  FastAPI (SSE 流式, 12 组 REST 路由)
      ↓
  LangGraph StateGraph (7 节点有向图)
      ├── Router 路由节点
      ├── Retrieve 检索节点
      ├── Chat Agent (ReAct 循环, 最多 5 轮)
      ├── Tool Executor (5 个 ReAct 工具)
      ├── Manage Agent (知识库管理)
      └── Eval Agent (自动评测)
      ↓
  ┌───────────┬────────────┬──────────────┐
  ChromaDB    SQLite       Redis          DeepSeek V4 Pro
  (向量存储)  (元数据+审计) (缓存+会话+队列) (LLM)
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
- Router 根据 `agent_mode` 路由到不同 Agent 节点（条件边）
- Chat 模式采用标准 ReAct 循环：思考 → 调工具 → 观察 → 再思考
- `should_continue_chat()` 检查 `needs_tool_call` 和 `iteration_count < 5`，防止无限循环
- 每个节点都是纯函数，状态通过 `AgentState` TypedDict 传递

### 数据隔离（多租户就绪）

所有数据层携带 `owner_id`：
- ChromaDB：`metadata={"owner_id": N}` + 查询 `filter={"owner_id": owner_id}`
- SQLite：所有查询 `WHERE owner_id = ?`
- JWT：payload 含 `sub`（owner_id）→ FastAPI `Depends(get_current_user)` 注入

### 两阶段 RAG 检索

```
用户问题
    → ChromaDB 语义搜索 topK=10
    → [可选] BM25 关键词 + RRF 融合
    → DashScope gte-rerank 重排 topK=5
    → 注入 System Prompt
    → LLM 生成回答
```

优雅降级：Rerank API 失败 → 自动回退 ChromaDB 原始排序（score=1.0）

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
| 4 | 评测集验证 —— 故意问知识库外的问题，验证系统诚实度 | `assets/test_data/` |

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
后端起 LangGraph graph.ainvoke(initial_state)
    ↓
SSE 事件流：
  data: {"type":"reasoning","content":"🔍 正在检索知识库..."}
  data: {"type":"reasoning","content":"🤔 分析检索结果..."}
  data: {"type":"answer","content":"根据知识库记录，我参与过..."}
  data: {"type":"done","content":"","conversation_id":"xxx"}
```

**关键实现细节：**
- 单次连接，多次推送（reasoning → answer → done）
- `Cache-Control: no-cache`，`X-Accel-Buffering: no`（禁用 Nginx 缓冲）
- AbortController 支持客户端中断（点"停止"按钮）
- 流式结束后自动保存对话日志到 SQLite

### Function Calling / 工具绑定

Chat Agent 绑定了 5 个 ReAct 工具：

```python
# agent/tools.py 中通过 @tool 装饰器定义
@tool
def search_knowledge_base(query: str) -> str:
    """搜索个人知识库，获取与 query 最相关的信息片段。"""
    ...

# Chat Agent 节点中绑定
llm_with_tools = llm.bind_tools([search_knowledge_base, list_my_files,
                                  get_kb_summary, get_chat_context,
                                  verify_answer_against_kb])
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
# 通过 langchain_deepseek 调用 DeepSeek V4 Pro
from langchain_deepseek import ChatDeepSeek

llm = ChatDeepSeek(
    model="deepseek-v4-pro",
    api_key=settings.deepseek_api_key,
    api_base="https://api.deepseek.com",
    temperature=0.3,      # 低温度，减少幻觉
    streaming=True,        # 启用流式
    max_tokens=4096,
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

## 技术栈

| 层 | 技术 |
|------|------|
| 前端 | Vue 3, Vite, Vue Router, Pinia, 纯 CSS 响应式 |
| 后端 | FastAPI, LangGraph 1.x, LangChain, SSE 流式 |
| AI | DeepSeek V4 Pro, DashScope Embedding, Rerank |
| 存储 | ChromaDB, SQLite, Redis |
| 安全 | JWT + bcrypt, slowapi 限流, 审计日志, 熔断降级 |
| 运维 | Docker Compose, Nginx, Prometheus, Grafana |

## 项目结构

```
personal_agent/
├── agent/                LangGraph 多智能体（graph_workflow / tools / prompts）
├── api/                  FastAPI 接口层（12 组路由 + SSE 流式 + 中间件）
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
