"""
ChromaDB 向量数据库封装
支持 owner_id 元数据过滤，实现多用户数据隔离
"""
import os
from typing import Optional
import chromadb
from chromadb.config import Settings as ChromaSettings
from langchain_chroma import Chroma
from loguru import logger

from config.settings import settings, get_dashscope_embeddings


COLLECTION_NAME = "knowledge_base"

# 全局单例
_chroma_client: Optional[chromadb.PersistentClient] = None
_vector_store: Optional[Chroma] = None


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
        embeddings = get_dashscope_embeddings()
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
# 向量库 CRUD
# ========================================

def add_documents(
    chunks: list[str],
    metadatas: list[dict],
    ids: Optional[list[str]] = None,
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
        ids = [
            hashlib.md5(chunk.encode("utf-8")).hexdigest()[:16]
            for chunk in chunks
        ]

    all_ids = []
    total = len(chunks)

    for i in range(0, total, batch_size):
        batch_chunks = chunks[i:i + batch_size]
        batch_metadatas = metadatas[i:i + batch_size]
        batch_ids = ids[i:i + batch_size]

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
    """
    vector_store = get_vector_store()

    try:
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
        return []


def delete_by_file(filename: str, owner_id: int) -> int:
    """
    删除某个文件的所有向量块（需 owner_id 校验）。
    返回删除数量。
    """
    vector_store = get_vector_store()
    client = _get_chroma_client()
    collection = client.get_collection(COLLECTION_NAME)

    try:
        # 查找匹配的文档ID
        existing = collection.get(
            where={"$and": [
                {"owner_id": owner_id},
                {"source": filename},
            ]}
        )
        if existing["ids"]:
            collection.delete(ids=existing["ids"])
            count = len(existing["ids"])
            logger.info(f"从向量库删除文件: {filename}, {count} 条")
            return count
        return 0
    except Exception as e:
        logger.error(f"向量库删除失败: {e}")
        return 0


def delete_all_by_owner(owner_id: int) -> int:
    """
    清空某个用户的所有向量数据。
    返回删除数量。
    """
    client = _get_chroma_client()
    collection = client.get_collection(COLLECTION_NAME)

    try:
        existing = collection.get(
            where={"owner_id": owner_id}
        )
        if existing["ids"]:
            collection.delete(ids=existing["ids"])
            count = len(existing["ids"])
            logger.info(f"清空用户向量库: owner_id={owner_id}, {count} 条")
            return count
        return 0
    except Exception as e:
        logger.error(f"向量库清空失败: {e}")
        return 0


def get_collection_stats(owner_id: int) -> dict:
    """获取某用户的向量库统计信息"""
    try:
        client = _get_chroma_client()
        collection = client.get_collection(COLLECTION_NAME)
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
        }
    except Exception as e:
        logger.error(f"获取向量库统计失败: {e}")
        return {"total_chunks": 0, "unique_files": 0, "files": []}
