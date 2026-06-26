"""
Adaptive policy engine based on historical outcomes.

This module does not retrain models. It computes a context-aware
"aggressive / balanced / conservative" stance from recent similar outcomes
and returns execution guardrails.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from data.db import _now_kst, get_conn


def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(str(v).replace(",", "").strip())
    except Exception:
        return default


def _market_regime_label(kospi_rate: float, kosdaq_rate: float) -> str:
    m = (float(kospi_rate) + float(kosdaq_rate)) / 2.0
    if m >= 0.8:
        return "risk_on"
    if m <= -0.8:
        return "risk_off"
    return "neutral"


@dataclass
class AdaptivePolicy:
    stance: str  # aggressive | balanced | conservative
    score: int
    samples: int
    hit_rate_3d: float | None
    avg_3d: float | None
    qty_multiplier: float
    allow_new_entry: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "stance": self.stance,
            "score": self.score,
            "samples": self.samples,
            "hit_rate_3d": self.hit_rate_3d,
            "avg_3d": self.avg_3d,
            "qty_multiplier": self.qty_multiplier,
            "allow_new_entry": self.allow_new_entry,
            "reason": self.reason,
        }


def _fetch_similar_signal_stats(signal_type: str, days: int = 180) -> tuple[int, float | None, float | None, str]:
    """Return directional edge stats for the judgment policy.

    avg3 is normalized so positive means "the historical action was useful":
    entry/add want price to rise after buy, exit wants price to fall after sell.
    """
    since = (_now_kst() - timedelta(days=max(7, int(days)))).strftime("%Y-%m-%d")
    st = signal_type or ""

    if st == "exit":
        where = "signal_type = ? AND verdict = '매도'"
        params = (since, st)
        hit_sql = "SUM(CASE WHEN result_pct < 0 THEN 1 ELSE 0 END) AS wins"
        avg_sql = "AVG(-result_pct) AS avg3"
        basis = "exit_sell_signals"
    elif st == "add":
        where = "signal_type = ? AND (action = '매수' OR verdict = '매수')"
        params = (since, st)
        hit_sql = "SUM(CASE WHEN result_pct > 0 THEN 1 ELSE 0 END) AS wins"
        avg_sql = "AVG(result_pct) AS avg3"
        basis = "add_buy_actions"
    elif st == "entry":
        where = "signal_type = ? AND verdict = '매수'"
        params = (since, st)
        hit_sql = "SUM(CASE WHEN result_pct > 0 THEN 1 ELSE 0 END) AS wins"
        avg_sql = "AVG(result_pct) AS avg3"
        basis = "entry_buy_signals"
    else:
        # Mixed/unknown signal types do not have a stable directional meaning.
        return 0, None, None, "mixed_or_unknown"

    with get_conn() as conn:
        row = conn.execute(
            f"""
            SELECT
              COUNT(*) AS n,
              {avg_sql},
              {hit_sql}
            FROM signals
            WHERE created_at >= ?
              AND {where}
              AND result_pct IS NOT NULL
              AND ABS(result_pct) <= 200
            """,
            params,
        ).fetchone()
    n = int(row["n"] or 0) if row else 0
    avg3 = float(row["avg3"]) if row and row["avg3"] is not None else None
    hit = None
    if row and n > 0:
        hit = round((int(row["wins"] or 0) / n) * 100.0, 1)
    return n, hit, avg3, basis


def _fetch_recent_buy_loss_streak(limit: int = 5) -> int:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT result_3d
            FROM trades
            WHERE side = '매수'
              AND result_3d IS NOT NULL
            ORDER BY executed_at DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    streak = 0
    for r in rows:
        if _to_float(r["result_3d"]) < 0:
            streak += 1
        else:
            break
    return streak


def get_judgment_adaptive_policy(
    signal_type: str,
    kospi_rate: float = 0.0,
    kosdaq_rate: float = 0.0,
    trigger_count: int = 0,
) -> AdaptivePolicy:
    st = signal_type or ""
    samples, hit, avg3, basis = _fetch_similar_signal_stats(signal_type=st, days=180)
    loss_streak = _fetch_recent_buy_loss_streak(limit=5)
    regime = _market_regime_label(kospi_rate, kosdaq_rate)

    score = 0
    if st in ("both", ""):
        score -= 1
    if st == "exit":
        if samples < 20:
            score -= 1
        if avg3 is not None and avg3 <= 0:
            score -= 1
    if samples >= 8 and hit is not None and avg3 is not None:
        if hit >= 58.0:
            score += 1
        if avg3 >= 1.0:
            score += 1
        if hit <= 42.0:
            score -= 1
        if avg3 <= -0.5:
            score -= 1
    # Keep adaptive brakes, but avoid over-blocking after short losing streaks.
    if loss_streak >= 4:
        score -= 1
    if regime == "risk_on":
        score += 1
    elif regime == "risk_off":
        score -= 1
    if trigger_count >= 3:
        score += 1

    if score >= 2:
        return AdaptivePolicy(
            stance="aggressive",
            score=score,
            samples=samples,
            hit_rate_3d=hit,
            avg_3d=avg3,
            qty_multiplier=1.2,
            allow_new_entry=True,
            reason=f"score={score}, regime={regime}, basis={basis}, samples={samples}, hit={hit}, edge3={avg3}",
        )
    # Move conservative gate one notch lower so borderline cases stay tradable.
    if score <= -2:
        return AdaptivePolicy(
            stance="conservative",
            score=score,
            samples=samples,
            hit_rate_3d=hit,
            avg_3d=avg3,
            qty_multiplier=0.7,
            allow_new_entry=(st == "add" or (trigger_count >= 3 and regime != "risk_off")),
            reason=f"score={score}, regime={regime}, basis={basis}, loss_streak={loss_streak}, samples={samples}",
        )
    # Gate: score<0 + 부진한 hit_rate → 충분한 샘플에서 실력 미달이므로 신규 진입 차단
    _neg_gate = (
        score < 0
        and samples >= 10
        and hit is not None
        and hit < 45.0
    )
    return AdaptivePolicy(
        stance="balanced",
        score=score,
        samples=samples,
        hit_rate_3d=hit,
        avg_3d=avg3,
        qty_multiplier=1.0,
        allow_new_entry=not _neg_gate,
        reason=f"score={score}, regime={regime}, basis={basis}, samples={samples}, neg_gate={_neg_gate}",
    )


def get_research_adaptive_policy(
    kospi_rate: float = 0.0,
    kosdaq_rate: float = 0.0,
    base_max_additions: int = 5,
) -> dict[str, Any]:
    since = (_now_kst() - timedelta(days=90)).strftime("%Y-%m-%d")
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT
              COUNT(*) AS n,
              AVG(result_7d) AS avg7,
              SUM(CASE WHEN result_7d > 0 THEN 1 ELSE 0 END) AS pos
            FROM screening_log
            WHERE created_at >= ?
              AND recommendation = '관심종목 등록'
              AND result_7d IS NOT NULL
            """,
            (since,),
        ).fetchone()
    n = int(row["n"] or 0) if row else 0
    avg7 = float(row["avg7"]) if row and row["avg7"] is not None else None
    hit = round((int(row["pos"] or 0) / n) * 100.0, 1) if row and n > 0 else None
    regime = _market_regime_label(kospi_rate, kosdaq_rate)

    score = 0
    if n >= 8 and hit is not None and avg7 is not None:
        if hit >= 57.0:
            score += 1
        if avg7 >= 1.0:
            score += 1
        if hit <= 43.0:
            score -= 1
        if avg7 <= -0.5:
            score -= 1
    if regime == "risk_on":
        score += 1
    elif regime == "risk_off":
        score -= 1

    max_add = int(base_max_additions or 5)
    stance = "balanced"
    if score >= 2:
        stance = "aggressive"
        max_add = min(max_add + 1, 8)
    elif score <= -1:
        stance = "conservative"
        max_add = max(1, min(max_add, 2))

    return {
        "stance": stance,
        "score": score,
        "samples": n,
        "hit_rate_7d": hit,
        "avg_7d": avg7,
        "max_additions": max_add,
        "reason": f"score={score}, regime={regime}, samples={n}, hit7d={hit}, avg7d={avg7}",
    }
