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
    description = "현재 보유 종목 목록, 평단가, 평가손익, 수익률 등을 조회합니다."
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
    description = "주문 가능 예수금 및 주문가능금액을 조회합니다."
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
        "exit 신호: 손절가·목표가 도달 여부 확인 필수. "
        "add 신호: 추가매수가 수준 및 물타기 1회 원칙 위반 여부 확인 필수."
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
        "exit/add 신호 처리 시 이미 매도됐는지 확인하거나, "
        "이전 주문이 체결됐는지 vs 아직 미체결로 남아 있는지 검증할 때 사용하세요. "
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
