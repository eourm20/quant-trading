"""
Strategy reflection + policy update loop (system-enforced pipeline support).
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta

from data.db import (
    _now_kst,
    get_conn,
    save_strategy_note,
)

logger = logging.getLogger(__name__)


def _next_policy_version(agent_type: str) -> str:
    ts = _now_kst().strftime("%Y%m%d%H%M%S")
    return f"{agent_type}-v{ts}"


def get_policy_snapshot(agent_type: str) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT agent_type, policy_version, policy_json, updated_at "
            "FROM strategy_policy_state WHERE agent_type = ?",
            (agent_type,),
        ).fetchone()
    if not row:
        default = {"risk_mode": "balanced", "min_confidence": 0.5}
        return {"agent_type": agent_type, "policy_version": f"{agent_type}-v0", "policy": default}
    try:
        policy = json.loads(row["policy_json"] or "{}")
    except Exception:
        policy = {}
    return {
        "agent_type": row["agent_type"],
        "policy_version": row["policy_version"] or f"{agent_type}-v0",
        "policy": policy or {},
        "updated_at": row["updated_at"],
    }


def upsert_policy_snapshot(agent_type: str, policy: dict, reason: str = "") -> dict:
    now = _now_kst().strftime("%Y-%m-%d %H:%M:%S")
    version = _next_policy_version(agent_type)
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO strategy_policy_state (agent_type, policy_version, policy_json, updated_at, note)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(agent_type) DO UPDATE SET
                policy_version=excluded.policy_version,
                policy_json=excluded.policy_json,
                updated_at=excluded.updated_at,
                note=excluded.note
            """,
            (agent_type, version, json.dumps(policy or {}, ensure_ascii=False), now, reason or ""),
        )
        conn.commit()
    return {"agent_type": agent_type, "policy_version": version, "policy": policy or {}}


def save_reflection(
    agent_type: str,
    stock_code: str,
    stock_name: str,
    status: str,
    praise_tags: list[str] | None = None,
    reflection_tags: list[str] | None = None,
    quality_score: float | None = None,
    detail: dict | None = None,
) -> int:
    now = _now_kst().strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO strategy_reflection_logs
                (created_at, agent_type, stock_code, stock_name, status,
                 praise_tags, reflection_tags, quality_score, detail_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                agent_type,
                stock_code,
                stock_name,
                status,
                json.dumps(praise_tags or [], ensure_ascii=False),
                json.dumps(reflection_tags or [], ensure_ascii=False),
                quality_score,
                json.dumps(detail or {}, ensure_ascii=False),
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)


def enqueue_policy_update(
    agent_type: str,
    suggestion: dict,
    low_risk: bool = True,
    source_reflection_id: int | None = None,
) -> int:
    now = _now_kst().strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO strategy_policy_update_queue
                (created_at, agent_type, suggestion_json, low_risk, status, source_reflection_id)
            VALUES (?, ?, ?, ?, 'pending', ?)
            """,
            (
                now,
                agent_type,
                json.dumps(suggestion or {}, ensure_ascii=False),
                1 if low_risk else 0,
                source_reflection_id,
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)


def mark_policy_update(update_id: int, status: str, applied_version: str = "", note: str = "") -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE strategy_policy_update_queue SET status = ?, applied_version = ?, note = ? WHERE id = ?",
            (status, applied_version, note, update_id),
        )
        conn.commit()


def reflect_judgment(
    stock_code: str,
    stock_name: str,
    opinion: str,
    preflight_ok: bool,
    missing_fields: list[str],
    policy_version: str,
) -> bool:
    first = (opinion or "").splitlines()[0] if opinion else ""
    status = "incomplete_context" if not preflight_ok else "completed"
    praise: list[str] = []
    reflection: list[str] = []
    quality = 0.5

    if not preflight_ok:
        reflection.append("INCOMPLETE_CONTEXT")
        quality = 0.2
    elif "[매도]" in first:
        praise.append("DECISIVE_EXIT")
        quality = 0.6
    elif "[매수]" in first or "[추가매수" in first or "[물타기" in first:
        praise.append("ACTIONABLE_ENTRY")
        quality = 0.6
    elif "[홀드]" in first:
        reflection.append("HOLD_CONSERVATIVE")
        quality = 0.5

    rid = save_reflection(
        agent_type="judgment",
        stock_code=stock_code,
        stock_name=stock_name,
        status=status,
        praise_tags=praise,
        reflection_tags=reflection,
        quality_score=quality,
        detail={
            "opinion_head": first[:200],
            "missing_fields": missing_fields,
            "policy_version": policy_version,
        },
    )
    return rid > 0


def reflect_research(
    status: str,
    result_text: str,
    policy_version: str,
) -> bool:
    text = result_text or ""
    added = text.count("watchlist: done")
    not_added = text.count("watchlist: not added")
    praise: list[str] = []
    reflection: list[str] = []
    quality = 0.5

    if status == "incomplete_context":
        reflection.append("INCOMPLETE_CONTEXT")
        quality = 0.2
    else:
        if added > 0:
            praise.append("SELECTIVE_ADD")
            quality += 0.1
        if not_added > added:
            reflection.append("STRICT_FILTERING")
            quality -= 0.05
        quality = max(0.0, min(1.0, quality))

    rid = save_reflection(
        agent_type="research",
        stock_code="",
        stock_name="",
        status=status,
        praise_tags=praise,
        reflection_tags=reflection,
        quality_score=quality,
        detail={
            "summary": text[:500],
            "policy_version": policy_version,
            "added_count": added,
            "not_added_count": not_added,
        },
    )
    return rid > 0


def _aggregate_quality(agent_type: str, days: int = 7) -> tuple[int, float]:
    since = (_now_kst() - timedelta(days=max(1, int(days)))).strftime("%Y-%m-%d")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) c, AVG(quality_score) a "
            "FROM strategy_reflection_logs WHERE agent_type = ? AND created_at >= ?",
            (agent_type, since),
        ).fetchone()
    count = int(row["c"] or 0) if row else 0
    avg_q = float(row["a"]) if row and row["a"] is not None else 0.0
    return count, avg_q


def _has_conflicting_pending_updates(agent_type: str) -> bool:
    """If there are non-low-risk pending updates, do not auto-apply low-risk updates."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) c FROM strategy_policy_update_queue "
            "WHERE agent_type = ? AND status = 'pending' AND low_risk = 0",
            (agent_type,),
        ).fetchone()
    return bool(int(row["c"] or 0)) if row else False


def _hours_since_policy_update(agent_type: str) -> float:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT updated_at FROM strategy_policy_state WHERE agent_type = ?",
            (agent_type,),
        ).fetchone()
    if not row or not row["updated_at"]:
        return 10_000.0
    try:
        last = row["updated_at"]
        from datetime import datetime

        last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
        return max(0.0, (_now_kst() - last_dt).total_seconds() / 3600.0)
    except Exception:
        return 10_000.0


def _is_adjacent_risk_mode(cur_mode: str, target_mode: str) -> bool:
    order = ["defensive", "balanced", "opportunistic"]
    try:
        return abs(order.index(cur_mode) - order.index(target_mode)) <= 1
    except Exception:
        return False


def run_policy_update_cycle(min_samples: int = 10, auto_apply_low_risk: bool = True) -> dict:
    """
    Reflection -> policy update automatic loop.
    Low-risk update only: risk_mode adjustment.
    """
    out = {"queued": 0, "applied": 0, "skipped": 0}
    for agent_type in ("judgment", "research"):
        cnt, avg_q = _aggregate_quality(agent_type, days=7)
        if cnt < max(1, int(min_samples)):
            out["skipped"] += 1
            continue

        target_mode = "balanced"
        if avg_q < 0.45:
            target_mode = "defensive"
        elif avg_q > 0.7:
            target_mode = "opportunistic"

        snap = get_policy_snapshot(agent_type)
        cur_mode = str((snap.get("policy") or {}).get("risk_mode", "balanced"))
        if cur_mode == target_mode:
            out["skipped"] += 1
            continue

        suggestion = {
            "risk_mode": target_mode,
            "reason": f"7d avg_quality={avg_q:.3f}, samples={cnt}",
        }
        qid = enqueue_policy_update(agent_type, suggestion=suggestion, low_risk=True)
        out["queued"] += 1

        if auto_apply_low_risk:
            if _has_conflicting_pending_updates(agent_type):
                mark_policy_update(qid, "rejected", note="conflicting_pending_high_risk_update")
                out["skipped"] += 1
                continue
            if _hours_since_policy_update(agent_type) < 24:
                mark_policy_update(qid, "rejected", note="update_cooldown_under_24h")
                out["skipped"] += 1
                continue
            if not _is_adjacent_risk_mode(cur_mode, target_mode):
                mark_policy_update(qid, "rejected", note="risk_mode_jump_blocked")
                out["skipped"] += 1
                continue

            new_policy = dict(snap.get("policy") or {})
            new_policy["risk_mode"] = target_mode
            updated = upsert_policy_snapshot(agent_type, new_policy, reason=suggestion["reason"])
            mark_policy_update(qid, "applied", applied_version=updated["policy_version"], note="auto_apply_low_risk")
            out["applied"] += 1
            try:
                save_strategy_note(
                    "general",
                    f"{agent_type} policy auto-update",
                    f"policy_version={updated['policy_version']}\n{json.dumps(suggestion, ensure_ascii=False)}",
                )
            except Exception:
                pass

    logger.info(f"[PolicyLoop] queued={out['queued']} applied={out['applied']} skipped={out['skipped']}")
    return out
