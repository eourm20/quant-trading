"""
네이버 뉴스 검색 API 클라이언트 (워커용 경량)
- 종목명으로 최근 뉴스 검색
- AI 판단 프롬프트용 요약 생성
"""

import logging
import os
import re
from datetime import datetime

import requests
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '..', '.env'))

logger = logging.getLogger(__name__)

NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID", "").strip()
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET", "").strip()
NAVER_NEWS_URL = "https://openapi.naver.com/v1/search/news.json"


def _strip_html(text: str) -> str:
    """HTML 태그 및 &quot; 등 엔티티 제거."""
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&quot;", '"').replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&apos;", "'")
    return text.strip()


def _parse_pub_date(pub_date: str) -> str:
    """네이버 pubDate → YYYY-MM-DD 변환."""
    try:
        # "Mon, 20 Mar 2026 09:30:00 +0900"
        dt = datetime.strptime(pub_date, "%a, %d %b %Y %H:%M:%S %z")
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return pub_date[:10] if len(pub_date) >= 10 else pub_date


def search_news(
    query: str,
    display: int = 10,
    sort: str = "date",
) -> list[dict]:
    """네이버 뉴스 검색.

    Args:
        query: 검색어 (종목명 등)
        display: 결과 수 (1~100)
        sort: 정렬 (date=최신순, sim=정확도순)

    Returns:
        list[dict]: [{title, description, link, pub_date}, ...]
    """
    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
        return []

    try:
        resp = requests.get(
            NAVER_NEWS_URL,
            params={"query": query, "display": min(display, 100), "sort": sort},
            headers={
                "X-Naver-Client-Id": NAVER_CLIENT_ID,
                "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        items = []
        for item in data.get("items", []):
            items.append({
                "title": _strip_html(item.get("title", "")),
                "description": _strip_html(item.get("description", "")),
                "link": item.get("originallink") or item.get("link", ""),
                "pub_date": _parse_pub_date(item.get("pubDate", "")),
            })
        return items

    except Exception as e:
        logger.error(f"네이버 뉴스 검색 실패 ({query}): {e}")
        return []


def format_news_for_ai(stock_name: str, max_items: int = 5) -> str:
    """AI 판단 프롬프트용 뉴스 요약 텍스트."""
    news = search_news(stock_name, display=max_items, sort="date")

    if not news:
        return "최근 뉴스 없음"

    lines = []
    for n in news:
        title = n["title"][:60]
        desc = n["description"][:80]
        lines.append(f"  - {n['pub_date']} {title}")
        if desc:
            lines.append(f"    → {desc}")

    return "\n".join(lines)


# 거시경제·지정학 키워드 (가장 시장 영향이 큰 이슈 중심)
_MACRO_KEYWORDS = [
    "미국 관세 무역",
    "연준 FOMC 금리",
    "지정학 리스크 전쟁",
]


def get_macro_news_for_ai(max_per_keyword: int = 2, max_total: int = 6) -> str:
    """거시경제·지정학 이슈 뉴스 요약.

    미국 관세, 금리, 전쟁 등 시장 전반에 영향을 주는 매크로 이슈를
    최신 뉴스로 조회하여 AI 판단 프롬프트에 제공한다.

    Returns:
        뉴스 헤드라인 목록 문자열. 뉴스가 없거나 API 미설정 시 빈 문자열.
    """
    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
        return ""

    seen: set[str] = set()
    items: list[dict] = []

    for kw in _MACRO_KEYWORDS:
        if len(items) >= max_total:
            break
        for n in search_news(kw, display=max_per_keyword, sort="date"):
            key = n["title"][:30]
            if key not in seen:
                seen.add(key)
                items.append(n)
            if len(items) >= max_total:
                break

    if not items:
        return ""

    lines = [f"  - {n['pub_date']} {n['title'][:65]}" for n in items]
    return "\n".join(lines)


def format_sector_news_for_ai(sector_name: str, max_items: int = 3) -> str:
    """업종·섹터 관련 뉴스 요약.

    Args:
        sector_name: 업종명 (예: "방위산업", "반도체")
        max_items: 최대 기사 수

    Returns:
        뉴스 헤드라인 목록 문자열. 빈 섹터명이거나 결과 없으면 빈 문자열.
    """
    if not sector_name or not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
        return ""

    news = search_news(f"{sector_name} 업황", display=max_items, sort="date")
    if not news:
        return ""

    lines = [f"  - {n['pub_date']} {n['title'][:65]}" for n in news]
    return "\n".join(lines)
