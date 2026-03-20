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
