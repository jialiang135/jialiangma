"""
全局配置模块
使用 pydantic-settings 读取 .env，封装所有模型客户端
"""

import os
from pathlib import Path

from loguru import logger
from pydantic_settings import BaseSettings

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent.resolve()


class Settings(BaseSettings):
    """全局配置，自动从 .env 文件加载"""

    # --- DeepSeek ---
    deepseek_api_key: str = "sk-your-deepseek-api-key-here"
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-pro"
    # 单次回答的输出 token 上限。**推理模型下这个预算由"思考"和"答案"共享**：
    # 思考消耗的 token 也算在内（实测某次问答 completion=310 中 reasoning=217）。
    # 给太小会把答案挤空（max_tokens=32 时 content 直接是空串），
    # 因此留出充足余量。端点实测接受到 32768。
    llm_max_tokens: int = 8192
    # 评测**裁判**模型的输出上限。刻意与上面的回答预算分开。
    #
    # 为什么必须单独给：ragas 要裁判输出"把回答拆成全部断言 + 逐条判定"的**长 JSON**，
    # 而推理模型的预算是思考和答案共享的 —— 上面 8192 对"回答一个问题"够用，
    # 对裁判就不够了：实测 5 道题里 4 道被截断，ragas 抛
    # `LLMDidNotFinishException`，然后**把该样本静默记成 NaN**（聚合时又被剔除，
    # 于是"1 个样本的平均值"被当成整体忠实度显示出来）。
    #
    # 端点实测接受到 32768，给足。
    llm_judge_max_tokens: int = 32768
    # 评测裁判的**并发数**与**单次超时**。
    #
    # 必须调 —— ragas 的默认值对第三方代理太激进（`max_workers=16` /
    # `timeout=180s`）。实测：服务器上 18 个裁判任务**全部 TimeoutError**，
    # 而同一批数据在本地全过；差别就在"16 路并发（每路还是 32768 的大预算）
    # 打到代理上排队"。降到 4 路并发后代理压力小、单次更快，配合 600s 超时
    # 给慢调用留余量。
    eval_judge_concurrency: int = 4
    eval_judge_timeout_seconds: int = 600
    eval_judge_max_retries: int = 3

    # --- 阿里云 DashScope ---
    dashscope_api_key: str = "sk-your-dashscope-api-key-here"
    embedding_model: str = "text-embedding-v4"
    rerank_model: str = "gte-rerank-v2"

    # --- 语音合成（TTS，阿里云 DashScope CosyVoice）---
    # 实测（2026-09）：**模型与音色必须同代配套，混用一律返回
    # "Engine return error code: 418"** —— 那个 418 不是"账号不可用"，
    # 是"这个模型不认这个音色"。三条实测结论：
    #   1. cosyvoice-v2 + longxiaochun_v2 / longwan_v2（预置音色）→ 可用，
    #      流式首包约 0.79 秒
    #   2. cosyvoice-v3.5-flash + 在 v3.5-flash 上复刻的音色
    #      （voice_id 形如 cosyvoice-v3.5-flash-bailian-xxx）→ 可用
    #   3. cosyvoice-v3.5-flash **不认 v2 系预置音色**（longxiaochun_v2 也报 418）；
    #      cosyvoice-v3-flash 本账号确实不可用
    # 换模型/音色前先跑 scripts/tts_check.py 自检；声音复刻见 scripts/tts_enroll.py。
    tts_enabled: bool = True
    tts_model: str = "cosyvoice-v2"
    tts_voice: str = "longxiaochun_v2"
    # 单连接同时进行的合成数上限。DashScope 侧对并发有配额，
    # 而且合成是"一句话一个请求"，并发过高会被限流。
    tts_max_concurrent: int = 2

    # --- 管理员 ---
    admin_username: str = "admin"
    admin_password: str = "admin123456"

    # --- JWT ---
    jwt_secret_key: str = "personal-agent-default-jwt-secret-change-in-production-env"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 480

    # --- 路径 ---
    chroma_persist_dir: str = "./assets/chroma_db"
    upload_dir: str = "./assets/upload_docs"
    test_data_dir: str = "./assets/test_data"
    log_dir: str = "./logs"
    log_level: str = "INFO"
    # 数据库文件。原先是硬编码在 core/database.py 里的常量，
    # 导致测试无法隔离、只能操作真实库（跑一次测试就污染一次）。
    # 改为配置项后，测试可以指向临时目录。
    db_path: str = "./assets/personal_agent.db"

    # --- 服务 ---
    host: str = "0.0.0.0"
    port: int = 7860

    # --- 安全 ---
    cors_origins: str = "*"  # 生产环境改为具体域名，如 "https://your-domain.com"
    # 每 IP 每分钟最大请求数。
    # 原先 60 —— 实测一次正常使用（问一个问题 + 页面轮询任务进度）
    # 单分钟就到 58 次，余量只剩 2 次，随时会 429。
    # 原因是进度查询是**轮询**接口：上传/重建/评测的进度每 1.5 秒查一次
    # 就是 40 次/分钟，再叠加正常浏览必然超。
    # 提到 240：仍能拦住脚本化的滥用（人手动操作不可能到 240/分钟），
    # 但给轮询留出了余量。
    # 更讲究的做法是给轮询接口单独豁免、给昂贵接口（对话）单独收紧，
    # 那需要按路由配置限流策略。
    rate_limit_per_minute: int = 240
    password_min_length: int = 8  # 密码最小长度
    max_login_attempts: int = 5  # 最大登录失败次数
    login_lockout_minutes: int = 30  # 登录锁定时间（分钟）
    trust_proxy_headers: bool = (
        True  # 是否信任 X-Forwarded-For / X-Real-IP（部署在反向代理后时为 True）
    )
    # 允许以默认密钥/口令启动。仅供本地开发与 CI 使用，生产必须为 False
    allow_insecure_defaults: bool = False

    # --- 上传限制 ---
    max_upload_size_mb: int = 50  # 单文件大小上限（须小于 nginx client_max_body_size）

    # --- 访问控制 ---
    # 是否允许公开注册。
    #
    # ⚠️ 打开后**注册者能读到本知识库的全部内容**（面试官注册账号来问分身问题，
    # 这正是产品本意）。所以公网部署 + 开放注册 = 你的简历、手机号、邮箱等
    # 个人信息对任何愿意注册的人开放。演示时打开，**公网必须关掉或加邀请码**。
    #
    # 这条注释原先写的是"注册后虽拿不到 owner_id=1 的知识库"——那是错的：
    # 检索层当时把 owner_id 写死成 1，注册用户和匿名访客都能读到。
    allow_registration: bool = False

    # 知识库的所有者。本系统是"单管理员的个人数字分身"，知识库属于管理员，
    # 所有**已登录**用户共享这一份。
    shared_kb_owner_id: int = 1

    # 匿名（未登录）用户能否检索知识库。默认**禁止**。
    #
    # 为什么要有这个开关：`POST /api/chat/stream/public` 的文档写着
    # "未登录用户无知识库访问权限"，但它传下去的 owner_id=0 被检索层无视了
    # （那里写死查 owner_id=1），结果匿名访客能直接问出手机号、邮箱。
    # 现在访问规则收在 core/kb_access.py 里，匿名默认拿不到知识库。
    # 只有在"这个分身本就该公开可问"的场景下才该打开。
    allow_anonymous_kb_access: bool = False

    # --- LLM 成本费率（每 100 万 token 的美元价）---
    # JSON 字符串，覆盖 core/token_tracker.py 的内置费率表。留空则只用内置表。
    #
    # 为什么做成配置而不是写死在代码里：本项目走的是第三方 DeepSeek 代理，
    # 各家定价不同，写死必然与实际账单不符。而原实现的内置表里**根本没有
    # 实际使用的 `deepseek-flash`**，导致每次调用都命中"未知模型"、成本记 0 ——
    # 界面上显示的费用全部来自更早的历史记录。
    #
    # 格式（只需填你实际在用的模型）：
    #   LLM_COST_RATES={"deepseek-flash": {"input": 0.28, "output": 1.10}}
    # 可选字段 cached_input：命中 prompt 缓存的输入单价（不填则这部分按 0 计，
    # 宁可低估也不假装知道缓存价）。
    #
    # 查不到费率的模型**不会套用默认价**，而是记 0 + 告警，并在统计接口里
    # 以 `unmapped_models` 暴露出来，界面据此提示"成本未知"。
    llm_cost_rates: str = ""

    # --- 异步任务队列 ---
    # 进程内线程池（见 core/async_queue.py 顶部的取舍说明）。
    # 这里曾经有 REDIS_URL / USE_RQ_QUEUE 两个开关和一整套 RQ 实现，已删除：
    # 单容器单进程的部署里 RQ 的收益为零，而"没有 worker 消费"是个静默故障
    # （任务入队后卡在 pending，前端永远轮询）—— 能力探测验不出来这一点。
    worker_threads: int = 4  # 线程池并发数（文档解析 + 向量化任务）

    # --- 文档处理 ---
    chunk_size: int = 1000
    # 块间重叠字符数。**仅对"定长兜底"路径生效** —— 即
    # ``rag/text_splitter.py`` 里 ``SemanticTextSplitter`` 在"无结构信息"或
    # "大块二次切分"时走的 ``create_text_splitter``，以及 ``use_semantic_splitter``
    # 为 False 时的纯定长分块。
    #
    # 默认走的语义分块（Markdown 标题 / 中文编号章节 / 段落）**刻意不做重叠**：
    # 它按结构边界切块，边界本身就保证一个语义单元不被切断，硬加"上一块的尾部"
    # 反而会把标题和正文混进相邻块、污染检索。所以"按结构切"和"加重叠"是两种
    # 互斥的思路，不能同时要。原实现声称 overlap=200 但在默认（语义）路径下
    # 根本没有重叠，注释与实现不一致；现按"B 方案"明确：overlap 只属于定长兜底。
    chunk_overlap: int = 200
    # 是否用结构感知分块（Markdown 标题 / 中文编号章节 / 段落 → 定长兜底）。
    # 简历、项目文档这类有层级的材料，按结构切块能保住"章节语义"，
    # 检索命中率明显好于纯定长切分。原实现里这个分块器从未被启用过。
    use_semantic_splitter: bool = True
    # 检索是否启用 BM25+向量混合检索（RRF 融合）与结果缓存。
    # 原实现两者都写好了，但默认关闭且没有任何调用点传过参数 —— 等于死代码。
    use_hybrid_search: bool = True
    use_search_cache: bool = True

    # --- 检索相关性阈值 ---
    # rerank 之后相关性分数（0~1）低于此值的结果会被丢弃，不再拼进 prompt。
    #
    # 为什么需要它：没有阈值时，只要向量库 / BM25 返回任何一条（哪怕相关度
    # 只有 0.01），就会被塞进上下文让 LLM 硬答 —— 用户问一个知识库里根本
    # 没有的问题时，会召回"最接近的垃圾块"然后编造。这是 RAG 最容易被击穿的点。
    #
    # ⚠️ **在这个项目的语料上，这个阈值实测无效 —— 但它留着仍有意义。**
    #
    # 实测（2026-09，92 题检索评测集 `assets/test_data/retrieval_testset.json`，
    # 用 `python scripts/eval_retrieval.py` 复现）：
    #
    #     有答案题(82) 的 top1 分数：0.174 ~ 0.9+（最低的 10 条在 0.17~0.19）
    #     无答案题(10) 的 top1 分数：0.251 ~ 0.652
    #
    # **两组完全重叠，没有任何单一阈值能把它们分开。** 阈值扫描结果：
    # 调到能拒掉无答案题（≥0.65）会误杀 **55/82** 道本该答对的题（67%）——
    # 代价远大于收益。
    #
    # 为什么分不开：这个知识库是**同一个人的简历与项目文档**，语义上高度相似。
    # 问"他有没有发表论文"虽然答不出，但检索能找到"学术相关"的段落，分数自然不低。
    # 这是**语料特性**，不是阈值调不好。
    #
    # 所以：**"知识库外的问题如何拒答"实际靠提示词层的诚实约束**（见 agent/prompts.py），
    # 不靠这里。别以为加大了阈值就能解决 —— 那是没测过。
    #
    # 那为什么还留着 0.05？它仍能挡掉**真正无关**的召回（分数在 0.0~0.05 的
    # 噪声片段，例如用户上传的无关文件）。它挡不住"语义相近但答案不存在"的情况。
    #
    # 换语料（比如换成产品手册、法规文档这类彼此区分度高的材料）时**必须重测** ——
    # 那种语料下阈值很可能真正有效。重测方法见 scripts/eval_retrieval.py。
    #
    # 设为 0 或负数等于关闭过滤。
    retrieval_min_score: float = 0.05
    # 注意：rerank 降级（分数未知为 None）时此阈值**不生效**，取舍见
    # ``rag/retriever.py`` 的 ``_retrieve_impl``。
    retrieval_min_score: float = 0.05

    model_config = {
        "env_file": os.path.join(os.path.dirname(__file__), ".env"),
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
        "extra": "ignore",  # 忽略 .env 中未定义的字段，避免多余环境变量导致启动失败
    }

    def resolve_path(self, relative_path: str) -> Path:
        """将相对路径转为基于项目根目录的绝对路径"""
        p = Path(relative_path)
        if p.is_absolute():
            return p
        return (PROJECT_ROOT / p).resolve()


# 全局单例
settings = Settings()

# 确保存储目录存在
for dir_attr in ["upload_dir", "chroma_persist_dir", "test_data_dir", "log_dir"]:
    dir_path = settings.resolve_path(getattr(settings, dir_attr))
    dir_path.mkdir(parents=True, exist_ok=True)

# 配置 Loguru 日志
log_path = settings.resolve_path(settings.log_dir)
logger.add(
    log_path / "app_{time:YYYY-MM-DD}.log",
    level=settings.log_level,
    rotation="10 MB",
    retention="30 days",
    encoding="utf-8",
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}",
)


# ========================================
# 启动期安全校验
# ========================================

# 代码仓库里的默认值 —— 一旦原样用于生产，等于把密钥公开在 GitHub 上
_INSECURE_DEFAULTS = {
    "jwt_secret_key": "personal-agent-default-jwt-secret-change-in-production-env",
    "admin_password": "admin123456",
}


def validate_security_settings() -> list[str]:
    """
    检查是否仍在使用仓库内置的默认密钥/口令。

    Returns:
        仍在使用的字段名列表（空列表表示全部已改）。

    Raises:
        RuntimeError: 使用了默认值且未显式允许（``ALLOW_INSECURE_DEFAULTS=true``）。
    """
    insecure = [k for k, v in _INSECURE_DEFAULTS.items() if getattr(settings, k) == v]
    if not insecure:
        return []

    if settings.allow_insecure_defaults:
        logger.warning(
            "⚠️ 正在使用默认的 {} —— 仅允许用于本地开发/CI，请勿部署到公网",
            "、".join(insecure),
        )
        return insecure

    raise RuntimeError(
        "检测到未修改的默认安全配置: "
        + "、".join(insecure)
        + "\n这些值来自代码仓库，任何人可见，等同于没有保护。"
        "\n请在 config/.env 中改为随机值（例如 JWT_SECRET_KEY 用 "
        '`python -c "import secrets;print(secrets.token_urlsafe(48))"` 生成）。'
        "\n本地开发/CI 可设置 ALLOW_INSECURE_DEFAULTS=true 跳过此检查。"
    )


validate_security_settings()


# ========================================
# 模型客户端 → 见 config/providers.py
# ========================================
#
# 这里原来放着四个工厂（get_deepseek_llm / close_llm_clients /
# get_dashscope_embeddings / rerank_with_dashscope），已经搬走：
#
#   能力定义  config/ports.py      —— Protocol，说明"需要什么"
#   具体实现  config/providers.py  —— DeepSeek / DashScope
#   装配      config/context.py    —— 唯一的取用点 get_context()
#
# 搬走的理由：本模块原本同时管三件事（配置、模块级副作用、模型客户端工厂），
# 423 行；而且调用方直接依赖**具体工厂**，导致测试替换实现有三种不同打法、
# 换 provider 要改所有调用点、一个进程跑不了两套配置。
