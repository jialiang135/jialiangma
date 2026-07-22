"""
定时任务管理
日志清理、数据备份、健康巡检、Token 用量统计
"""
import os
import time
import sqlite3
from pathlib import Path
from loguru import logger

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    APSCHEDULER_AVAILABLE = True
except ImportError:
    APSCHEDULER_AVAILABLE = False


PROJECT_ROOT = Path(__file__).parent.parent.resolve()
DB_PATH = PROJECT_ROOT / "assets" / "personal_agent.db"
LOG_DIR = PROJECT_ROOT / "logs"
ASSETS_DIR = PROJECT_ROOT / "assets"


def _get_conn():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


# ─── 定时任务函数 ───

def cleanup_old_logs(retention_days: int = 30):
    """清理过期日志文件"""
    cutoff = time.time() - retention_days * 86400
    count = 0
    for f in LOG_DIR.glob("*.log*"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                count += 1
        except OSError:
            pass
    if count:
        logger.info(f"🗑 清理了 {count} 个过期日志文件")


def cleanup_old_audit_logs(retention_days: int = 90):
    """清理过期审计日志"""
    try:
        conn = _get_conn()
        conn.execute(
            "DELETE FROM audit_log WHERE created_at < datetime('now', '-{} days')".format(retention_days)
        )
        deleted = conn.rowcount
        conn.commit()
        conn.close()
        if deleted:
            logger.info(f"🗑 清理了 {deleted} 条过期审计日志")
    except Exception as e:
        logger.warning(f"清理审计日志失败: {e}")


def backup_database():
    """SQLite 数据库备份"""
    backup_dir = ASSETS_DIR / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"personal_agent_{timestamp}.db"

    try:
        import shutil
        shutil.copy2(DB_PATH, backup_path)
        # 只保留最近 7 天的备份
        backups = sorted(backup_dir.glob("personal_agent_*.db"))
        for old in backups[:-7]:
            old.unlink()
        logger.info(f"💾 数据库已备份: {backup_path.name}")
    except Exception as e:
        logger.error(f"数据库备份失败: {e}")


def health_check_job():
    """定时健康检查（输出版本 + 容器状态）"""
    import psutil
    disk = psutil.disk_usage("/")
    mem = psutil.virtual_memory()
    logger.info(
        f"🏥 健康检查 | "
        f"CPU: {psutil.cpu_percent(interval=1)}% | "
        f"内存: {mem.percent}% | "
        f"磁盘: {disk.percent}% | "
        f"进程数: {len(psutil.pids())}"
    )


# ─── 调度器启动 ───

_scheduler = None


def start_scheduler():
    """启动定时任务调度器"""
    global _scheduler
    if not APSCHEDULER_AVAILABLE:
        logger.warning("⚠️ APScheduler 未安装，定时任务不可用。pip install apscheduler")
        return None

    if _scheduler is not None:
        return _scheduler

    _scheduler = BackgroundScheduler(timezone="Asia/Shanghai")

    # 每日凌晨 3 点：清理日志
    _scheduler.add_job(cleanup_old_logs, "cron", hour=3, minute=0,
                       kwargs={"retention_days": 30})
    # 每日凌晨 4 点：备份数据库
    _scheduler.add_job(backup_database, "cron", hour=4, minute=0)
    # 每周日凌晨 5 点：清理审计日志
    _scheduler.add_job(cleanup_old_audit_logs, "cron", day_of_week=0, hour=5, minute=0,
                       kwargs={"retention_days": 90})
    # 每 30 分钟：健康检查
    _scheduler.add_job(health_check_job, "interval", minutes=30)

    _scheduler.start()
    logger.info("⏰ 定时任务调度器已启动（日志清理/数据库备份/健康巡检）")
    return _scheduler


def stop_scheduler():
    """停止调度器"""
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("⏰ 定时任务调度器已停止")
