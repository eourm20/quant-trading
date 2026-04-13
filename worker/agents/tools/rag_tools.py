"""
벡터 RAG 도구 — FAISS + OpenAI 임베딩 기반 텍스트 유사도 검색.

대상: signals 테이블의 dart_summary + news_summary + triggered_conditions
용도: "비슷한 공시/뉴스 상황에서 AI가 어떤 판단을 했고 결과가 어땠는지" 검색

구조:
  - 벡터: FAISS IndexIDMap(IndexFlatIP) — signal_id를 키로 직접 저장
  - 정규화: L2 정규화 → 코사인 유사도 (InnerProduct = cosine after normalize)
  - 메타데이터: 기존 signals 테이블 재활용 (FAISS가 signal_id 반환 → SQLite 조회)
  - 영속성: data/faiss_index.bin 파일로 저장

의존성: faiss-cpu (pip install faiss-cpu)
"""

from __future__ import annotations
import logging
import os
import threading
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '..', '..', '.env'))

logger = logging.getLogger(__name__)

_OPENAI_KEY = os.getenv("OPENAI_API_KEY", "").strip()
_INDEX_PATH = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'data', 'faiss_index.bin')
_INDEX_LOCK = threading.Lock()

EMBED_MODEL = "text-embedding-3-small"
EMBED_DIM = 1536  # text-embedding-3-small 차원


# ── 임베딩 ────────────────────────────────────────────────────────────────────

def _embed(text: str) -> list[float]:
    """OpenAI 임베딩 생성 (1536차원 float 리스트)."""
    from openai import OpenAI
    client = OpenAI(api_key=_OPENAI_KEY)
    resp = client.embeddings.create(model=EMBED_MODEL, input=text)
    return resp.data[0].embedding


# ── FAISS 인덱스 관리 ─────────────────────────────────────────────────────────

def _load_index():
    """디스크에서 인덱스 로드. 없으면 새로 생성."""
    try:
        import faiss
    except ImportError:
        raise RuntimeError("faiss-cpu 미설치. pip install faiss-cpu 실행 후 재시도하세요.")

    path = Path(_INDEX_PATH)
    if path.exists():
        index = faiss.read_index(str(path))
        logger.debug(f"[RAG] 인덱스 로드 완료: {index.ntotal}건")
        return index

    # 신규 생성: IndexIDMap으로 signal_id를 직접 키로 사용
    flat = faiss.IndexFlatIP(EMBED_DIM)   # InnerProduct (코사인 = L2정규화 후 IP)
    index = faiss.IndexIDMap(flat)
    return index


def _save_index(index) -> None:
    """인덱스를 디스크에 저장."""
    import faiss
    Path(_INDEX_PATH).parent.mkdir(parents=True, exist_ok=True)
    faiss.write_index(index, str(_INDEX_PATH))


# ── 핵심 함수 ─────────────────────────────────────────────────────────────────

def _build_document(
    triggered_conditions: str,
    dart_summary: str | None,
    news_summary: str | None,
    indicator_snapshot: str | dict | None = None,
) -> str:
    """임베딩할 텍스트 조합."""
    import json as _json
    parts = [f"[신호조건] {triggered_conditions}"]
    if indicator_snapshot:
        try:
            snap = _json.loads(indicator_snapshot) if isinstance(indicator_snapshot, str) else indicator_snapshot
            key_vals = []
            for k in ["rsi", "trend", "above_ma20", "ma_cross", "volume_ratio", "bollinger_position", "stochastic_k"]:
                v = snap.get(k)
                if v is not None:
                    key_vals.append(f"{k}:{v}")
            if key_vals:
                parts.append(f"[지표] {' '.join(key_vals)}")
        except Exception:
            pass
    if dart_summary:
        parts.append(f"[공시] {dart_summary[:500]}")
    if news_summary:
        parts.append(f"[뉴스] {news_summary[:300]}")
    return "\n".join(parts)


def index_signal(
    signal_id: int,
    stock_name: str,
    signal_type: str,
    verdict: str | None,
    result_3d: float | None,
    triggered_conditions: str,
    dart_summary: str | None = None,
    news_summary: str | None = None,
    indicator_snapshot: str | dict | None = None,
) -> bool:
    """신호 1건을 FAISS 인덱스에 추가. 이미 존재하면 덮어씀."""
    if not _OPENAI_KEY:
        return False

    document = _build_document(triggered_conditions, dart_summary, news_summary, indicator_snapshot)

    try:
        import faiss
        import numpy as np

        vec = np.array([_embed(document)], dtype=np.float32)
        faiss.normalize_L2(vec)  # 코사인 유사도를 위한 L2 정규화
        ids = np.array([signal_id], dtype=np.int64)

        with _INDEX_LOCK:
            index = _load_index()
            # 기존 항목 제거 후 재추가 (upsert)
            try:
                index.remove_ids(ids)
            except Exception:
                pass
            index.add_with_ids(vec, ids)
            _save_index(index)

        logger.debug(f"[RAG] 인덱싱 완료: signal_id={signal_id} ({stock_name})")
        return True

    except Exception as e:
        logger.warning(f"[RAG] 인덱싱 실패 signal_id={signal_id}: {e}")
        return False


def search_similar_context(query: str, n_results: int = 5) -> list[dict]:
    """쿼리와 유사한 과거 신호 검색.
    반환: [{signal_id, stock_name, verdict, result_3d, similarity, context_preview}]
    """
    if not _OPENAI_KEY:
        return []

    try:
        import faiss
        import numpy as np
        from data.db import get_conn

        with _INDEX_LOCK:
            index = _load_index()

        if index.ntotal == 0:
            return []

        # 쿼리 임베딩 + 정규화
        vec = np.array([_embed(query)], dtype=np.float32)
        faiss.normalize_L2(vec)

        k = min(n_results, index.ntotal)
        scores, ids = index.search(vec, k)  # scores = 코사인 유사도 (0~1)

        # signal_id로 메타데이터 조회 (기존 signals 테이블)
        valid_ids = [int(i) for i in ids[0] if i >= 0]
        if not valid_ids:
            return []

        placeholders = ",".join("?" * len(valid_ids))
        with get_conn() as conn:
            rows = conn.execute(
                f"""SELECT id, stock_name, signal_type, verdict,
                           result_pct, result_1d, result_5d,
                           triggered_conditions, dart_summary, news_summary
                    FROM signals WHERE id IN ({placeholders})""",
                valid_ids,
            ).fetchall()

        # id → row 매핑
        row_map = {r["id"]: dict(r) for r in rows}

        output = []
        for i, sid in enumerate(valid_ids):
            row = row_map.get(sid)
            if not row:
                continue
            similarity = round(float(scores[0][i]), 4)
            doc_preview = _build_document(
                row.get("triggered_conditions") or "",
                row.get("dart_summary"),
                row.get("news_summary"),
            )[:200]
            output.append({
                "signal_id": sid,
                "stock_name": row.get("stock_name"),
                "signal_type": row.get("signal_type"),
                "verdict": row.get("verdict"),
                "result_3d": row.get("result_pct"),
                "result_1d": row.get("result_1d"),
                "result_5d": row.get("result_5d"),
                "similarity": similarity,
                "context_preview": doc_preview,
            })

        return output

    except Exception as e:
        logger.warning(f"[RAG] 검색 실패: {e}")
        return []


def bulk_index_existing_signals(days: int = 180) -> int:
    """기존 signals 데이터 일괄 인덱싱. 반환: 인덱싱된 건수."""
    from data.db import get_conn
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    KST = ZoneInfo("Asia/Seoul")
    since = (datetime.now(tz=KST) - timedelta(days=days)).strftime("%Y-%m-%d")

    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, stock_name, signal_type, verdict, result_pct,
                      triggered_conditions, dart_summary, news_summary,
                      indicator_snapshot
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
            indicator_snapshot=r["indicator_snapshot"],
        )
        if ok:
            count += 1
    logger.info(f"[RAG] 일괄 인덱싱 완료: {count}/{len(rows)}건")
    return count


def get_index_stats() -> dict:
    """인덱스 상태 조회."""
    try:
        import faiss
        with _INDEX_LOCK:
            index = _load_index()
        return {
            "total_vectors": index.ntotal,
            "dimension": EMBED_DIM,
            "index_path": str(_INDEX_PATH),
            "index_exists": Path(_INDEX_PATH).exists(),
        }
    except Exception as e:
        return {"error": str(e)}


# ── Agent 도구 ─────────────────────────────────────────────────────────────────

from worker.agents.tools.registry import BaseTool


class SearchTextContextTool(BaseTool):
    """FAISS 벡터 유사도 기반 과거 신호 컨텍스트 검색 (공시/뉴스 텍스트 기반)."""

    name = "search_text_context"
    label = "공시·뉴스 유사 검색"
    description = (
        "현재 종목의 공시·뉴스 키워드와 유사했던 과거 신호를 벡터 검색으로 찾습니다. "
        "'수주 발표', '영업이익 서프라이즈' 등 텍스트 기반 상황이 비슷했을 때 "
        "AI가 어떤 판단을 했고 결과가 어땠는지 참고할 수 있습니다. "
        "뉴스나 공시의 해석이 애매할 때 쓰는 보조 도구이며, 현재 사실 확인을 대신하지는 않습니다."
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
    """신호 저장 후 FAISS 인덱스에 추가 (워커 자동 호출용)."""

    name = "rag_index_signal"
    label = "RAG 인덱싱"
    description = "신호를 FAISS 벡터 인덱스에 추가합니다. 신호 저장 직후 호출하세요."
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


# ── 스크리닝 로그 RAG ─────────────────────────────────────────────────────────

_SCREENING_INDEX_PATH = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'data', 'faiss_screening_index.bin')


def index_screening_result(
    log_id: int,
    stock_name: str,
    recommendation: str,
    reason: str | None = None,
    dart_summary: str | None = None,
    news_summary: str | None = None,
    indicator_snapshot: str | dict | None = None,
) -> bool:
    """스크리닝 결과 1건을 별도 FAISS 인덱스에 추가."""
    if not _OPENAI_KEY:
        return False

    cond_text = f"추천:{recommendation} {reason or ''}"
    document = _build_document(cond_text, dart_summary, news_summary, indicator_snapshot)

    try:
        import faiss
        import numpy as np

        vec = np.array([_embed(document)], dtype=np.float32)
        faiss.normalize_L2(vec)
        ids = np.array([log_id], dtype=np.int64)

        path = Path(_SCREENING_INDEX_PATH)
        with _INDEX_LOCK:
            if path.exists():
                index = faiss.read_index(str(path))
            else:
                flat = faiss.IndexFlatIP(EMBED_DIM)
                index = faiss.IndexIDMap(flat)
            try:
                index.remove_ids(ids)
            except Exception:
                pass
            index.add_with_ids(vec, ids)
            path.parent.mkdir(parents=True, exist_ok=True)
            faiss.write_index(index, str(path))

        logger.debug(f"[RAG-screening] 인덱싱 완료: log_id={log_id} ({stock_name}) → {recommendation}")
        return True

    except Exception as e:
        logger.warning(f"[RAG-screening] 인덱싱 실패 log_id={log_id}: {e}")
        return False


def search_similar_screening_context(query: str, n_results: int = 5) -> list[dict]:
    """스크리닝 인덱스에서 유사한 과거 스크리닝 결과 검색.
    반환: [{log_id, stock_name, recommendation, similarity, context_preview}]
    """
    if not _OPENAI_KEY:
        return []

    path = Path(_SCREENING_INDEX_PATH)
    if not path.exists():
        return []

    try:
        import faiss
        import numpy as np
        from data.db import get_conn

        index = faiss.read_index(str(path))
        if index.ntotal == 0:
            return []

        vec = np.array([_embed(query)], dtype=np.float32)
        faiss.normalize_L2(vec)

        k = min(n_results, index.ntotal)
        scores, ids = index.search(vec, k)

        valid_ids = [int(i) for i in ids[0] if i >= 0]
        if not valid_ids:
            return []

        placeholders = ",".join("?" * len(valid_ids))
        with get_conn() as conn:
            rows = conn.execute(
                f"""SELECT id, stock_name, recommendation, reason, dart_summary, news_summary
                    FROM screening_log WHERE id IN ({placeholders})""",
                valid_ids,
            ).fetchall()

        row_map = {r["id"]: dict(r) for r in rows}
        output = []
        for i, lid in enumerate(valid_ids):
            row = row_map.get(lid)
            if not row:
                continue
            preview = f"추천:{row.get('recommendation','')} {(row.get('reason') or '')[:100]}"
            output.append({
                "log_id": lid,
                "stock_name": row.get("stock_name"),
                "recommendation": row.get("recommendation"),
                "similarity": round(float(scores[0][i]), 4),
                "context_preview": preview,
            })
        return output

    except Exception as e:
        logger.warning(f"[RAG-screening] 검색 실패: {e}")
        return []


class SearchScreeningContextTool(BaseTool):
    """FAISS 벡터 유사도 기반 과거 스크리닝 결과 검색."""

    name = "search_screening_context"
    label = "스크리닝 이력 검색"
    description = (
        "과거 스크리닝에서 이 종목 또는 유사한 공시/뉴스 상황을 분석한 이력을 검색합니다. "
        "'이 종목 전에 왜 안 담았지?' 또는 '비슷한 공시 상황에서 스크리닝 결과가 어땠는지' 확인할 때 활용하세요. "
        "Research Agent가 후보 제외 사유를 되짚거나 중복 실수를 줄일 때 특히 유용합니다."
    )
    input_schema = {
        "properties": {
            "query": {
                "type": "string",
                "description": "검색 쿼리 — 종목명, 공시 내용, 스크리닝 상황 키워드",
            },
            "n_results": {
                "type": "integer",
                "description": "최대 결과 수",
                "default": 3,
            },
        },
        "required": ["query"],
    }

    def execute(self, query: str, n_results: int = 3) -> dict:
        if not _OPENAI_KEY:
            return {"error": "OPENAI_API_KEY 미설정"}
        try:
            results = search_similar_screening_context(query, n_results=n_results)
            return {"count": len(results), "results": results}
        except Exception as e:
            return {"error": str(e)}
