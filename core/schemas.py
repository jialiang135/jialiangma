"""
Pydantic 数据模型定义
"""

from datetime import datetime

from pydantic import BaseModel, Field

from config.settings import settings

# ========================================
# 用户名 / 密码的长度约束 —— 集中在这里
# ----------------------------------------
# 为什么要有这组常量：原先 `UserCreate` / `UserRegister` 各自写死
# `min_length=6`，而 `core.auth.validate_password_strength` 要求的是
# `settings.password_min_length`（默认 8）。结果是 6~7 位的密码**能通过
# schema 校验、再被处理器打回**，而且报错格式还不一样（422 vs 400）。
# 现在两处引用同一组常量，注册页展示的规则也从这组常量出（见 AuthRequirements）。
# ========================================

USERNAME_MIN_LENGTH = 3
USERNAME_MAX_LENGTH = 50
PASSWORD_MAX_LENGTH = 128
PASSWORD_MIN_LENGTH = settings.password_min_length


class UserCreate(BaseModel):
    username: str = Field(..., min_length=USERNAME_MIN_LENGTH, max_length=USERNAME_MAX_LENGTH)
    password: str = Field(..., min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH)


class UserLogin(BaseModel):
    username: str
    password: str


class UserRegister(BaseModel):
    """用户注册——默认注册为普通用户"""

    username: str = Field(..., min_length=USERNAME_MIN_LENGTH, max_length=USERNAME_MAX_LENGTH)
    password: str = Field(..., min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH)


class AuthRequirements(BaseModel):
    """
    登录/注册页需要展示的校验规则（公开端点，无需鉴权）。

    存在的意义是**让前端不必再抄一份规则**：注册页的"实时校验清单"
    直接照这个渲染，后端改了规则前端自动跟着变，不会出现两处漂移。
    `allow_registration` 同理 —— 注册关掉时前端据此直接显示"未开放注册"，
    而不是让用户填完表单才收到 403。
    """

    allow_registration: bool
    username_min_length: int
    username_max_length: int
    password_min_length: int
    password_max_length: int
    password_require_digit: bool
    password_require_letter: bool


class UserOut(BaseModel):
    id: int
    username: str
    role: str
    created_at: datetime

    model_config = {"from_attributes": True}


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    owner_id: int
    username: str
    role: str = "user"


# ========================================
# 文件管理
# ========================================


class FileMetaOut(BaseModel):
    id: int
    owner_id: int
    filename: str
    filepath: str
    file_size: int
    chunk_count: int
    created_at: datetime

    model_config = {"from_attributes": True}


class FileUploadResponse(BaseModel):
    success: bool
    filename: str
    chunk_count: int
    message: str


class FileDeleteRequest(BaseModel):
    file_id: int


class KnowledgeBaseStats(BaseModel):
    total_files: int
    total_chunks: int
    files: list[FileMetaOut]


# ========================================
# 对话
# ========================================


class ChatRequest(BaseModel):
    message: str
    agent_mode: str = Field(default="chat", pattern="^(chat|manage|eval)$")
    conversation_id: str | None = None


class ChatLogOut(BaseModel):
    id: int
    owner_id: int
    agent_mode: str
    conversation_id: str | None = None
    question: str
    answer: str
    reasoning: str | None = None
    sources: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class ChatHistoryResponse(BaseModel):
    conversations: list[ChatLogOut]
    total: int


class ConversationSummary(BaseModel):
    """侧边栏对话摘要——每条代表一个独立对话组"""

    group_id: str  # conversation_id 或 '__single_xxx'
    first_log_id: int  # 该组第一条日志 ID（用于加载详情）
    turn_count: int  # 对话轮数
    last_at: str | None = None  # 最后活跃时间
    first_question: str | None = None  # 第一轮问题（作为对话标题）


class ConversationListResponse(BaseModel):
    conversations: list[ConversationSummary]
    total: int


# ========================================
# 通用响应
# ========================================


class APIResponse(BaseModel):
    success: bool
    message: str
    data: dict | None = None


# ========================================
# 管理员：用户管理
# ========================================


class UserAdminOut(BaseModel):
    id: int
    username: str
    role: str
    created_at: datetime
    file_count: int = 0
    chat_count: int = 0

    model_config = {"from_attributes": True}


class UserRoleUpdate(BaseModel):
    role: str = Field(..., pattern="^(admin|user)$")


# ========================================
# 管理员：仪表盘
# ========================================


class DashboardStats(BaseModel):
    user_count: int
    file_count: int
    today_chats: int
    total_chats: int
    total_tokens: int
    total_cost: float
    disk_used_mb: float
    chroma_db_mb: float


# ========================================
# 管理员：全局查询
# ========================================


class ChatLogAdminOut(BaseModel):
    id: int
    owner_id: int
    username: str = ""
    agent_mode: str
    conversation_id: str | None = None
    question: str
    answer: str
    reasoning: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class FileAdminOut(BaseModel):
    id: int
    owner_id: int
    username: str = ""
    filename: str
    file_size: int
    chunk_count: int
    created_at: datetime

    model_config = {"from_attributes": True}
