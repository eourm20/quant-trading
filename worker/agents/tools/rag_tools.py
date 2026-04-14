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
import json
import re
import threading
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '..', '..', '.env'))

logger = logging.getLogger(__name__)

_OPENAI_KEY = os.getenv("OPENAI_API_KEY", "").strip()
_INDEX_PATH = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'data', 'faiss_index.bin')
_INDEX_LOCK = threading.Lock()
_NEWS_INDEX_PATH = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'data', 'faiss_news_index.bin')
_MARKET_REGIME_INDEX_PATH = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'data', 'faiss_market_regime_index.bin')
_POSTMORTEM_INDEX_PATH = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'data', 'faiss_postmortem_index.bin')
_TOOL_TRACE_INDEX_PATH = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'data', 'faiss_tool_trace_index.bin')
_WATCHLIST_DECISION_INDEX_PATH = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'data', 'faiss_watchlist_decision_index.bin')

EMBED_MODEL = "text-embedding-3-small"
EMBED_DIM = 1536  # text-embedding-3-small 차원
KST = datetime.now().astimezone().tzinfo


def _ensure_memory_tables() -> None:
    from data.db import get_conn

    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rag_memory_docs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_key TEXT UNIQUE NOT NULL,
                memory_type TEXT NOT NULL,
                ref_table TEXT DEFAULT NULL,
                ref_id INTEGER DEFAULT NULL,
                created_at TEXT NOT NULL,
                stock_code TEXT DEFAULT NULL,
                stock_name TEXT DEFAULT NULL,
                title TEXT DEFAULT NULL,
                content TEXT NOT NULL,
                extra_json TEXT DEFAULT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rag_memory_type_date ON rag_memory_docs (memory_type, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rag_memory_stock_date ON rag_memory_docs (stock_code, created_at)")
        conn.commit()


def _upsert_memory_doc(
    source_key: str,
    memory_type: str,
    content: str,
    created_at: str | None = None,
    ref_table: str | None = None,
    ref_id: int | None = None,
    stock_code: str | None = None,
    stock_name: str | None = None,
    title: str | None = None,
    extra: dict | None = None,
) -> int:
    from data.db import get_conn, _now_kst

    _ensure_memory_tables()
    created = created_at or _now_kst().strftime("%Y-%m-%d %H:%M:%S")
    extra_json = json.dumps(extra or {}, ensure_ascii=False) if extra else None

    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM rag_memory_docs WHERE source_key = ?",
            (source_key,),
        ).fetchone()
        if row:
            conn.execute(
                """UPDATE rag_memory_docs
                   SET memory_type=?, ref_table=?, ref_id=?, created_at=?, stock_code=?, stock_name=?,
                       title=?, content=?, extra_json=?
                   WHERE source_key=?""",
                (
                    memory_type, ref_table, ref_id, created, stock_code, stock_name,
                    title, content, extra_json, source_key,
                ),
            )
            conn.commit()
            return int(row["id"])

        cur = conn.execute(
            """INSERT INTO rag_memory_docs
               (source_key, memory_type, ref_table, ref_id, created_at, stock_code, stock_name, title, content, extra_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                source_key, memory_type, ref_table, ref_id, created,
                stock_code, stock_name, title, content, extra_json,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def _parse_dt(text: str | None) -> datetime | None:
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except Exception:
            continue
    return None


def _recency_weight(created_at: str | None) -> float:
    dt = _parse_dt(created_at)
    if not dt:
        return 1.0
    now = datetime.now()
    age_days = max(0.0, (now - dt).total_seconds() / 86400.0)
    # 0d=1.15, 7d~1.08, 30d~1.0, 90d~0.9, 180d~0.8
    return max(0.75, 1.15 - min(age_days, 180.0) * 0.002)


def _lexical_score(query: str, text: str) -> float:
    q = set(re.findall(r"[A-Za-z0-9가-힣_]+", (query or "").lower()))
    d = set(re.findall(r"[A-Za-z0-9가-힣_]+", (text or "").lower()))
    if not q or not d:
        return 0.0
    return len(q & d) / max(1.0, len(q))


def _load_or_create_index(path: str):
    import faiss

    p = Path(path)
    if p.exists():
        return faiss.read_index(str(p))
    flat = faiss.IndexFlatIP(EMBED_DIM)
    return faiss.IndexIDMap(flat)


def _save_index_to_path(index, path: str) -> None:
    import faiss

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    faiss.write_index(index, str(p))


def _index_memory_document(index_path: str, doc_id: int, document: str) -> bool:
    try:
        import faiss
        import numpy as np

        vec = np.array([_embed(document)], dtype=np.float32)
        faiss.normalize_L2(vec)
        ids = np.array([doc_id], dtype=np.int64)

        with _INDEX_LOCK:
            index = _load_or_create_index(index_path)
            try:
                index.remove_ids(ids)
            except Exception:
                pass
            index.add_with_ids(vec, ids)
            _save_index_to_path(index, index_path)
        return True
    except Exception as e:
        logger.warning(f"[RAG-memory] index failed path={index_path} doc_id={doc_id}: {e}")
        return False


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
    ai_response: str | None = None,
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


def _extract_rag_json_summary(text: str | None, max_len: int = 600) -> str:
    """Extract compact text from [RAG_CONTEXT_JSON] block if present."""
    if not text:
        return ""
    raw = str(text)
    m = re.search(r"\[RAG_CONTEXT_JSON\]\s*```json\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if not m:
        return re.sub(r"\s+", " ", raw)[:max_len]
    try:
        obj = json.loads(m.group(1))
        if isinstance(obj, dict):
            keys = [
                "stock_name", "stock_code", "recommendation", "reason",
                "met_conditions", "rr_ratio", "current_price",
            ]
            parts = []
            for k in keys:
                if k in obj and obj[k] not in (None, "", []):
                    parts.append(f"{k}:{obj[k]}")
            if not parts:
                parts = [f"{k}:{v}" for k, v in list(obj.items())[:8]]
            return " | ".join(parts)[:max_len]
    except Exception:
        pass
    return re.sub(r"\s+", " ", raw)[:max_len]


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
        # Extra memory indices for agent retrieval.
        if news_summary:
            index_news_memory(
                source_key=f"signals_news:{signal_id}",
                title=f"{stock_name} signal news",
                content=news_summary,
                ref_table="signals",
                ref_id=signal_id,
                stock_name=stock_name,
            )
        trace = f"signal_type={signal_type} verdict={verdict or ''} conditions={triggered_conditions or ''}"
        index_tool_trace_memory(
            source_key=f"signals_trace:{signal_id}",
            tool_trace=trace,
            ref_table="signals",
            ref_id=signal_id,
            stock_name=stock_name,
        )
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
    return bulk_index_existing_signals_in_batches(days=days, batch_size=100)


def bulk_index_signals_by_ids(signal_ids: list[int], batch_size: int = 100) -> int:
    """지정한 signal_id 목록만 배치 인덱싱."""
    from data.db import get_conn
    ids = sorted({int(sid) for sid in (signal_ids or []) if sid})
    if not ids:
        return 0

    batch_size = max(1, int(batch_size or 100))
    count = 0

    with get_conn() as conn:
        for i in range(0, len(ids), batch_size):
            chunk = ids[i:i + batch_size]
            placeholders = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"""SELECT id, stock_name, signal_type, verdict, result_pct,
                           triggered_conditions, dart_summary, news_summary,
                           indicator_snapshot
                    FROM signals
                    WHERE id IN ({placeholders}) AND verdict IS NOT NULL""",
                chunk,
            ).fetchall()
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
    logger.info(f"[RAG] 지정 ID 배치 인덱싱 완료: {count}/{len(ids)}건 (batch_size={batch_size})")
    return count


def bulk_index_existing_signals_in_batches(
    days: int = 180,
    batch_size: int = 100,
    max_rows: int = 0,
) -> int:
    """기존 signals 데이터 배치 인덱싱. max_rows=0이면 제한 없음."""
    from data.db import get_conn
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    KST = ZoneInfo("Asia/Seoul")
    since = (datetime.now(tz=KST) - timedelta(days=days)).strftime("%Y-%m-%d")
    batch_size = max(1, int(batch_size or 100))
    max_rows = max(0, int(max_rows or 0))

    total = 0
    with get_conn() as conn:
        offset = 0
        while True:
            if max_rows:
                remaining = max_rows - offset
                if remaining <= 0:
                    break
                fetch_limit = min(batch_size, remaining)
            else:
                fetch_limit = batch_size

            rows = conn.execute(
                """SELECT id, stock_name, signal_type, verdict, result_pct,
                          triggered_conditions, dart_summary, news_summary,
                          indicator_snapshot
                   FROM signals
                   WHERE created_at >= ? AND verdict IS NOT NULL
                   ORDER BY id ASC
                   LIMIT ? OFFSET ?""",
                (since, fetch_limit, offset),
            ).fetchall()

            if not rows:
                break

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
                    total += 1
            offset += len(rows)
    logger.info(f"[RAG] 배치 일괄 인덱싱 완료: {total}건 (batch_size={batch_size}, max_rows={max_rows or 'all'})")
    return total


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


def index_news_memory(
    source_key: str,
    title: str,
    content: str,
    created_at: str | None = None,
    ref_table: str | None = None,
    ref_id: int | None = None,
    stock_code: str | None = None,
    stock_name: str | None = None,
) -> bool:
    text = re.sub(r"\s+", " ", f"{title or ''}\n{content or ''}").strip()
    if not text:
        return False
    doc_id = _upsert_memory_doc(
        source_key=source_key,
        memory_type="news",
        ref_table=ref_table,
        ref_id=ref_id,
        created_at=created_at,
        stock_code=stock_code,
        stock_name=stock_name,
        title=title,
        content=text[:3000],
    )
    return _index_memory_document(_NEWS_INDEX_PATH, doc_id, text[:3000])


def index_market_regime_memory(
    source_key: str,
    market_snapshot: str,
    created_at: str | None = None,
    ref_table: str | None = None,
    ref_id: int | None = None,
    extra: dict | None = None,
) -> bool:
    text = re.sub(r"\s+", " ", market_snapshot or "").strip()
    if not text:
        return False
    doc_id = _upsert_memory_doc(
        source_key=source_key,
        memory_type="market_regime",
        ref_table=ref_table,
        ref_id=ref_id,
        created_at=created_at,
        title="market regime",
        content=text[:3000],
        extra=extra,
    )
    return _index_memory_document(_MARKET_REGIME_INDEX_PATH, doc_id, text[:3000])


def index_postmortem_memory(
    source_key: str,
    title: str,
    content: str,
    created_at: str | None = None,
    ref_table: str | None = None,
    ref_id: int | None = None,
    extra: dict | None = None,
) -> bool:
    text = re.sub(r"\s+", " ", f"{title or ''}\n{content or ''}").strip()
    if not text:
        return False
    doc_id = _upsert_memory_doc(
        source_key=source_key,
        memory_type="postmortem",
        ref_table=ref_table,
        ref_id=ref_id,
        created_at=created_at,
        title=title,
        content=text[:4000],
        extra=extra,
    )
    return _index_memory_document(_POSTMORTEM_INDEX_PATH, doc_id, text[:4000])


def index_tool_trace_memory(
    source_key: str,
    tool_trace: str,
    created_at: str | None = None,
    ref_table: str | None = None,
    ref_id: int | None = None,
    stock_code: str | None = None,
    stock_name: str | None = None,
    extra: dict | None = None,
) -> bool:
    text = re.sub(r"\s+", " ", tool_trace or "").strip()
    if not text:
        return False
    doc_id = _upsert_memory_doc(
        source_key=source_key,
        memory_type="tool_trace",
        ref_table=ref_table,
        ref_id=ref_id,
        created_at=created_at,
        stock_code=stock_code,
        stock_name=stock_name,
        title="tool trace",
        content=text[:3000],
        extra=extra,
    )
    return _index_memory_document(_TOOL_TRACE_INDEX_PATH, doc_id, text[:3000])


def index_watchlist_decision_memory(
    source_key: str,
    stock_code: str,
    stock_name: str,
    recommendation: str,
    reason: str,
    created_at: str | None = None,
    ref_table: str | None = None,
    ref_id: int | None = None,
    ai_response: str | None = None,
    extra: dict | None = None,
) -> bool:
    ai_ctx = _extract_rag_json_summary(ai_response, max_len=350) if ai_response else ""
    text = f"stock:{stock_name}({stock_code}) recommendation:{recommendation} reason:{reason}"
    if ai_ctx:
        text += f" ai:{ai_ctx}"
    doc_id = _upsert_memory_doc(
        source_key=source_key,
        memory_type="watchlist_decision",
        ref_table=ref_table,
        ref_id=ref_id,
        created_at=created_at,
        stock_code=stock_code,
        stock_name=stock_name,
        title=f"{stock_name} decision",
        content=text[:3500],
        extra=extra,
    )
    return _index_memory_document(_WATCHLIST_DECISION_INDEX_PATH, doc_id, text[:3500])


def _search_memory_index(index_path: str, memory_type: str, query: str, n_results: int = 5) -> list[dict]:
    if not _OPENAI_KEY:
        return []
    p = Path(index_path)
    if not p.exists():
        return []
    try:
        import faiss
        import numpy as np
        from data.db import get_conn

        index = faiss.read_index(str(p))
        if index.ntotal == 0:
            return []

        vec = np.array([_embed(query)], dtype=np.float32)
        faiss.normalize_L2(vec)
        k = min(max(1, int(n_results * 2)), index.ntotal)
        scores, ids = index.search(vec, k)
        valid_ids = [int(i) for i in ids[0] if i >= 0]
        if not valid_ids:
            return []

        placeholders = ",".join("?" * len(valid_ids))
        with get_conn() as conn:
            rows = conn.execute(
                f"""SELECT id, created_at, stock_code, stock_name, title, content, extra_json
                    FROM rag_memory_docs
                    WHERE memory_type = ? AND id IN ({placeholders})""",
                [memory_type, *valid_ids],
            ).fetchall()

        row_map = {int(r["id"]): dict(r) for r in rows}
        out = []
        for i, rid in enumerate(valid_ids):
            row = row_map.get(rid)
            if not row:
                continue
            vec_score = float(scores[0][i])
            lex = _lexical_score(query, f"{row.get('title') or ''} {row.get('content') or ''}")
            recency = _recency_weight(row.get("created_at"))
            hybrid = (0.75 * vec_score + 0.25 * lex) * recency
            out.append(
                {
                    "memory_id": rid,
                    "memory_type": memory_type,
                    "created_at": row.get("created_at"),
                    "stock_code": row.get("stock_code"),
                    "stock_name": row.get("stock_name"),
                    "title": row.get("title"),
                    "similarity": round(vec_score, 4),
                    "hybrid_score": round(hybrid, 4),
                    "context_preview": re.sub(r"\s+", " ", row.get("content") or "")[:220],
                }
            )
        out.sort(key=lambda x: x["hybrid_score"], reverse=True)
        return out[:n_results]
    except Exception as e:
        logger.warning(f"[RAG-memory] search failed type={memory_type}: {e}")
        return []


def search_news_context(query: str, n_results: int = 5) -> list[dict]:
    return _search_memory_index(_NEWS_INDEX_PATH, "news", query, n_results=n_results)


def search_market_regime_context(query: str, n_results: int = 5) -> list[dict]:
    return _search_memory_index(_MARKET_REGIME_INDEX_PATH, "market_regime", query, n_results=n_results)


def search_postmortem_context(query: str, n_results: int = 5) -> list[dict]:
    return _search_memory_index(_POSTMORTEM_INDEX_PATH, "postmortem", query, n_results=n_results)


def search_tool_trace_context(query: str, n_results: int = 5) -> list[dict]:
    return _search_memory_index(_TOOL_TRACE_INDEX_PATH, "tool_trace", query, n_results=n_results)


def search_watchlist_decision_context(query: str, n_results: int = 5) -> list[dict]:
    return _search_memory_index(_WATCHLIST_DECISION_INDEX_PATH, "watchlist_decision", query, n_results=n_results)


def search_agent_memory_context(query: str, n_results: int = 6) -> list[dict]:
    buckets = [
        *search_news_context(query, n_results=max(2, n_results // 2)),
        *search_market_regime_context(query, n_results=max(2, n_results // 2)),
        *search_postmortem_context(query, n_results=max(2, n_results // 2)),
        *search_tool_trace_context(query, n_results=max(2, n_results // 2)),
        *search_watchlist_decision_context(query, n_results=max(2, n_results // 2)),
    ]
    buckets.sort(key=lambda x: x.get("hybrid_score", 0.0), reverse=True)
    return buckets[:n_results]


def bulk_index_agent_memory(days: int = 180) -> dict:
    from data.db import get_conn
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    kst = ZoneInfo("Asia/Seoul")
    since = (datetime.now(tz=kst) - timedelta(days=days)).strftime("%Y-%m-%d")
    counts = {
        "news": 0,
        "market_regime": 0,
        "postmortem": 0,
        "tool_trace": 0,
        "watchlist_decision": 0,
    }

    with get_conn() as conn:
        s_rows = conn.execute(
            """SELECT id, created_at, stock_code, stock_name, news_summary, tool_sequence, reasoning_chain
               FROM signals
               WHERE created_at >= ?""",
            (since,),
        ).fetchall()
        sc_rows = conn.execute(
            """SELECT id, created_at, stock_code, stock_name, recommendation, reason, ai_response, news_summary, market_snapshot
               FROM screening_log
               WHERE created_at >= ?""",
            (since,),
        ).fetchall()
        note_rows = conn.execute(
            """SELECT id, created_at, category, summary, detail
               FROM strategy_notes
               WHERE created_at >= ?""",
            (since,),
        ).fetchall()

    for r in s_rows:
        sid = int(r["id"])
        if r["news_summary"]:
            if index_news_memory(
                source_key=f"signals_news:{sid}",
                title=f"{r['stock_name'] or ''} news",
                content=r["news_summary"],
                created_at=r["created_at"],
                ref_table="signals",
                ref_id=sid,
                stock_code=r["stock_code"],
                stock_name=r["stock_name"],
            ):
                counts["news"] += 1
        trace = " -> ".join([x for x in [r["tool_sequence"], r["reasoning_chain"]] if x])
        if trace:
            if index_tool_trace_memory(
                source_key=f"signals_trace:{sid}",
                tool_trace=trace,
                created_at=r["created_at"],
                ref_table="signals",
                ref_id=sid,
                stock_code=r["stock_code"],
                stock_name=r["stock_name"],
            ):
                counts["tool_trace"] += 1

    for r in sc_rows:
        lid = int(r["id"])
        if r["news_summary"]:
            if index_news_memory(
                source_key=f"screening_news:{lid}",
                title=f"{r['stock_name'] or ''} screening news",
                content=r["news_summary"],
                created_at=r["created_at"],
                ref_table="screening_log",
                ref_id=lid,
                stock_code=r["stock_code"],
                stock_name=r["stock_name"],
            ):
                counts["news"] += 1

        if r["market_snapshot"]:
            if index_market_regime_memory(
                source_key=f"screening_market:{lid}",
                market_snapshot=r["market_snapshot"],
                created_at=r["created_at"],
                ref_table="screening_log",
                ref_id=lid,
                extra={"stock_name": r["stock_name"]},
            ):
                counts["market_regime"] += 1

        if index_watchlist_decision_memory(
            source_key=f"screening_decision:{lid}",
            stock_code=r["stock_code"] or "",
            stock_name=r["stock_name"] or "",
            recommendation=r["recommendation"] or "",
            reason=r["reason"] or "",
            created_at=r["created_at"],
            ref_table="screening_log",
            ref_id=lid,
            ai_response=r["ai_response"],
        ):
            counts["watchlist_decision"] += 1

        ai_ctx = _extract_rag_json_summary(r["ai_response"], max_len=500) if r["ai_response"] else ""
        trace = f"recommendation={r['recommendation'] or ''} reason={r['reason'] or ''} {ai_ctx}".strip()
        if trace:
            if index_tool_trace_memory(
                source_key=f"screening_trace:{lid}",
                tool_trace=trace,
                created_at=r["created_at"],
                ref_table="screening_log",
                ref_id=lid,
                stock_code=r["stock_code"],
                stock_name=r["stock_name"],
            ):
                counts["tool_trace"] += 1

    for r in note_rows:
        nid = int(r["id"])
        cat = (r["category"] or "").lower()
        text = f"{r['summary'] or ''}\n{r['detail'] or ''}"
        if not text.strip():
            continue
        if cat in ("daily_review", "postmortem", "review"):
            if index_postmortem_memory(
                source_key=f"note_postmortem:{nid}",
                title=r["summary"] or "daily review",
                content=text,
                created_at=r["created_at"],
                ref_table="strategy_notes",
                ref_id=nid,
                extra={"category": r["category"]},
            ):
                counts["postmortem"] += 1
        if cat in ("watchlist", "market", "screening"):
            if index_market_regime_memory(
                source_key=f"note_market:{nid}",
                market_snapshot=text,
                created_at=r["created_at"],
                ref_table="strategy_notes",
                ref_id=nid,
                extra={"category": r["category"]},
            ):
                counts["market_regime"] += 1

    return counts
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
    ai_response: str | None = None,
) -> bool:
    """스크리닝 결과 1건을 별도 FAISS 인덱스에 추가."""
    if not _OPENAI_KEY:
        return False

    ai_ctx = _extract_rag_json_summary(ai_response) if ai_response else ""
    cond_text = f"recommendation:{recommendation} {reason or ''}"
    if ai_ctx:
        cond_text += f"\n[AI_CONTEXT] {ai_ctx}"
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
        if news_summary:
            index_news_memory(
                source_key=f"screening_news:{log_id}",
                title=f"{stock_name} screening news",
                content=news_summary,
                ref_table="screening_log",
                ref_id=log_id,
                stock_name=stock_name,
            )
        index_watchlist_decision_memory(
            source_key=f"screening_decision:{log_id}",
            stock_code="",
            stock_name=stock_name,
            recommendation=recommendation,
            reason=reason or "",
            ref_table="screening_log",
            ref_id=log_id,
            ai_response=ai_response,
        )
        trace = f"recommendation={recommendation} reason={reason or ''} {ai_ctx}".strip()
        if trace:
            index_tool_trace_memory(
                source_key=f"screening_trace:{log_id}",
                tool_trace=trace,
                ref_table="screening_log",
                ref_id=log_id,
                stock_name=stock_name,
            )
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
                f"""SELECT id, stock_name, recommendation, reason, dart_summary, news_summary, ai_response
                    FROM screening_log WHERE id IN ({placeholders})""",
                valid_ids,
            ).fetchall()

        row_map = {r["id"]: dict(r) for r in rows}
        output = []
        for i, lid in enumerate(valid_ids):
            row = row_map.get(lid)
            if not row:
                continue
            ai_ctx = _extract_rag_json_summary(row.get("ai_response"), max_len=180)
            reason = (row.get("reason") or "")[:100]
            preview = f"추천:{row.get('recommendation','')} {reason}"
            if ai_ctx:
                preview += f" | {ai_ctx}"
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


class SearchAgentMemoryContextTool(BaseTool):
    """Hybrid retrieval over multi-index agent memory."""

    name = "search_agent_memory_context"
    label = "에이전트 메모리 검색"
    description = (
        "뉴스/장세/복기/툴실행흔적/워치리스트결정의 통합 메모리에서 유사 문맥을 찾습니다. "
        "벡터 유사도 + 키워드 중첩 + 최근성 가중치가 함께 반영됩니다."
    )
    input_schema = {
        "properties": {
            "query": {
                "type": "string",
                "description": "검색 쿼리",
            },
            "n_results": {
                "type": "integer",
                "description": "최대 결과 수",
                "default": 6,
            },
        },
        "required": ["query"],
    }

    def execute(self, query: str, n_results: int = 6) -> dict:
        if not _OPENAI_KEY:
            return {"error": "OPENAI_API_KEY 미설정"}
        try:
            results = search_agent_memory_context(query, n_results=n_results)
            return {"count": len(results), "results": results}
        except Exception as e:
            return {"error": str(e)}
