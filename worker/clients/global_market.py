"""
글로벌 시장 지수 조회 (Yahoo Finance API)
- 나스닥, S&P500, 달러/원 환율
- httpx 단일 요청으로 여러 심볼 동시 조회
"""

import logging
import httpx

logger = logging.getLogger(__name__)

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
    try:
        resp = httpx.get(url, headers=_HEADERS, timeout=6)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("quoteResponse", {}).get("result") or []

        # 심볼 → 이름 역매핑
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

    except Exception as e:
        logger.debug(f"글로벌 지수 조회 실패: {e}")
        # query1 실패 시 query2 폴백
        try:
            url2 = url.replace("query1.", "query2.")
            resp2 = httpx.get(url2, headers=_HEADERS, timeout=6)
            resp2.raise_for_status()
            data2 = resp2.json()
            items2 = data2.get("quoteResponse", {}).get("result") or []
            symbol_to_name = {v: k for k, v in _SYMBOLS.items()}
            results2: dict[str, dict] = {}
            for item in items2:
                symbol = item.get("symbol", "")
                name = symbol_to_name.get(symbol)
                if not name:
                    continue
                price = float(item.get("regularMarketPrice") or 0)
                change_pct = float(item.get("regularMarketChangePercent") or 0)
                if price:
                    results2[name] = {"price": price, "change_pct": change_pct}
            return results2
        except Exception as e2:
            logger.debug(f"글로벌 지수 폴백 조회도 실패: {e2}")
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
