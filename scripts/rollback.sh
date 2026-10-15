#!/usr/bin/env bash
# Legacy entry point retained so old documentation/commands fail safely.
# The current docker-compose.yml builds from source and has no registry image/tag
# configuration, so this registry-based rollback cannot work for this deployment.
set -euo pipefail

cat >&2 <<'EOF'
自动镜像回滚当前不可用：本项目生产 Compose 使用 tar+SSH 同步源码并在服务器构建，
没有配置镜像仓库或 IMAGE_TAG。为避免误拉取镜像/误切换容器，本脚本已禁用。
请按 docs/DEPLOY.md 的流程操作；需要自动回滚时，先实现并验证镜像标签发布/保留策略。
EOF
exit 2
