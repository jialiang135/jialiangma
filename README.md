# 个人数字分身 · AI 面试助手

[![Python](https://img.shields.io/badge/Python-3.12-blue)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green)](https://fastapi.tiangolo.com)
[![Vue](https://img.shields.io/badge/Vue-3-green)](https://vuejs.org)
[![Docker](https://img.shields.io/badge/Docker-ready-blue)](https://docker.com)

> 基于 LangGraph + DeepSeek V4 Pro 的企业级多 Agent 私有知识库问答系统

**🌐 在线地址：http://39.106.191.98**

---

## 架构全景

```
手机/桌面浏览器
      ↓
   Nginx (反向代理 + 限流 + HTTPS)
      ↓
   FastAPI (SSE 流式)
      ↓
   LangGraph ReAct Agent
      ├── ChromaDB (向量存储)
      ├── Redis (缓存 + 会话 + 队列)
      ├── SQLite (元数据 + 审计日志)
      ├── DeepSeek V4 Pro (LLM)
      └── DashScope (Embedding + Rerank)

监控: Prometheus + Grafana  |  容器: Docker Compose
```

## 核心功能

### 🤖 智能对话
- ReAct 多 Agent 架构，SSE 流式输出
- 推理过程可视化（思考步骤时间线）
- 5 个 ReAct 工具：知识库检索、文件列表、内容摘要、上下文、幻觉校验
- 防幻觉策略：检索为空强制兜底回答

### 📚 知识库管理
- 支持 PDF / Word / Excel / TXT / Markdown / 代码 / 图片(OCR) / ZIP 等 15 种格式
- 文档切片 → 清洗 → 去重 → 向量化全链路
- 混合检索：语义搜索 + BM25 关键词 + DashScope Rerank 重排序
- 优雅降级：Rerank API 失败自动回退原始排序

### 📱 全端适配
- 手机 / 平板 / 桌面 三档响应式断点（480px / 768px / 1024px）
- 汉堡菜单侧滑导航、刘海屏安全区域适配
- 触摸友好：44px 最小触控目标、减少动画偏好支持

### 🛡️ 企业安全
| 功能 | 说明 |
|------|------|
| JWT 鉴权 | HS256 签名，480 分钟过期 |
| 密码强度 | 至少 8 位，含数字和字母 |
| 登录锁定 | 连续 5 次失败锁定 30 分钟 |
| 请求限流 | IP 级别，默认 60 次/分钟 |
| CORS 白名单 | 生产环境限制域名 |
| 审计日志 | 全量 API 调用记录（用户/操作/耗时/IP） |

### ⚡ 高可用
| 功能 | 说明 |
|------|------|
| 熔断降级 | LLM 失败 ≥5 次自动熔断，返回兜底回答，60s 后自动恢复 |
| 优雅关闭 | SIGTERM 等待请求处理完再退出 |
| 异步队列 | 长任务异步化（文档入库），Redis RQ + 线程池 fallback |
| 定时备份 | 每日凌晨 4 点自动备份数据库，保留 7 天 |

### 🔧 Agent 平台化
| 功能 | 说明 |
|------|------|
| 工具注册中心 | `@register_tool` 装饰器插件式注册，`/api/tools` 查看所有工具 |
| 会话管理 | Redis 持久化 + 24h TTL + 上下文超 8000 token 自动裁剪 |
| 定时任务 | 日志清理(30天)、数据库备份、审计归档(90天)、健康巡检 |

### 📈 可观测性
| 功能 | 说明 |
|------|------|
| Prometheus | `/metrics` 端点，采集 QPS / 延迟 / 错误率 |
| Grafana | 一键起面板，预配 Prometheus 数据源 |
| 健康巡检 | 检查 API / 磁盘 / 内存 / 容器状态，异常钉钉告警 |
| 审计日志 | `GET /api/admin/audit-logs` 查询，SQLite 持久化 |

---

## 快速开始

### 本地开发

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置 API Key
# 编辑 config/.env，填入 DeepSeek 和 DashScope 的 API Key

# 3. 构建前端
cd frontend && npm ci && npm run build && cd ..

# 4. 启动
python main.py
# → 前端 http://localhost:7860
# → Swagger http://localhost:7860/api/docs
```

默认管理员 `admin` / `admin123456`。

### Docker 部署

```bash
# 完整栈（含 Redis + Prometheus + Grafana）
docker compose up -d --build

# 核心服务（节省资源）
docker compose up -d app nginx redis
```

### 服务器部署

```bash
cd /opt/personal-agent
git pull
docker compose up -d --build
```

---

## API 接口

启动后访问 Swagger 文档：http://localhost:7860/api/docs

| 方法 | 路径 | 说明 | 权限 |
|------|------|------|------|
| POST | `/api/auth/login` | 登录 | 公开 |
| POST | `/api/auth/register` | 注册 | 公开 |
| GET | `/api/auth/me` | 当前用户 | 登录 |
| POST | `/api/chat/stream` | SSE 流式对话 | 登录 |
| GET | `/api/chat/conversations` | 历史对话 | 登录 |
| POST | `/api/kb/upload` | 上传文档 | 管理员 |
| GET | `/api/kb/files` | 文件列表 | 登录 |
| DELETE | `/api/kb/files/{id}` | 删除文件 | 管理员 |
| GET | `/api/tools` | 工具列表 | 登录 |
| GET | `/api/admin/audit-logs` | 审计日志 | 管理员 |
| GET | `/api/admin/circuit-status` | 熔断器状态 | 管理员 |
| GET | `/api/admin/queue-status` | 队列状态 | 管理员 |
| GET | `/api/metrics` | Prometheus 指标 | — |

---

## 运维命令

```bash
# 查看实时日志
docker logs -f personal-agent

# 健康巡检
./scripts/health_check.sh

# 回滚到指定版本
./scripts/rollback.sh v1.2.0

# 查看审计日志
curl http://localhost:7860/api/admin/audit-logs \
  -H "Authorization: Bearer <token>"

# 查看熔断器状态
curl http://localhost:7860/api/admin/circuit-status \
  -H "Authorization: Bearer <token>"
```

## 技术栈

| 层级 | 技术 |
|------|------|
| 前端 | Vue 3, Vite, Vue Router, Pinia, 纯 CSS 响应式 |
| 后端 | FastAPI, LangGraph, LangChain, SSE 流式 |
| AI | DeepSeek V4 Pro, DashScope Embedding, Rerank |
| 存储 | ChromaDB, SQLite, Redis |
| 安全 | JWT + bcrypt, slowapi 限流, 审计日志 |
| 运维 | Docker Compose, Nginx, Prometheus, Grafana |

## 项目结构

```
personal_agent/
├── agent/                LangGraph 多智能体（graph_workflow / tools / prompts）
├── api/                  FastAPI 接口层（routes + SSE 流式 + 中间件）
├── core/                 核心模块（auth / database / audit / circuit_breaker / session / scheduler）
├── rag/                  RAG 检索链路（loader / splitter / vector_store / retriever / bm25 / cache）
├── config/               配置（settings + .env + prometheus + grafana）
├── frontend/             Vue 3 前端（Vite 构建，纯 CSS 响应式）
├── scripts/              运维脚本（rollback / health_check）
├── nginx/                Nginx 配置（反向代理 + HTTPS 模板）
├── .github/workflows/    CI/CD 流水线（GitHub Actions）
├── assets/               运行时数据（chroma_db / upload_docs / backups）
├── logs/                 日志（Loguru 轮转）
├── Dockerfile            多阶段构建
├── docker-compose.yml    编排文件（app + nginx + redis + prometheus + grafana）
└── requirements.txt
```
