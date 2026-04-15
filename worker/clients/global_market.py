"""
글로벌 시장 지수 조회 (Yahoo Finance API)
- 나스닥, S&P500, 달러/원 환율
- httpx 단일 요청으로 여러 심볼 동시 조회
"""

import logging
import httpx
import time

logger = logging.getLogger(__name__)
_LAST_401_LOG_TS = 0.0
_LOG_COOLDOWN_SEC = 1800  # 30 minutes


def _log_fetch_error(stage: str, err: Exception) -> None:
    """401은 Yahoo 정책 이슈로 빈번하므로 저소음 로그로 처리."""
    global _LAST_401_LOG_TS
    status = getattr(getattr(err, "response", None), "status_code", None)
    if status == 401:
        now = time.time()
        if (now - _LAST_401_LOG_TS) >= _LOG_COOLDOWN_SEC:
            _LAST_401_LOG_TS = now
            logger.info(f"글로벌 지수 {stage} 401 응답 (Yahoo 제한 가능) — fallback 진행")
        return
    logger.warning(f"글로벌 지수 {stage} 조회 실패: {err}")

# 조회할 글로벌 심볼 (Yahoo Finance 코드)
_SYMBOLS = {
    "나스닥": "^IXIC",
    "S&P500": "^GSPC",
    "달러/원": "USDKRW=X",
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


def get_global_indices() -> dict[str, dict]:
    """Yahoo Finance v7 quote API로 주요 글로벌 지수 조회.

    Returns:
        {
            "나스닥": {"price": 17000.0, "change_pct": -1.23},
            "S&P500": {"price": 5000.0, "change_pct": -0.5},
            "달러/원": {"price": 1380.0, "change_pct": 0.2},
        }
        실패 시 빈 dict 반환.
    """
    symbols_str = ",".join(_SYMBOLS.values())
    url = (
        "https://query1.finance.yahoo.com/v7/finance/quote"
        f"?symbols={symbols_str}&fields=regularMarketPrice,regularMarketChangePercent"
    )
    def _parse_items(items: list) -> dict:
        symbol_to_name = {v: k for k, v in _SYMBOLS.items()}
        results: dict[str, dict] = {}
        for item in items:
            symbol = item.get("symbol", "")
            name = symbol_to_name.get(symbol)
            if not name:
                continue
            price = float(item.get("regularMarketPrice") or 0)
            change_pct = float(item.get("regularMarketChangePercent") or 0)
            if price:
                results[name] = {"price": price, "change_pct": change_pct}
        return results

    # 1차: query1 v7
    try:
        resp = httpx.get(url, headers=_HEADERS, timeout=6)
        resp.raise_for_status()
        items = resp.json().get("quoteResponse", {}).get("result") or []
        result = _parse_items(items)
        if result:
            return result
        logger.warning(f"글로벌 지수 v7 응답 비어있음: {resp.text[:200]}")
    except Exception as e:
        _log_fetch_error("v7", e)

    # 2차: query2 v7
    try:
        url2 = url.replace("query1.", "query2.")
        resp2 = httpx.get(url2, headers=_HEADERS, timeout=6)
        resp2.raise_for_status()
        items2 = resp2.json().get("quoteResponse", {}).get("result") or []
        result2 = _parse_items(items2)
        if result2:
            return result2
        logger.warning(f"글로벌 지수 v7 query2 응답 비어있음: {resp2.text[:200]}")
    except Exception as e2:
        _log_fetch_error("v7 query2", e2)

    # 3차: v8 chart API (심볼별 개별 조회)
    try:
        symbol_to_name = {v: k for k, v in _SYMBOLS.items()}
        result3: dict[str, dict] = {}
        for name, symbol in _SYMBOLS.items():
            r = httpx.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                params={"interval": "1d", "range": "2d"},
                headers=_HEADERS,
                timeout=6,
            )
            r.raise_for_status()
            meta = r.json().get("chart", {}).get("result", [{}])[0].get("meta", {})
            price = float(meta.get("regularMarketPrice") or 0)
            prev = float(meta.get("chartPreviousClose") or meta.get("previousClose") or 0)
            change_pct = ((price - prev) / prev * 100) if prev else 0.0
            if price:
                result3[name] = {"price": price, "change_pct": change_pct}
        if result3:
            return result3
    except Exception as e3:
        _log_fetch_error("v8", e3)

    return {}


def format_global_indices_for_ai(indices: dict[str, dict] | None = None) -> str:
    """글로벌 지수를 AI 프롬프트용 한 줄 텍스트로 포맷.

    Example:
        "나스닥: 17,234.56pt (-1.23%) / S&P500: 5,012.34pt (-0.50%) / 달러/원: 1,382원 (+0.21%)"
    """
    if indices is None:
        indices = get_global_indices()
    if not indices:
        return ""

    parts = []
    for name in ("나스닥", "S&P500", "달러/원"):  # 표시 순서 고정
        data = indices.get(name)
        if not data:
            continue
        price = data["price"]
        pct = data["change_pct"]
        sign = "+" if pct >= 0 else ""
        if name == "달러/원":
            parts.append(f"{name}: {price:,.0f}원 ({sign}{pct:.2f}%)")
        else:
            parts.append(f"{name}: {price:,.2f}pt ({sign}{pct:.2f}%)")

    return " / ".join(parts)
