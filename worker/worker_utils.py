"""
Pure utility functions shared across worker modules.
No global mutable state — safe to import from anywhere.
"""

import logging
import os
from datetime import datetime, time as dtime, timezone, timedelta as _td

import yaml

logger = logging.getLogger(__name__)

_KST = timezone(_td(hours=9))


def _now_kst() -> datetime:
    """UTC/로컬 관계없이 항상 KST 현재 시각 반환 (naive — 기존 코드 호환)."""
    return datetime.now(_KST).replace(tzinfo=None)


# KRX 거래 세션 (규정값)
_SESSIONS: dict[str, tuple[dtime, dtime]] = {
    "premarket":   (dtime(8, 30),  dtime(9, 0)),    # 장전 시간외 (trde_tp 61)
    "main":        (dtime(9, 0),   dtime(15, 30)),   # 정규장 (trde_tp 0/3)
    "aftermarket": (dtime(15, 40), dtime(16, 0)),    # 장후 시간외 (trde_tp 81)
    "offhours":    (dtime(16, 0),  dtime(18, 0)),    # 시간외 단일가 (trde_tp 62)
}

_MARKET_CALENDAR_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "krx_holidays.yaml")
_MARKET_CALENDAR_CACHE: tuple[float, set[str]] | None = None


def _load_krx_holiday_dates() -> set[str]:
    """Load KRX holiday dates (YYYY-MM-DD) from config file."""
    global _MARKET_CALENDAR_CACHE
    try:
        mtime = os.path.getmtime(_MARKET_CALENDAR_PATH)
    except Exception:
        mtime = -1.0
    if _MARKET_CALENDAR_CACHE and _MARKET_CALENDAR_CACHE[0] == mtime:
        return _MARKET_CALENDAR_CACHE[1]

    dates: set[str] = set()
    try:
        with open(_MARKET_CALENDAR_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        for item in (data.get("closed_dates") or []):
            text = str(item or "").strip()
            if text:
                dates.add(text)
    except Exception as e:
        logger.warning(f"[market-calendar] KRX holiday config load failed: {e}")

    _MARKET_CALENDAR_CACHE = (mtime, dates)
    return dates


def _is_krx_closed_day(dt: datetime) -> tuple[bool, str]:
    """Return (closed, reason) for a KST date."""
    if dt.weekday() >= 5:
        return True, "weekend"
    ymd = dt.strftime("%Y-%m-%d")
    if ymd in _load_krx_holiday_dates():
        return True, "holiday_calendar"
    return False, "open_day"


def _get_market_event_context(now_kst: datetime | None = None) -> dict:
    """System-level market event flags used by execution and AI context."""
    now_kst = now_kst or _now_kst()
    tomorrow = now_kst + _td(days=1)
    today_closed, today_reason = _is_krx_closed_day(now_kst)
    tomorrow_closed, tomorrow_reason = _is_krx_closed_day(tomorrow)
    return {
        "today": now_kst.strftime("%Y-%m-%d"),
        "tomorrow": tomorrow.strftime("%Y-%m-%d"),
        "today_closed": today_closed,
        "today_closed_reason": today_reason,
        "tomorrow_closed": tomorrow_closed,
        "tomorrow_closed_reason": tomorrow_reason,
    }


def _run_on_open_day(job_name: str, fn, *args, **kwargs):
    """Run scheduled job only when today is an open KRX day."""
    ctx = _get_market_event_context()
    if ctx.get("today_closed"):
        logger.info(
            f"[market-event] today={ctx.get('today')} closed "
            f"(reason={ctx.get('today_closed_reason')}) - {job_name} skip"
        )
        return None
    return fn(*args, **kwargs)


def get_current_session() -> str | None:
    """현재 거래 가능 세션 반환. 장외 시간이면 None."""
    now = _now_kst()
    if now.weekday() >= 5:
        return None
    t = now.time()
    for session, (start, end) in _SESSIONS.items():
        if start <= t <= end:
            return session
    return None


# ---------------------------------------------------------------------------
# Price / value parsers
# ---------------------------------------------------------------------------

def _safe_int_price(value) -> int:
    try:
        return abs(int(str(value or "0").replace(",", "").strip()))
    except Exception:
        return 0


def _safe_float(value) -> float:
    try:
        return float(str(value or "").replace(",", "").strip())
    except Exception:
        return 0.0


def _extract_change_pct(payload: dict | None) -> float:
    if not payload:
        return 0.0
    for key in ("prdy_ctrt", "flu_rt", "change_rate", "chg_rt"):
        if key in payload:
            v = _safe_float(payload.get(key))
            if v != 0.0:
                return v
    return 0.0


def _extract_trade_value(payload: dict | None) -> float:
    if not payload:
        return 0.0
    for key in ("acml_tr_pbmn", "acc_trdval", "tot_tr_amt"):
        if key in payload:
            v = _safe_float(payload.get(key))
            if v > 0:
                return v
    return 0.0


def _extract_first_number(payload: dict | None, keys: tuple[str, ...]) -> float:
    if not payload:
        return 0.0
    for key in keys:
        if key in payload:
            v = _safe_float(payload.get(key))
            if v != 0.0:
                return v
    return 0.0


def _fmt_pct_or_na(value: float | None) -> str:
    if value is None:
        return "N/A"
    try:
        v = float(value)
    except Exception:
        return "N/A"
    if abs(v) <= 1e-9:
        return "N/A"
    return f"{v:+.2f}%"


def _fmt_num_or_na(value: float | int | None, digits: int = 0) -> str:
    if value is None:
        return "N/A"
    try:
        v = float(value)
    except Exception:
        return "N/A"
    if abs(v) <= 1e-9:
        return "N/A"
    if digits <= 0:
        return f"{v:,.0f}"
    return f"{v:.{digits}f}"


# ---------------------------------------------------------------------------
# Market label helpers
# ---------------------------------------------------------------------------

def _label_market_regime(avg_change_pct: float) -> str:
    if avg_change_pct >= 0.8:
        return "risk_on"
    if avg_change_pct <= -0.8:
        return "risk_off"
    return "neutral"


def _label_trend(avg_change_pct: float) -> str:
    if avg_change_pct >= 0.4:
        return "bullish"
    if avg_change_pct <= -0.4:
        return "bearish"
    return "sideways"


def _label_volatility(abs_moves: list[float]) -> str:
    if not abs_moves:
        return "medium"
    m = sum(abs_moves) / len(abs_moves)
    if m >= 1.5:
        return "high"
    if m <= 0.5:
        return "low"
    return "medium"


def _label_aggressiveness(market_regime: str, volatility: str) -> str:
    if market_regime == "risk_on" and volatility != "high":
        return "high"
    if market_regime == "risk_off" or volatility == "high":
        return "low"
    return "medium"


# ---------------------------------------------------------------------------
# Date / time parsers
# ---------------------------------------------------------------------------

def _parse_hhmm(value: str, default_h: int, default_m: int) -> tuple[int, int]:
    s = str(value or "").strip()
    if ":" not in s:
        return default_h, default_m
    hh, mm = s.split(":", 1)
    try:
        return max(0, min(23, int(hh))), max(0, min(59, int(mm)))
    except Exception:
        return default_h, default_m


def _parse_ymd(value: str) -> str:
    s = str(value or "").replace("-", "").strip()
    return s if len(s) == 8 and s.isdigit() else ""


def _parse_trade_dt(value: str) -> datetime | None:
    s = str(value or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            pass
    return None


def _business_days_elapsed(start_dt, end_dt) -> int:
    """start_dt(당일 포함) 다음 거래일~end_dt까지의 평일 개수.
    한국 휴일 캘린더는 미반영, 주말만 제외.
    """
    if not start_dt or not end_dt or end_dt <= start_dt:
        return 0
    days = 0
    cur = start_dt + _td(days=1)
    while cur <= end_dt:
        if cur.weekday() < 5:
            days += 1
        cur += _td(days=1)
    return days