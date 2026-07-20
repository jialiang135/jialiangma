# 个人数字分身 · 多 Agent 私有 RAG 系统

基于 **LangGraph + DeepSeek V4 Pro** 的多智能体知识库问答系统，用于 AI Agent 岗位面试演示。

## 技术栈

| 层级 | 技术 |
|------|------|
| Agent 编排 | LangGraph · ReAct 模式 |
| 大模型 | DeepSeek V4 Pro |
| Embedding | 阿里云 DashScope text-embedding-v4 |
| Rerank | 阿里云 DashScope gte-rerank |
| 向量库 | ChromaDB |
| 后端 | FastAPI · SQLite · SSE 流式 |
| 前端 | Vue 3 · Vite · Pinia |
| 部署 | Docker · Docker Compose |

## 快速启动

```bash
# 1. 配置 API Key
cp config/.env.example config/.env
# 编辑 .env，填入 DeepSeek 和 DashScope 的 API Key

# 2. 安装依赖
pip install -r requirements.txt

# 3. 启动
python main.py
# → http://localhost:7860（前端页面）
# → http://localhost:7860/api/docs（Swagger 文档）
```

默认管理员：`admin` / `admin123456`（可在 `.env` 中修改）。

## Docker 部署

```bash
docker compose up -d --build
# → http://localhost:7863
```

## 项目结构

```
personal_agent/
├── agent/             # LangGraph Agent（chat / manage / eval）
│   ├── chat_agent.py  # ReAct 问答节点
│   ├── eval_agent.py  # 自动评测 + 幻觉检测
│   ├── tools.py       # 5 个 ReAct 工具
│   └── graph_workflow.py  # 状态图编排
├── api/               # FastAPI 路由 + SSE 流式
├── core/              # 鉴权、数据库、数据模型
├── rag/               # 文档解析 → 分块 → 向量化 → 检索 → 重排
├── frontend/          # Vue 3 前端
├── config/            # 全局配置（pydantic-settings）
├── tests/             # pytest 全链路测试
└── assets/            # 运行时数据（chroma_db、upload_docs）
```

## 核心特性

- **ReAct 多 Agent**：路由器 → 检索 → 推理 ⇄ 工具调用 → 最终回答
- **两阶段检索**：ChromaDB 语义搜索 → DashScope gte-rerank 重排
- **防幻觉**：系统提示词约束 + `verify_answer_against_kb` 工具自检
- **自动评测**：批量问答 + LLM 幻觉检测 + 评测报告
- **多租户隔离**：owner_id 贯穿向量库、SQLite、JWT
- **优雅降级**：Rerank 失败 → 回退原始排序；OCR 未装 → 跳过
- **SSE 流式**：推理步骤 + 回答文本实时推送
