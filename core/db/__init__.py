"""
core/db —— 数据库层（按实体拆分的包）
====================================

拆分前是单文件 1366 行 / 57 个函数，找 `get_conversations` 得翻页。
现在按实体分文件，每块 100~250 行。

数据库层只有这一个入口。按实体分模块，例如
`from core.db.chats import get_conversations` —— 一眼看出依赖的是哪块数据。
"""

from core.db.audit import (
    cleanup_old_audit_logs,
    count_audit_logs,
    get_audit_logs,
    insert_audit_log,
)
from core.db.base import (
    Base,
    UTCDateTime,
    get_db_path,
    utcnow,
)
from core.db.chats import (
    delete_conversation,
    get_all_chat_logs,
    get_chat_by_conversation_id,
    get_chat_history,
    get_chat_history_count,
    get_chat_log_by_id,
    get_conversations,
    insert_chat_log,
)
from core.db.engine import (
    async_session_factory,
    backup_database,
    bind_main_loop,
    dispose_engine,
    engine,
    get_session,
    init_database,
    run_async_blocking,
    run_async_from_thread,
    session_scope,
)
from core.db.eval_reports import (
    create_eval_report,
    delete_eval_report,
    get_eval_report,
    list_eval_reports,
    update_eval_report,
)
from core.db.files import (
    create_upload_task,
    delete_all_file_records,
    delete_file_record,
    find_file_by_hash_or_name,
    get_all_files,
    get_file_by_id,
    get_files_by_owner,
    get_upload_task,
    insert_file_record,
    update_file_chunk_count,
    update_file_hash,
    update_upload_task,
)
from core.db.models import (
    AuditLog,
    ChatLog,
    EvalReport,
    FileRecord,
    LoginAttempt,
    TokenUsage,
    UploadTask,
    User,
)
from core.db.stats import (
    get_global_stats,
)
from core.db.users import (
    check_login_locked,
    count_recent_failed_logins,
    create_admin_user,
    create_user,
    delete_user_cascade,
    get_all_users,
    get_user_by_id,
    get_user_by_username,
    get_users_with_counts,
    record_login_attempt,
    update_user_role,
)

__all__ = [
    "AuditLog",
    "Base",
    "ChatLog",
    "EvalReport",
    "FileRecord",
    "LoginAttempt",
    "TokenUsage",
    "UTCDateTime",
    "UploadTask",
    "User",
    "async_session_factory",
    "backup_database",
    "bind_main_loop",
    "check_login_locked",
    "cleanup_old_audit_logs",
    "count_audit_logs",
    "count_recent_failed_logins",
    "create_admin_user",
    "create_eval_report",
    "create_upload_task",
    "create_user",
    "delete_all_file_records",
    "delete_conversation",
    "delete_eval_report",
    "delete_file_record",
    "delete_user_cascade",
    "dispose_engine",
    "engine",
    "find_file_by_hash_or_name",
    "get_all_chat_logs",
    "get_all_files",
    "get_all_users",
    "get_audit_logs",
    "get_chat_by_conversation_id",
    "get_chat_history",
    "get_chat_history_count",
    "get_chat_log_by_id",
    "get_conversations",
    "get_db_path",
    "get_eval_report",
    "get_file_by_id",
    "get_files_by_owner",
    "get_global_stats",
    "get_session",
    "get_upload_task",
    "get_user_by_id",
    "get_user_by_username",
    "get_users_with_counts",
    "init_database",
    "insert_audit_log",
    "insert_chat_log",
    "insert_file_record",
    "list_eval_reports",
    "record_login_attempt",
    "run_async_blocking",
    "run_async_from_thread",
    "session_scope",
    "update_eval_report",
    "update_file_chunk_count",
    "update_file_hash",
    "update_upload_task",
    "update_user_role",
    "utcnow",
]
