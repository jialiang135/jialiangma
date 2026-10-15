#!/bin/bash
# ═══════════════════════════════════════════
# 健康巡检脚本
# 检查：API 健康 / 磁盘 / 内存 / Docker 容器状态
# 配合 cron 定时执行：*/5 * * * * /opt/personal-agent/scripts/health_check.sh
# ═══════════════════════════════════════════
set -e

BASE_URL="${BASE_URL:-http://localhost:8080}"
DINGTALK_WEBHOOK="${DINGTALK_WEBHOOK:-}"
LOG_FILE="/opt/personal-agent/logs/health_check.log"

ALERT=0
MSG=""

# ─── 1. API 健康检查 ───
if curl -sf --max-time 10 "$BASE_URL/api/health" > /dev/null 2>&1; then
    MSG="${MSG}✅ API 正常\n"
else
    MSG="${MSG}❌ API 不可达！\n"
    ALERT=1
fi

# ─── 2. 磁盘使用率 ───
DISK_USAGE=$(df -h / | awk 'NR==2 {print $5}' | sed 's/%//')
if [ "$DISK_USAGE" -gt 85 ]; then
    MSG="${MSG}⚠️ 磁盘使用率: ${DISK_USAGE}% (阈值 85%)\n"
    ALERT=1
else
    MSG="${MSG}✅ 磁盘: ${DISK_USAGE}%\n"
fi

# ─── 3. 内存 ───
MEM_FREE=$(free -m | awk 'NR==2 {print $7}')
MEM_TOTAL=$(free -m | awk 'NR==2 {print $2}')
MEM_PCT=$((100 - MEM_FREE * 100 / MEM_TOTAL))
if [ "$MEM_PCT" -gt 80 ]; then
    MSG="${MSG}⚠️ 内存使用率: ${MEM_PCT}% (阈值 80%)\n"
    ALERT=1
else
    MSG="${MSG}✅ 内存: ${MEM_PCT}%\n"
fi

# ─── 4. Docker 容器状态 ───
if docker compose -f /opt/personal-agent/docker-compose.yml ps | grep -q "unhealthy\|Restarting"; then
    MSG="${MSG}❌ 容器异常！\n"
    ALERT=1
else
    MSG="${MSG}✅ Docker 容器正常\n"
fi

# ─── 5. 日志 ───
echo "[$(date '+%Y-%m-%d %H:%M:%S')]" >> "$LOG_FILE"
echo -e "$MSG" | tee -a "$LOG_FILE"

# ─── 6. 钉钉告警 ───
if [ "$ALERT" -eq 1 ] && [ -n "$DINGTALK_WEBHOOK" ]; then
    curl -s -H "Content-Type: application/json" \
        -X POST "$DINGTALK_WEBHOOK" \
        -d "{\"msgtype\":\"text\",\"text\":{\"content\":\"🚨 个人数字分身 健康告警\n$(echo -e $MSG)\"}}" \
        > /dev/null 2>&1 || true
fi

exit $ALERT
