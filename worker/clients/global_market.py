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
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_ALPHA_KEY = (os.getenv("ALPHAVANTAGE_API_KEY") or "").strip()
_ALPHA_BASE = "https://www.alphavantage.co/query"
_HEADERS = {"User-Agent": "quant-trading-worker/1.0"}


def _safe_float(value: Any) -> float:
    try:
        return float(str(value).replace(",", "").strip())
    except Exception:
        return 0.0


def _fetch_alpha(params: dict[str, str]) -> dict:
    if not _ALPHA_KEY:
        logger.warning("ALPHAVANTAGE_API_KEY is not configured")
        return {}
    try:
        q = dict(params)
        q["apikey"] = _ALPHA_KEY
        resp = httpx.get(_ALPHA_BASE, params=q, headers=_HEADERS, timeout=8.0)
        resp.raise_for_status()
        data = resp.json() or {}
        if "Error Message" in data or "Information" in data or "Note" in data:
            logger.warning(f"Alpha Vantage response warning: {data}")
            return {}
        return data
    except Exception as e:
        logger.warning(f"Alpha Vantage fetch failed: {e}")
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
    """Return major global indicators for report/AI context."""
    out: dict[str, dict] = {}

    nasdaq = _fetch_equity_change("QQQ")
    if nasdaq:
        out["나스닥"] = nasdaq

    spx = _fetch_equity_change("SPY")
    if spx:
        out["S&P500"] = spx

    usdkrw = _fetch_fx_usdkrw()
    if usdkrw:
        out["달러/원"] = usdkrw

    return out


def get_macro_proxies() -> dict[str, dict]:
    """Return macro proxy quotes from Alpha Vantage."""
    out: dict[str, dict] = {}

    wti = _fetch_equity_change("USO")
    if wti:
        out["wti"] = wti

    gold = _fetch_equity_change("GLD")
    if gold:
        out["gold"] = gold

    return out


def format_global_indices_for_ai(indices: dict[str, dict] | None = None) -> str:
    if indices is None:
        indices = get_global_indices()
    if not indices:
        return ""

    parts: list[str] = []
    for name in ("나스닥", "S&P500", "달러/원"):
        data = indices.get(name) or {}
        price = _safe_float(data.get("price"))
        pct = _safe_float(data.get("change_pct"))
        if price <= 0:
            continue
        sign = "+" if pct >= 0 else ""
        if name == "달러/원":
            parts.append(f"{name}: {price:,.0f}원 ({sign}{pct:.2f}%)")
        else:
            parts.append(f"{name}: {price:,.2f} ({sign}{pct:.2f}%)")
    return " / ".join(parts)
