#!/bin/bash
# ═══════════════════════════════════════════
# 回滚脚本 — 一键回滚到指定版本
# 用法: ./scripts/rollback.sh v1.2.0
# ═══════════════════════════════════════════
set -e

VERSION=${1:-}
REGISTRY="${REGISTRY:-registry.cn-hangzhou.aliyuncs.com}"
NAMESPACE="${NAMESPACE:-personal-agent}"
IMAGE="${REGISTRY}/${NAMESPACE}/personal-agent"

if [ -z "$VERSION" ]; then
    echo "❌ 请指定回滚版本号"
    echo "用法: $0 <version-tag>"
    echo "示例: $0 v1.2.0"
    echo ""
    echo "可用版本:"
    docker images "$IMAGE" --format "  {{.Tag}}" | sort -r
    exit 1
fi

echo "⏪ 正在回滚到版本: $VERSION"
echo ""

# 1. 拉取指定版本镜像
echo "📦 拉取镜像 $IMAGE:$VERSION ..."
docker pull "$IMAGE:$VERSION"

# 2. 标记为当前使用版本
CURRENT_TAG=$(docker inspect personal-agent-app --format '{{index .Config.Labels "org.opencontainers.image.version"}}' 2>/dev/null || echo "unknown")
echo "📋 当前版本: $CURRENT_TAG"

# 3. 切换到指定版本
cd /opt/personal-agent
export IMAGE_TAG="$VERSION"
docker compose up -d --no-build --force-recreate

# 4. 验证
sleep 5
if curl -sf http://localhost:8080/api/health > /dev/null 2>&1; then
    echo "✅ 回滚成功！当前版本: $VERSION"
else
    echo "⚠️ 回滚后健康检查失败，请手动排查"
    echo "💡 可回滚到上一版本: docker compose up -d --no-build"
fi

# 5. 记录回滚
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 回滚 $CURRENT_TAG → $VERSION" >> /opt/personal-agent/logs/rollback.log
