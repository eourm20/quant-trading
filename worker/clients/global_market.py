"""
Global market data via official API providers.

Primary provider:
- Alpha Vantage (API key required)
  - QQQ as Nasdaq proxy
  - SPY as S&P 500 proxy
  - USD/KRW exchange rate
  - WTI proxy: USO
  - Gold proxy: GLD
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_ALPHA_KEY = (os.getenv("ALPHAVANTAGE_API_KEY") or "").strip()
_ALPHA_BASE = "https://www.alphavantage.co/query"
_HEADERS = {"User-Agent": "quant-trading-worker/1.0"}
_ALPHA_MIN_INTERVAL_SEC = max(0.0, float(os.getenv("ALPHAVANTAGE_MIN_INTERVAL_SEC", "12.5") or 12.5))
_ALPHA_MAX_RETRIES = max(0, int(os.getenv("ALPHAVANTAGE_MAX_RETRIES", "2") or 2))
_ALPHA_RETRY_BACKOFF_SEC = max(0.1, float(os.getenv("ALPHAVANTAGE_RETRY_BACKOFF_SEC", "2.0") or 2.0))
_ALPHA_CACHE_TTL_SEC = max(0, int(float(os.getenv("ALPHAVANTAGE_CACHE_TTL_SEC", "900") or 900)))
_alpha_lock = threading.Lock()
_alpha_last_call_ts = 0.0
_cache_lock = threading.Lock()
_cache_global_indices: dict[str, Any] = {"value": {}, "expires_at": 0.0}
_cache_macro_proxies: dict[str, Any] = {"value": {}, "expires_at": 0.0}
_alpha_usage_lock = threading.Lock()
_alpha_usage_by_date: dict[str, int] = {}

_KST = timezone(timedelta(hours=9))


def _today_kst() -> str:
    return datetime.now(_KST).strftime("%Y-%m-%d")


def _count_alpha_http_call() -> None:
    day = _today_kst()
    with _alpha_usage_lock:
        _alpha_usage_by_date[day] = int(_alpha_usage_by_date.get(day, 0)) + 1
        calls = _alpha_usage_by_date[day]
    logger.info(f"[alpha_usage] date={day} calls={calls}")


def get_alpha_usage_stats() -> dict[str, Any]:
    """Runtime usage counters for Alpha Vantage HTTP calls.

    Note: in-memory only (resets when process restarts).
    """
    day = _today_kst()
    with _alpha_usage_lock:
        today_calls = int(_alpha_usage_by_date.get(day, 0))
        by_date = dict(_alpha_usage_by_date)
    return {
        "today": day,
        "today_calls": today_calls,
        "by_date": by_date,
        "cache_ttl_sec": _ALPHA_CACHE_TTL_SEC,
    }


def _safe_float(value: Any) -> float:
    try:
        return float(str(value).replace(",", "").strip())
    except Exception:
        return 0.0


def _fetch_alpha(params: dict[str, str]) -> dict:
    global _alpha_last_call_ts
    if not _ALPHA_KEY:
        logger.warning("ALPHAVANTAGE_API_KEY is not configured")
        return {}
    for attempt in range(_ALPHA_MAX_RETRIES + 1):
        try:
            # Free tier quota protection: keep a safe gap between requests.
            with _alpha_lock:
                now = time.monotonic()
                wait_sec = _ALPHA_MIN_INTERVAL_SEC - (now - _alpha_last_call_ts)
                if wait_sec > 0:
                    time.sleep(wait_sec)
                _alpha_last_call_ts = time.monotonic()

            q = dict(params)
            q["apikey"] = _ALPHA_KEY
            _count_alpha_http_call()
            resp = httpx.get(_ALPHA_BASE, params=q, headers=_HEADERS, timeout=8.0)
            resp.raise_for_status()
            data = resp.json() or {}
            params_brief = {
                "function": params.get("function"),
                "symbol": params.get("symbol"),
                "from_currency": params.get("from_currency"),
                "to_currency": params.get("to_currency"),
            }
            if "Error Message" in data or "Information" in data or "Note" in data:
                logger.warning(
                    f"Alpha Vantage response warning (attempt {attempt + 1}/{_ALPHA_MAX_RETRIES + 1}, "
                    f"params={params_brief}): {data}"
                )
                if attempt < _ALPHA_MAX_RETRIES:
                    time.sleep(_ALPHA_RETRY_BACKOFF_SEC * (attempt + 1))
                    continue
                logger.error(
                    "Alpha Vantage request exhausted by warning responses "
                    f"(params={params}, retries={_ALPHA_MAX_RETRIES})"
                )
                return {}
            return data
        except Exception as e:
            logger.warning(
                "Alpha Vantage fetch failed "
                f"(attempt {attempt + 1}/{_ALPHA_MAX_RETRIES + 1}, params={params}): {e}"
            )
            if attempt < _ALPHA_MAX_RETRIES:
                time.sleep(_ALPHA_RETRY_BACKOFF_SEC * (attempt + 1))
                continue
            logger.error(
                "Alpha Vantage request failed after retries "
                f"(params={params}, retries={_ALPHA_MAX_RETRIES}, error={e})"
            )
            return {}
    return {}


def _fetch_equity_change(symbol: str) -> dict[str, float]:
    data = _fetch_alpha({"function": "GLOBAL_QUOTE", "symbol": symbol})
    q = data.get("Global Quote") or {}
    price = _safe_float(q.get("05. price"))
    pct_raw = str(q.get("10. change percent") or "").replace("%", "").strip()
    pct = _safe_float(pct_raw)
    if price <= 0:
        return {}
    return {"price": price, "change_pct": pct}


def _fetch_fx_usdkrw() -> dict[str, float]:
    data = _fetch_alpha(
        {
            "function": "CURRENCY_EXCHANGE_RATE",
            "from_currency": "USD",
            "to_currency": "KRW",
        }
    )
    block = data.get("Realtime Currency Exchange Rate") or {}
    price = _safe_float(block.get("5. Exchange Rate"))
    if price <= 0:
        return {}
    return {"price": price, "change_pct": 0.0}


def get_global_indices() -> dict[str, dict]:
    """Return major global indicators for report/AI context.

    Keys are stable ASCII identifiers for robust cross-platform handling:
    - nasdaq
    - sp500
    - usdkrw
    """
    now = time.time()
    with _cache_lock:
        if _ALPHA_CACHE_TTL_SEC > 0 and now < float(_cache_global_indices.get("expires_at") or 0):
            cached = _cache_global_indices.get("value") or {}
            if isinstance(cached, dict):
                return dict(cached)

    out: dict[str, dict] = {}

    nasdaq = _fetch_equity_change("QQQ")
    if nasdaq:
        out["nasdaq"] = nasdaq

    spx = _fetch_equity_change("SPY")
    if spx:
        out["sp500"] = spx

    usdkrw = _fetch_fx_usdkrw()
    if usdkrw:
        out["usdkrw"] = usdkrw

    with _cache_lock:
        _cache_global_indices["value"] = dict(out)
        _cache_global_indices["expires_at"] = now + _ALPHA_CACHE_TTL_SEC
    return out


def get_macro_proxies() -> dict[str, dict]:
    """Return macro proxy quotes from Alpha Vantage."""
    now = time.time()
    with _cache_lock:
        if _ALPHA_CACHE_TTL_SEC > 0 and now < float(_cache_macro_proxies.get("expires_at") or 0):
            cached = _cache_macro_proxies.get("value") or {}
            if isinstance(cached, dict):
                return dict(cached)

    out: dict[str, dict] = {}

    wti = _fetch_equity_change("USO")
    if wti:
        out["wti"] = wti

    gold = _fetch_equity_change("GLD")
    if gold:
        out["gold"] = gold

    with _cache_lock:
        _cache_macro_proxies["value"] = dict(out)
        _cache_macro_proxies["expires_at"] = now + _ALPHA_CACHE_TTL_SEC
    return out


def format_global_indices_for_ai(indices: dict[str, dict] | None = None) -> str:
    if indices is None:
        indices = get_global_indices()
    if not indices:
        return ""

    parts: list[str] = []
    labels = [
        ("nasdaq", "나스닥"),
        ("sp500", "S&P500"),
        ("usdkrw", "달러/원"),
    ]
    for key, label in labels:
        data = indices.get(key) or {}
        price = _safe_float(data.get("price"))
        pct = _safe_float(data.get("change_pct"))
        if price <= 0:
            continue
        sign = "+" if pct >= 0 else ""
        if key == "usdkrw":
            parts.append(f"{label}: {price:,.0f}원 ({sign}{pct:.2f}%)")
        else:
            parts.append(f"{label}: {price:,.2f} ({sign}{pct:.2f}%)")
    return " / ".join(parts)
