#!/usr/bin/env bash
# ============================================
# 个人数字分身 · Docker 启动脚本 (Linux/Mac)
# ============================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

echo "============================================"
echo "  个人数字分身 · Docker 启动"
echo "============================================"

# 检查 config/.env 是否存在
if [ ! -f "config/.env" ]; then
    echo "[WARNING] config/.env 不存在！"
    echo "         请复制 config/.env.example 或手动创建 config/.env 并填入 API Key。"
    echo ""
fi

# 检查必要的 API Key 是否已配置
if [ -f "config/.env" ]; then
    source config/.env 2>/dev/null || true
    if [ -z "$DEEPSEEK_API_KEY" ] || [ "$DEEPSEEK_API_KEY" = "sk-your-deepseek-api-key-here" ]; then
        echo "[WARNING] DEEPSEEK_API_KEY 未配置，LLM 推理功能将不可用。"
    fi
    if [ -z "$DASHSCOPE_API_KEY" ] || [ "$DASHSCOPE_API_KEY" = "sk-your-dashscope-api-key-here" ]; then
        echo "[WARNING] DASHSCOPE_API_KEY 未配置，Embedding / Rerank 功能将不可用。"
    fi
fi

echo ""
echo ">>> 构建并启动 Docker 容器..."
docker compose build --pull
docker compose up -d

echo ""
echo "============================================"
echo "  服务已启动！"
echo "  访问地址: http://localhost:7863"
echo "  API 文档: http://localhost:7863/api/docs"
echo "  健康检查: http://localhost:7863/api/health"
echo "============================================"
echo ""
echo "查看日志: docker compose logs -f"
echo "停止服务: docker compose down"
