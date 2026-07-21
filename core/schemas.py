"""
Pydantic 数据模型定义
"""
from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime


# ========================================
# 用户 / 鉴权
# ========================================

class UserCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=6, max_length=128)


class UserLogin(BaseModel):
    username: str
    password: str


class UserRegister(BaseModel):
    """用户注册——默认注册为普通用户"""
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=6, max_length=128)


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
    conversation_id: Optional[str] = None


class ChatLogOut(BaseModel):
    id: int
    owner_id: int
    agent_mode: str
    conversation_id: Optional[str] = None
    question: str
    answer: str
    reasoning: Optional[str] = None
    sources: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class ChatHistoryResponse(BaseModel):
    conversations: list[ChatLogOut]
    total: int


class ConversationSummary(BaseModel):
    """侧边栏对话摘要——每条代表一个独立对话组"""
    group_id: str                          # conversation_id 或 '__single_xxx'
    first_log_id: int                      # 该组第一条日志 ID（用于加载详情）
    turn_count: int                        # 对话轮数
    last_at: Optional[str] = None          # 最后活跃时间
    first_question: Optional[str] = None   # 第一轮问题（作为对话标题）


class ConversationListResponse(BaseModel):
    conversations: list[ConversationSummary]
    total: int


# ========================================
# 通用响应
# ========================================

class APIResponse(BaseModel):
    success: bool
    message: str
    data: Optional[dict] = None
