"""
벡터 RAG 도구 — chromadb + OpenAI 임베딩 기반 텍스트 유사도 검색.

대상: signals 테이블의 dart_summary + news_summary + triggered_conditions
용도: "비슷한 공시/뉴스 상황에서 AI가 어떤 판단을 했고 결과가 어땠는지" 검색

의존성: chromadb (pip install chromadb)
"""

from __future__ import annotations
import json
import logging
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '..', '..', '.env'))

logger = logging.getLogger(__name__)

_OPENAI_KEY = os.getenv("OPENAI_API_KEY", "").strip()
_CHROMA_DIR = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'data', 'chroma_db')

EMBED_MODEL = "text-embedding-3-small"
COLLECTION_NAME = "signal_contexts"


def _get_collection():
    """chromadb 컬렉션 반환. 없으면 생성."""
    try:
        import chromadb
        from chromadb.utils.embedding_functions import OpenAIEmbeddingFunction
    except ImportError:
        raise RuntimeError("chromadb 미설치. pip install chromadb 실행 후 재시도하세요.")

    Path(_CHROMA_DIR).mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=_CHROMA_DIR)

    embed_fn = OpenAIEmbeddingFunction(
        api_key=_OPENAI_KEY,
        model_name=EMBED_MODEL,
    )
    return client.get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=embed_fn,
        metadata={"hnsw:space": "cosine"},
    )


def index_signal(
    signal_id: int,
    stock_name: str,
    signal_type: str,
    verdict: str | None,
    result_3d: float | None,
    triggered_conditions: str,
    dart_summary: str | None = None,
    news_summary: str | None = None,
) -> bool:
    """신호 1건을 벡터DB에 인덱싱. 이미 존재하면 업데이트."""
    if not _OPENAI_KEY:
        return False

    # 임베딩할 텍스트: 조건 + 공시 + 뉴스
    text_parts = [f"[신호조건] {triggered_conditions}"]
    if dart_summary:
        text_parts.append(f"[공시] {dart_summary[:500]}")
    if news_summary:
        text_parts.append(f"[뉴스] {news_summary[:300]}")
    document = "\n".join(text_parts)

    metadata = {
        "signal_id": signal_id,
        "stock_name": stock_name,
        "signal_type": signal_type or "",
        "verdict": verdict or "",
        "result_3d": result_3d if result_3d is not None else 0.0,
    }

    try:
        col = _get_collection()
        col.upsert(
            ids=[str(signal_id)],
            documents=[document],
            metadatas=[metadata],
        )
        return True
    except Exception as e:
        logger.warning(f"[RAG] 인덱싱 실패 signal_id={signal_id}: {e}")
        return False


def search_similar_context(query: str, n_results: int = 5) -> list[dict]:
    """쿼리 텍스트와 유사한 과거 신호 컨텍스트 검색."""
    try:
        col = _get_collection()
        results = col.query(query_texts=[query], n_results=n_results)
        output = []
        for i, doc in enumerate(results["documents"][0]):
            meta = results["metadatas"][0][i]
            dist = results["distances"][0][i] if results.get("distances") else None
            similarity = round(1 - dist, 3) if dist is not None else None
            output.append({
                "signal_id": meta.get("signal_id"),
                "stock_name": meta.get("stock_name"),
                "signal_type": meta.get("signal_type"),
                "verdict": meta.get("verdict"),
                "result_3d": meta.get("result_3d"),
                "similarity": similarity,
                "context_preview": doc[:200],
            })
        return output
    except Exception as e:
        logger.warning(f"[RAG] 검색 실패: {e}")
        return []


def bulk_index_existing_signals(days: int = 180) -> int:
    """기존 signals 테이블 데이터를 일괄 인덱싱. 반환: 인덱싱된 건수."""
    from data.db import get_conn
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    KST = ZoneInfo("Asia/Seoul")
    since = (datetime.now(tz=KST) - timedelta(days=days)).strftime("%Y-%m-%d")

    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, stock_name, signal_type, verdict, result_pct,
                      triggered_conditions, dart_summary, news_summary
               FROM signals
               WHERE created_at >= ? AND verdict IS NOT NULL""",
            (since,),
        ).fetchall()

    count = 0
    for r in rows:
        ok = index_signal(
            signal_id=r["id"],
            stock_name=r["stock_name"] or "",
            signal_type=r["signal_type"] or "",
            verdict=r["verdict"],
            result_3d=r["result_pct"],
            triggered_conditions=r["triggered_conditions"] or "",
            dart_summary=r["dart_summary"],
            news_summary=r["news_summary"],
        )
        if ok:
            count += 1
    logger.info(f"[RAG] 일괄 인덱싱 완료: {count}/{len(rows)}건")
    return count


# ── Agent 도구 ─────────────────────────────────────────────────────────────

from worker.agents.tools.registry import BaseTool


class SearchTextContextTool(BaseTool):
    """벡터 유사도 기반 과거 신호 컨텍스트 검색 (공시/뉴스 텍스트 기반)."""

    name = "search_text_context"
    label = "공시·뉴스 유사 검색"
    description = (
        "현재 종목의 공시·뉴스 키워드와 유사했던 과거 신호를 벡터 검색으로 찾습니다. "
        "'수주 발표', '영업이익 서프라이즈' 등 텍스트 기반 상황이 비슷했을 때 "
        "AI가 어떤 판단을 했고 결과가 어땠는지 참고할 수 있습니다."
    )
    input_schema = {
        "properties": {
            "query": {
                "type": "string",
                "description": "검색 쿼리 — 현재 공시/뉴스 핵심 내용이나 신호 조건 요약",
            },
            "n_results": {
                "type": "integer",
                "description": "최대 결과 수",
                "default": 5,
            },
        },
        "required": ["query"],
    }

    def execute(self, query: str, n_results: int = 5) -> dict:
        if not _OPENAI_KEY:
            return {"error": "OPENAI_API_KEY 미설정"}
        try:
            results = search_similar_context(query, n_results=n_results)
            return {"count": len(results), "results": results}
        except Exception as e:
            return {"error": str(e)}


class RagIndexSignalTool(BaseTool):
    """신호 저장 후 벡터DB에 인덱싱 (워커 자동 호출용)."""

    name = "rag_index_signal"
    label = "RAG 인덱싱"
    description = "신호를 벡터DB에 인덱싱합니다. 신호 저장 직후 호출하세요."
    input_schema = {
        "properties": {
            "signal_id": {"type": "integer"},
            "stock_name": {"type": "string"},
            "signal_type": {"type": "string", "default": ""},
            "verdict": {"type": "string", "default": ""},
            "result_3d": {"type": "number"},
            "triggered_conditions": {"type": "string"},
            "dart_summary": {"type": "string", "default": ""},
            "news_summary": {"type": "string", "default": ""},
        },
        "required": ["signal_id", "stock_name", "triggered_conditions"],
    }

    def execute(self, signal_id: int, stock_name: str, triggered_conditions: str,
                signal_type: str = "", verdict: str = "",
                result_3d: float | None = None,
                dart_summary: str = "", news_summary: str = "") -> dict:
        ok = index_signal(
            signal_id=signal_id,
            stock_name=stock_name,
            signal_type=signal_type,
            verdict=verdict or None,
            result_3d=result_3d,
            triggered_conditions=triggered_conditions,
            dart_summary=dart_summary or None,
            news_summary=news_summary or None,
        )
        return {"ok": ok, "signal_id": signal_id}
