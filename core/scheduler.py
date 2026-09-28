"""
定时任务管理
日志清理、数据备份、健康巡检、审计日志清理

注意线程模型：APScheduler 的 BackgroundScheduler 在**自己的线程**里执行任务，
那里没有 event loop。而数据层是 async 的，因此所有 DB 操作都要通过
``core.database.run_async_from_thread`` 提交到主循环执行。
"""
import time
from pathlib import Path

from loguru import logger

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    APSCHEDULER_AVAILABLE = True
except ImportError:
    APSCHEDULER_AVAILABLE = False


PROJECT_ROOT = Path(__file__).parent.parent.resolve()
LOG_DIR = PROJECT_ROOT / "logs"
ASSETS_DIR = PROJECT_ROOT / "assets"


# ─── 定时任务函数 ───

def cleanup_old_logs(retention_days: int = 30):
    """清理过期日志文件（纯文件操作，不涉及事件循环）"""
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
    from core.database import cleanup_old_audit_logs as _cleanup
    from core.database import run_async_from_thread

    try:
        deleted = run_async_from_thread(_cleanup(retention_days))
        if deleted:
            logger.info(f"🗑 清理了 {deleted} 条过期审计日志")
    except Exception as e:
        logger.warning(f"清理审计日志失败: {e}")


def backup_database():
    """
    SQLite 数据库备份。

    走 ``VACUUM INTO``（见 core/database.backup_database），**不能用
    shutil.copy2**：启用 WAL 后主库文件可能缺少尚未 checkpoint 的事务，
    裸拷贝会得到不一致的副本。
    """
    from core.database import backup_database as _backup
    from core.database import run_async_from_thread

    backup_dir = ASSETS_DIR / "backups"
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"personal_agent_{timestamp}.db"

    try:
        run_async_from_thread(_backup(backup_path))
        # 只保留最近 7 份
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
