"""
외부 정보 도구: 뉴스, DART 공시/재무.
"""

from __future__ import annotations
import logging
from worker.agents.tools.registry import BaseTool

logger = logging.getLogger(__name__)


class GetNewsTool(BaseTool):
    name = "get_news"
    label = "뉴스 검색"
    description = "종목명 기준으로 최근 뉴스를 검색하여 반환합니다."
    input_schema = {
        "properties": {
            "stock_name": {
                "type": "string",
                "description": "종목명 (예: 삼성전자)",
            },
            "max_items": {
                "type": "integer",
                "description": "최대 뉴스 수",
                "default": 5,
            },
        },
        "required": ["stock_name"],
    }

    def execute(self, stock_name: str, max_items: int = 5) -> dict:
        try:
            from worker.clients.news_client import format_news_for_ai, NAVER_CLIENT_ID
            if not NAVER_CLIENT_ID:
                return {"error": "뉴스 API 미설정"}
            text = format_news_for_ai(stock_name, max_items=max_items)
            return {"news": text}
        except Exception as e:
            return {"error": str(e)}


class GetDartTool(BaseTool):
    name = "get_dart"
    label = "공시·재무 조회"
    description = "DART에서 종목의 최근 공시 및 재무 정보를 조회합니다."
    input_schema = {
        "properties": {
            "stock_code": {
                "type": "string",
                "description": "종목 코드 (예: 005930)",
            },
        },
        "required": ["stock_code"],
    }

    def execute(self, stock_code: str) -> dict:
        try:
            from worker.clients.dart_client import format_full_context_for_ai, DART_API_KEY
            if not DART_API_KEY:
                return {"error": "DART API 미설정"}
            text = format_full_context_for_ai(stock_code)
            return {"dart": text}
        except Exception as e:
            return {"error": str(e)}
