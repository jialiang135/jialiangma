"""
ChromaDB 向量数据库封装
支持 owner_id 元数据过滤，实现多用户数据隔离
"""

# 启用延迟注解：注解不再在导入时求值。
# 原因：chromadb.PersistentClient 是**函数**（工厂）而不是类，写成
# `chromadb.PersistentClient | None` 会在导入时抛
# TypeError: unsupported operand type(s) for |: 'function' and 'NoneType'。
# （`Optional[X]` 不会，因为它只是把 X 包一层、不当类型用。）
from __future__ import annotations

import os

import chromadb
from chromadb.config import Settings as ChromaSettings
from langchain_chroma import Chroma
from loguru import logger

from config.context import get_context
from config.settings import settings

COLLECTION_NAME = "knowledge_base"

# 全局单例
_chroma_client: chromadb.PersistentClient | None = None
_vector_store: Chroma | None = None


class VectorStoreError(RuntimeError):
    """
    向量库检索/统计链路故障（Chroma 挂了 / embedding 挂了 / filter 写错等）。

    为什么用异常而不是返回 ``[]``：空列表与"知识库里本来就没有相关内容"
    **完全无法区分**，上层会把故障当成"没找到"，自信地答"我的知识库中没有
    这方面的信息" —— 用户得到一个错误结论。抛异常能让 ``retrieve()`` 把故障
    如实透出给调用方（见 ``rag/retriever.py`` 的 ``_retrieve_impl``）。
    """


def _get_chroma_client() -> chromadb.PersistentClient:
    """获取 ChromaDB 持久化客户端（懒加载）"""
    global _chroma_client
    if _chroma_client is None:
        persist_dir = str(settings.resolve_path(settings.chroma_persist_dir))
        os.makedirs(persist_dir, exist_ok=True)
        _chroma_client = chromadb.PersistentClient(
            path=persist_dir,
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        logger.info("ChromaDB 客户端初始化: {}", persist_dir)
    return _chroma_client


def get_vector_store() -> Chroma:
    """获取 Chroma LangChain 向量存储（懒加载）"""
    global _vector_store
    if _vector_store is None:
        client = _get_chroma_client()
        embeddings = get_context().embed.embeddings()
        _vector_store = Chroma(
            client=client,
            collection_name=COLLECTION_NAME,
            embedding_function=embeddings,
        )
        logger.info("ChromaDB 向量存储就绪, collection={}", COLLECTION_NAME)
    return _vector_store


def reset_vector_store():
    """重置向量存储（用于重建知识库）"""
    global _vector_store
    _vector_store = None


# ========================================
# 缓存失效
# ========================================


def _invalidate_retrieval_caches(owner_id: int) -> None:
    """
    文档变更后清掉检索侧缓存。

    **两处缓存必须一起清**，否则检索会读到旧数据：

    - BM25 索引：``rag/bm25_search.py`` 按 owner_id 惰性缓存整个语料
    - 检索结果缓存：``rag/search_cache.py`` 的 TTL 缓存

    之所以挂在向量库的写操作里（而不是各调用点），是为了让上传、删除、
    清空、重建**所有路径**都自动失效 —— 漏一处就会出现"删了文件还检索得到"。
    """
    try:
        from rag.bm25_search import invalidate_bm25_cache

        invalidate_bm25_cache(owner_id)
    except Exception as e:
        logger.warning("BM25 缓存失效失败 (owner_id={}): {}", owner_id, e)

    try:
        from rag.search_cache import clear_cache

        clear_cache()
    except Exception as e:
        logger.warning("检索结果缓存清理失败: {}", e)


# ========================================
# 向量库 CRUD
# ========================================


def add_documents(
    chunks: list[str],
    metadatas: list[dict],
    ids: list[str] | None = None,
    batch_size: int = 10,
) -> list[str]:
    """
    将文本块分批添加到向量库（DashScope Embedding 限制每批最多 10 条）。
    metadatas 必须包含 owner_id 和 source 字段。
    返回添加的文档 ID 列表。
    """
    if not chunks:
        return []

    vector_store = get_vector_store()

    if ids is None:
        import hashlib

        ids = [hashlib.md5(chunk.encode("utf-8")).hexdigest()[:16] for chunk in chunks]

    all_ids = []
    total = len(chunks)

    for i in range(0, total, batch_size):
        batch_chunks = chunks[i : i + batch_size]
        batch_metadatas = metadatas[i : i + batch_size]
        batch_ids = ids[i : i + batch_size]

        try:
            added = vector_store.add_texts(
                texts=batch_chunks,
                metadatas=batch_metadatas,
                ids=batch_ids,
            )
            all_ids.extend(added)
            logger.info(f"向量库入库: {i + len(batch_chunks)}/{total}")
        except Exception as e:
            logger.error(f"向量库入库失败 (batch {i // batch_size}): {e}")
            raise

    logger.info(f"向量库入库完成: {len(all_ids)} 条")

    # 文档变了 → 检索侧缓存必须失效，否则新增内容检索不到
    owner_ids = {m.get("owner_id") for m in metadatas if m.get("owner_id") is not None}
    for oid in owner_ids:
        _invalidate_retrieval_caches(oid)

    return all_ids


def search_by_owner(
    query: str,
    owner_id: int,
    top_k: int = 10,
) -> list[dict]:
    """
    按 owner_id 过滤检索。
    只返回当前用户的文档，实现数据隔离。
    返回: [{"content": str, "metadata": dict, "score": float}, ...]

    Raises:
        VectorStoreError: 检索链路故障（Chroma 客户端初始化 / 向量检索 / filter
            出错）。注意"检索成功但该用户没有文档"返回的是**空列表、不抛异常**
            —— 两者语义不同，上层必须能区分。
    """
    try:
        vector_store = get_vector_store()
        results = vector_store.similarity_search_with_score(
            query,
            k=top_k,
            filter={"owner_id": owner_id},
        )
        return [
            {
                "content": doc.page_content,
                "metadata": doc.metadata,
                "score": score,
            }
            for doc, score in results
        ]
    except Exception as e:
        logger.error(f"向量检索失败: {e}")
        raise VectorStoreError(f"向量检索失败: {e}") from e


def delete_by_file(filename: str, owner_id: int) -> int:
    """
    删除某个文件的所有向量块（需 owner_id 校验）。
    返回删除数量。
    """
    collection = _get_or_create_collection()

    count = 0
    try:
        # 查找匹配的文档ID
        existing = collection.get(
            where={
                "$and": [
                    {"owner_id": owner_id},
                    {"source": filename},
                ]
            }
        )
        if existing["ids"]:
            collection.delete(ids=existing["ids"])
            count = len(existing["ids"])
            logger.info(f"从向量库删除文件: {filename}, {count} 条")
    except Exception as e:
        logger.warning(f"向量库删除失败: {e}")

    # 无论是否删到东西都失效缓存：宁可多清一次，也不要留下旧结果
    _invalidate_retrieval_caches(owner_id)
    return count


def delete_all_by_owner(owner_id: int) -> int:
    """
    清空某个用户的所有向量数据。
    返回删除数量。
    """
    collection = _get_or_create_collection()

    count = 0
    try:
        existing = collection.get(where={"owner_id": owner_id})
        if existing["ids"]:
            collection.delete(ids=existing["ids"])
            count = len(existing["ids"])
            logger.info(f"清空用户向量库: owner_id={owner_id}, {count} 条")
    except Exception as e:
        logger.warning(f"向量库清空失败: {e}")

    _invalidate_retrieval_caches(owner_id)
    return count


def _get_or_create_collection():
    """获取或创建 ChromaDB collection（不存在时自动创建，避免启动报错）"""
    client = _get_chroma_client()
    return client.get_or_create_collection(COLLECTION_NAME)


def get_collection_stats(owner_id: int) -> dict:
    """
    获取某用户的向量库统计信息。

    返回值在原有键（``total_chunks`` / ``unique_files`` / ``files``）之外
    **新增 ``error`` 键**（向后兼容，老调用方读旧键不受影响）：

    - 成功：``error`` 为 ``None``（哪怕该用户一条数据都没有，也是成功的空）。
    - 失败：``error`` 为错误描述字符串。

    原实现异常时直接返回 ``{"total_chunks": 0, ...}``，把"查询失败"伪装成
    "知识库为空" —— 上层会看到"向量块总数: 0"而以为知识库真的空。加了 ``error``
    之后调用方能区分这两种情况（本次不改各调用点，只把能力暴露出来）。
    """
    try:
        collection = _get_or_create_collection()
        existing = collection.get(
            where={"owner_id": owner_id},
            include=["metadatas"],
        )

        total_chunks = len(existing["ids"])
        # 统计唯一文件数
        sources = set()
        for meta in existing.get("metadatas", []):
            if meta and "source" in meta:
                sources.add(meta["source"])

        return {
            "total_chunks": total_chunks,
            "unique_files": len(sources),
            "files": sorted(sources),
            "error": None,
        }
    except Exception as e:
        logger.warning(f"获取向量库统计失败: {e}")
        return {
            "total_chunks": 0,
            "unique_files": 0,
            "files": [],
            "error": f"获取向量库统计失败: {e}",
        }


def list_chunks(
    source: str,
    owner_id: int,
    limit: int = 200,
    offset: int = 0,
) -> dict:
    """
    列出某个文件在向量库里的**全部切片**（按 ``chunk_idx`` 升序）。

    这是"知识库页能看见切片内容"的数据来源。切片正文本来就**完整**存在
    Chroma 里 —— 只有 SSE 的证据轨为了控制单帧大小把它截断到 800 字
    （见 ``api/sse_stream.py`` 的 ``MAX_EVIDENCE_CONTENT``）。所以这个功能
    不需要重新解析文件，也不会因为截断而失真。

    Args:
        source:   文件名（向量库元数据里的 ``source``）。
        owner_id: 知识库归属，由 ``core/kb_access.py`` 裁决后传入。
        limit:    本次返回的切片数上限（避免超大文件一次吐几十万字符）。
        offset:   起始偏移，供前端翻页。

    Returns:
        ``{"total": int, "chunks": [{"chunk_idx", "page", "content", "chars"}, ...]}``

        缺 ``page`` 的切片（非 PDF）该字段为 ``None``。

    Note:
        Chroma 的 ``get()`` 是**同步阻塞**调用，路由层必须用
        ``asyncio.to_thread`` 包起来，否则会卡住事件循环。
    """
    collection = _get_or_create_collection()
    existing = collection.get(
        where={"$and": [{"owner_id": owner_id}, {"source": source}]},
        include=["metadatas", "documents"],
    )

    metadatas = existing.get("metadatas") or []
    documents = existing.get("documents") or []

    rows = []
    # strict=False：只读的展示路径。metadatas/documents 理论上等长，万一 Chroma
    # 返回不一致的长度，宁可少显示几条，也不要整个接口 500。
    for meta, doc in zip(metadatas, documents, strict=False):
        if not meta:
            continue
        idx = meta.get("chunk_idx")
        rows.append((idx if isinstance(idx, int) else 0, meta.get("page"), doc or ""))
    rows.sort(key=lambda r: r[0])

    window = rows[offset : offset + max(1, limit)]
    return {
        "total": len(rows),
        "chunks": [
            {"chunk_idx": idx, "page": page, "content": doc, "chars": len(doc)}
            for idx, page, doc in window
        ],
    }
