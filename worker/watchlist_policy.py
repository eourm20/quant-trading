"""Shared watchlist/position payload normalization for tool paths."""

from __future__ import annotations

from typing import Any

# Value-based watchlist fields
VALUE_FIELDS = {
    "rsi_oversold",
    "rsi_overbought",
    "rsi_oversold_intraday",
    "rsi_critical",
    "volume_surge_ratio",
    "cci_oversold",
    "cci_overbought",
}

# Boolean watchlist fields
FLAG_FIELDS = {
    "golden_cross",
    "death_cross",
    "ma20_support_break",
    "ma5_support_break",
    "ma5_recovery",
    "new_high_20d",
    "trend_follow_entry",
    "macd_golden_cross",
    "macd_death_cross",
    "bollinger_upper_break",
    "bollinger_lower_break",
    "bollinger_critical_below",
    "stochastic_golden_cross",
    "stochastic_death_cross",
    "ichimoku_golden_cross",
    "ichimoku_death_cross",
    "ichimoku_cloud_breakout",
    "ichimoku_cloud_breakdown",
}

# Position-only fields (never written to watchlist table)
POSITION_ONLY_FIELDS = {
    "target_price",
    "stop_loss_price",
    "add_buy_price",
    "mid_sell_price",
    "rsi_oversold_add",
    "bollinger_lower_break_add",
    "ma5_recovery_add",
}

CORE_FALLBACK = {
    "rsi_oversold": 40,
    "rsi_overbought": 65,
    "golden_cross": True,
    "death_cross": True,
    "volume_surge_ratio": 2.0,
    "bollinger_lower_break": True,
    "ma20_support_break": True,
    "trend_follow_entry": True,
}

_INT_VALUE_FIELDS = {
    "rsi_oversold",
    "rsi_overbought",
    "rsi_oversold_intraday",
    "rsi_critical",
    "cci_oversold",
    "cci_overbought",
}


def _to_number(field: str, value: Any) -> int | float | None:
    if value is None:
        return None
    try:
        raw = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    if field in _INT_VALUE_FIELDS:
        return int(raw)
    return raw


def normalize_watchlist_payload(
    *,
    horizon: str = "중기",
    analysis: dict | None = None,
    raw_conditions: dict | None = None,
) -> tuple[dict, dict]:
    """
    Returns:
      - watchlist_payload: safe conditions for watchlist table (+horizon)
      - position_payload: position-only fields for positions table updates
    """
    analysis = analysis or {}
    raw_conditions = raw_conditions or {}

    watchlist_payload: dict[str, Any] = {}
    position_payload: dict[str, Any] = {}

    # 1) Prefer analysis.enabled_conditions when available
    enabled_conditions = analysis.get("enabled_conditions", {})
    if isinstance(enabled_conditions, dict):
        for cond_id, cond_info in enabled_conditions.items():
            if not isinstance(cond_info, dict) or not cond_info.get("enabled"):
                continue
            if cond_id in POSITION_ONLY_FIELDS:
                continue
            if cond_id in VALUE_FIELDS:
                candidate = cond_info.get("value", analysis.get(cond_id))
                parsed = _to_number(cond_id, candidate)
                if parsed is not None:
                    watchlist_payload[cond_id] = parsed
            elif cond_id in FLAG_FIELDS:
                watchlist_payload[cond_id] = True

    # 2) Merge explicit tool conditions
    for field, value in raw_conditions.items():
        if field in POSITION_ONLY_FIELDS:
            parsed = _to_number(field, value)
            if parsed is not None:
                position_payload[field] = parsed
            continue
        if field in VALUE_FIELDS:
            parsed = _to_number(field, value)
            if parsed is not None:
                watchlist_payload[field] = parsed
            continue
        if field in FLAG_FIELDS:
            watchlist_payload[field] = bool(value)
            continue
        if field == "strategy_note":
            txt = str(value or "").strip()
            if txt:
                watchlist_payload[field] = txt

    # 3) Guaranteed fallback so watchlist always has practical triggers
    if not any(k in watchlist_payload for k in (VALUE_FIELDS | FLAG_FIELDS)):
        for field, default_value in CORE_FALLBACK.items():
            if field in VALUE_FIELDS:
                fallback_value = analysis.get(field, default_value)
                parsed = _to_number(field, fallback_value)
                watchlist_payload[field] = parsed if parsed is not None else default_value
            else:
                watchlist_payload[field] = bool(default_value)

    watchlist_payload["horizon"] = horizon or "중기"
    return watchlist_payload, position_payload
