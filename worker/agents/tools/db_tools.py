"""
DB 도구: 신호 이력, 진입 근거, watchlist 업데이트, 관심종목 추가.
"""

from __future__ import annotations
import logging
from worker.agents.tools.registry import BaseTool

logger = logging.getLogger(__name__)


class GetSignalHistoryTool(BaseTool):
    name = "get_signal_history"
    label = "AI 판단 이력 조회"
    description = "특정 종목의 과거 AI 판단 이력(판정, 날짜, 결과 수익률)을 조회합니다."
    input_schema = {
        "properties": {
            "stock_code": {"type": "string", "description": "종목 코드"},
            "signal_type": {
                "type": "string",
                "description": "신호 유형 필터 — 'entry'/'exit'/'add'/'both'/'' (전체)",
                "default": "",
            },
            "limit": {"type": "integer", "description": "최대 조회 건수", "default": 5},
        },
        "required": ["stock_code"],
    }

    def execute(self, stock_code: str, signal_type: str = "", limit: int = 5) -> dict:
        try:
            from data.db import get_signal_history
            rows = get_signal_history(stock_code, signal_type=signal_type, limit=limit)
            return {"history": rows}
        except Exception as e:
            return {"error": str(e)}


class GetEntryReasonTool(BaseTool):
    name = "get_entry_reason"
    label = "진입 근거 조회"
    description = "해당 종목의 최근 전략 노트(진입 근거, 매매 메모)를 조회합니다."
    input_schema = {
        "properties": {
            "stock_code": {
                "type": "string",
                "description": "종목 코드. 생략하면 전체 최근 노트 반환.",
                "default": "",
            },
            "limit": {"type": "integer", "description": "최대 조회 건수", "default": 5},
        },
        "required": [],
    }

    def execute(self, stock_code: str = "", limit: int = 5) -> dict:
        try:
            from data.db import get_strategy_notes
            notes = get_strategy_notes(limit=limit)
            if stock_code:
                # 전략 노트에는 stock_code 필드가 없으므로 summary/detail에서 키워드 검색
                notes = [n for n in notes if stock_code in str(n.get("summary", "")) or
                         stock_code in str(n.get("detail", ""))]
            return {"notes": notes}
        except Exception as e:
            return {"error": str(e)}


class UpdateWatchlistTool(BaseTool):
    name = "update_watchlist"
    label = "조건값 변경"
    description = (
        "watchlist의 신호 조건 임계값을 변경합니다. "
        "변경 가능 필드: rsi_oversold, rsi_overbought, rsi_oversold_intraday, volume_surge_ratio. "
        "목표가/손절가는 quant_position_update를 사용하세요."
    )
    input_schema = {
        "properties": {
            "stock_code": {"type": "string", "description": "종목 코드"},
            "field": {
                "type": "string",
                "description": "변경할 필드명 (rsi_oversold / rsi_overbought / rsi_oversold_intraday / volume_surge_ratio)",
            },
            "value": {
                "type": "number",
                "description": "새 값",
            },
            "reason": {
                "type": "string",
                "description": "변경 근거 (로그용)",
                "default": "",
            },
        },
        "required": ["stock_code", "field", "value"],
    }

    _ALLOWED_FIELDS = {
        "rsi_oversold", "rsi_overbought",
        "rsi_oversold_intraday", "volume_surge_ratio",
    }

    def execute(self, stock_code: str, field: str, value: float, reason: str = "") -> dict:
        if field not in self._ALLOWED_FIELDS:
            return {"error": f"변경 불가 필드: {field}. 허용: {', '.join(self._ALLOWED_FIELDS)}"}
        try:
            from data.db import update_stock_field
            update_stock_field(stock_code, field, value)
            logger.info(f"[Agent] watchlist 업데이트: {stock_code} {field}={value} ({reason})")
            return {"ok": True, "stock_code": stock_code, "field": field, "value": value}
        except Exception as e:
            return {"error": str(e)}


class AddToWatchlistTool(BaseTool):
    """Research Agent 전용 — 관심종목 신규 등록."""

    name = "add_to_watchlist"
    label = "관심종목 등록"
    description = "신규 종목을 관심종목(watchlist)에 등록합니다. 이미 존재하면 무시됩니다."
    input_schema = {
        "properties": {
            "stock_code": {"type": "string", "description": "종목 코드"},
            "stock_name": {"type": "string", "description": "종목명"},
            "horizon": {
                "type": "string",
                "description": "매매 기간: '단기'/'중기'/'장기'/''",
                "default": "중기",
            },
            "conditions": {
                "type": "object",
                "description": "신호 조건 JSON (선택). 예: {\"rsi_oversold\": 40}",
                "default": {},
            },
            "reason": {
                "type": "string",
                "description": "편입 근거 (전략 노트 기록용)",
                "default": "",
            },
        },
        "required": ["stock_code", "stock_name"],
    }

    def execute(
        self,
        stock_code: str,
        stock_name: str,
        horizon: str = "중기",
        conditions: dict | None = None,
        reason: str = "",
    ) -> dict:
        conditions = conditions or {}
        try:
            from data.db import upsert_stock, get_watchlist
            existing = [s for s in get_watchlist() if s["code"] == stock_code]
            if existing:
                return {"ok": True, "already_exists": True, "stock_code": stock_code}
            payload = {"horizon": horizon, **conditions}
            upsert_stock(stock_code, stock_name, enabled=True, conditions=payload)
            logger.info(f"[Agent] watchlist 추가: {stock_name}({stock_code}) horizon={horizon} 근거={reason}")
            return {"ok": True, "stock_code": stock_code, "stock_name": stock_name}
        except Exception as e:
            return {"error": str(e)}
