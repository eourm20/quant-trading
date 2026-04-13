"""
포트폴리오/잔고 도구: 보유 현황, 예수금, 포지션 관리 정보.
"""

from __future__ import annotations
import logging
from worker.agents.tools.registry import BaseTool

logger = logging.getLogger(__name__)


def _kiwoom():
    from worker.clients.kiwoom_client import KiwoomClient
    return KiwoomClient()


def _p(v) -> int:
    try:
        return int(str(v or "0").replace(",", "").lstrip())
    except Exception:
        return 0


def _f(v) -> float:
    try:
        return float(str(v or "0").replace(",", ""))
    except Exception:
        return 0.0


class GetPortfolioTool(BaseTool):
    name = "get_portfolio"
    label = "보유 현황 확인"
    description = (
        "현재 보유 종목 목록, 평단가, 평가손익, 수익률 등을 조회합니다. "
        "entry/add 판단에서 이미 들고 있는 종목인지, 포트 집중도가 과한지, 신규 매수 여지가 있는지 확인할 때 먼저 유용합니다. "
        "Research Agent도 기존 보유 종목과 중복 편입을 피하고 전체 익스포저를 점검할 때 사용하세요."
    )
    input_schema = {
        "properties": {},
        "required": [],
    }

    def execute(self) -> dict:
        try:
            holdings = _kiwoom().get_holdings()
            result = []
            for h in holdings:
                code = str(h.get("stk_cd") or h.get("stock_code", "")).strip()
                name = str(h.get("stk_nm") or h.get("stock_name", "")).strip()
                qty = abs(_p(h.get("rmn_qty") or h.get("quantity")))
                avg = abs(_p(h.get("pchs_avg_pric") or h.get("avg_price")))
                cur = abs(_p(h.get("cur_prc") or h.get("current_price")))
                profit_rate = _f(h.get("evlt_pfls_rt") or h.get("profit_rate"))
                eval_amount = abs(_p(h.get("evlt_amt") or h.get("eval_amount")))
                result.append({
                    "stock_code": code,
                    "stock_name": name,
                    "quantity": qty,
                    "avg_price": avg,
                    "current_price": cur,
                    "profit_rate": profit_rate,
                    "eval_amount": eval_amount,
                })
            return {"holdings": result, "count": len(result)}
        except Exception as e:
            return {"error": str(e)}


class GetDepositTool(BaseTool):
    name = "get_deposit"
    label = "예수금 조회"
    description = (
        "주문 가능 예수금 및 주문가능금액을 조회합니다. "
        "신규 매수나 추가매수 수량을 제안하기 전 현금 여력을 확인할 때 사용하세요. "
        "현금이 부족하면 무리하게 매수 결론을 내리지 말고 보수적으로 판단하세요."
    )
    input_schema = {
        "properties": {},
        "required": [],
    }

    def execute(self) -> dict:
        try:
            return _kiwoom().get_deposit()
        except Exception as e:
            return {"error": str(e)}


class GetPositionsTool(BaseTool):
    name = "get_positions"
    label = "포지션 정보 조회"
    description = (
        "보유 종목의 포지션 관리 정보(목표가, 손절가, 추가매수가, 물타기 여부 등)를 조회합니다. "
        "exit 신호에서는 목표가/손절가 판단의 기준점이므로 사실상 우선 확인 도구입니다. "
        "add 신호에서는 추가매수 가격대와 물타기 1회 원칙 위반 여부를 확인할 때 중요합니다. "
        "포지션 정보 없이 exit/add를 단정하지 마세요."
    )
    input_schema = {
        "properties": {
            "stock_code": {
                "type": "string",
                "description": "특정 종목만 조회할 경우 종목 코드. 생략하면 전체 반환.",
            },
        },
        "required": [],
    }

    def execute(self, stock_code: str | None = None) -> dict:
        try:
            from data.db import get_positions, get_position
            if stock_code:
                pos = get_position(stock_code)
                return {"position": pos}
            return {"positions": get_positions()}
        except Exception as e:
            return {"error": str(e)}


class GetOrderStatusTool(BaseTool):
    name = "get_order_status"
    label = "당일 주문 현황 조회"
    description = (
        "당일 체결 내역과 미체결 주문을 Kiwoom에서 직접 조회합니다. "
        "exit/add 신호 처리 시 이미 같은 방향 주문이 나갔는지, 이미 매도됐는지, 미체결 주문이 남아 있는지 확인할 때 사용하세요. "
        "중복 주문이나 이미 끝난 포지션에 대한 잘못된 판단을 막는 안전장치 역할입니다. "
        "stock_code를 지정하면 해당 종목만 필터링합니다."
    )
    input_schema = {
        "properties": {
            "stock_code": {
                "type": "string",
                "description": "특정 종목만 조회할 경우 종목 코드. 생략하면 전체 반환.",
                "default": "",
            },
        },
        "required": [],
    }

    def execute(self, stock_code: str = "") -> dict:
        try:
            kw = _kiwoom()
            executions = kw.get_executions(stock_code=stock_code)
            pending = kw.get_pending_orders(stock_code=stock_code)
            return {
                "executions": executions,
                "executions_count": len(executions),
                "pending_orders": pending,
                "pending_count": len(pending),
            }
        except Exception as e:
            return {"error": str(e)}


class GetRealizedPnlTool(BaseTool):
    name = "get_realized_pnl"
    label = "실현손익 조회"
    description = (
        "실현손익을 조회합니다. scope=today는 당일 실현손익, scope=period는 기간 실현손익, "
        "scope=both는 둘 다 반환합니다. 워커 자동 스케줄이 아닌 필요 시점 수동 조회용입니다."
    )
    input_schema = {
        "properties": {
            "scope": {
                "type": "string",
                "enum": ["today", "period", "both"],
                "default": "both",
                "description": "조회 범위",
            },
            "days": {
                "type": "integer",
                "default": 30,
                "description": "기간 조회 일수(scope=period/both에서 사용)",
            },
        },
        "required": [],
    }

    def execute(self, scope: str = "both", days: int = 30) -> dict:
        try:
            kw = _kiwoom()
            result: dict = {"scope": scope}
            if scope in ("today", "both"):
                result["today"] = kw.get_realized_pnl_today()
            if scope in ("period", "both"):
                result["period"] = kw.get_realized_pnl_period(days=max(1, int(days)))
            return result
        except Exception as e:
            return {"error": str(e)}
