"""
주문 실행 도구 — 자동/수동 모드 분기.
AUTO_TRADE=true  → 즉시 실행
AUTO_TRADE=false → 텔레그램 승인 대기 (5분 타임아웃)
"""

from __future__ import annotations
import logging
import os
import threading

logger = logging.getLogger(__name__)

AUTO_TRADE = os.getenv("AUTO_TRADE", "false").lower() == "true"

# 승인 이벤트 저장소: {stock_code: threading.Event}
_pending_approvals: dict[str, dict] = {}
_pending_lock = threading.Lock()


def register_approval_request(stock_code: str, order_type: str, quantity: int) -> threading.Event:
    event = threading.Event()
    with _pending_lock:
        _pending_approvals[stock_code] = {
            "event": event,
            "order_type": order_type,
            "quantity": quantity,
            "approved": False,
        }
    return event


def resolve_approval(stock_code: str, approved: bool) -> bool:
    """telegram_bot.py에서 호출 — 승인/거부 처리."""
    with _pending_lock:
        entry = _pending_approvals.get(stock_code)
        if not entry:
            return False
        entry["approved"] = approved
        entry["event"].set()
        return True


def get_pending_approvals() -> list[dict]:
    """현재 대기 중인 승인 목록 반환 (telegram_bot 전용)."""
    with _pending_lock:
        return [
            {"stock_code": k, "order_type": v["order_type"], "quantity": v["quantity"]}
            for k, v in _pending_approvals.items()
        ]


from worker.agents.tools.registry import BaseTool


class ExecuteOrderTool(BaseTool):
    name = "execute_order"
    label = "주문 실행"
    description = (
        "매수 또는 매도 주문을 실행합니다. "
        "자동 모드(AUTO_TRADE=true)이면 즉시 체결, "
        "수동 모드이면 텔레그램으로 승인 요청 후 실행합니다."
    )
    input_schema = {
        "properties": {
            "stock_code": {"type": "string", "description": "종목 코드"},
            "stock_name": {"type": "string", "description": "종목명"},
            "order_type": {
                "type": "string",
                "description": "'1' = 매수, '2' = 매도",
                "enum": ["1", "2"],
            },
            "quantity": {"type": "integer", "description": "주문 수량"},
            "price": {
                "type": "integer",
                "description": "주문 가격. 0 = 시장가(손절/긴급 매도 전용). 지정가 시 1 이상 입력하면 실제 현재가로 자동 강제됨.",
                "default": 0,
            },
            "order_market": {
                "type": "string",
                "description": "거래소: 'KRX' / 'NXT' / 'SOR' (생략 시 자동)",
                "default": "",
            },
            "reason": {
                "type": "string",
                "description": "매매 근거 (로그 및 텔레그램 표시용)",
                "default": "",
            },
        },
        "required": ["stock_code", "stock_name", "order_type", "quantity"],
    }

    def execute(
        self,
        stock_code: str,
        stock_name: str,
        order_type: str,
        quantity: int,
        price: int = 0,
        order_market: str = "",
        reason: str = "",
    ) -> dict:
        side = "매수" if order_type == "1" else "매도"

        if AUTO_TRADE:
            return self._place(stock_code, stock_name, order_type, quantity, price, order_market, side, reason)

        # 수동 모드 — 텔레그램 승인 대기
        try:
            from notifications.telegram import send_message_with_inline_buttons
            send_message_with_inline_buttons(
                f"🤖 *AI 주문 요청*\n"
                f"종목: *{stock_name}* ({stock_code})\n"
                f"주문: {side} {quantity}주\n"
                f"근거: {reason or '없음'}",
                buttons=[
                    [("✅ 승인", f"agent_approve:{stock_code}:{order_type}:{quantity}")],
                    [("❌ 취소", f"agent_reject:{stock_code}")],
                ],
            )
        except Exception as e:
            logger.warning(f"[ExecuteOrderTool] 텔레그램 승인 요청 실패: {e}")
            return {"error": f"텔레그램 승인 요청 실패: {e}"}

        event = register_approval_request(stock_code, order_type, quantity)
        approved = event.wait(timeout=300)  # 5분 대기

        with _pending_lock:
            entry = _pending_approvals.pop(stock_code, {})
        final_approved = approved and entry.get("approved", False)

        if not final_approved:
            logger.info(f"[ExecuteOrderTool] {stock_name} {side} — 거부 또는 타임아웃")
            return {"status": "rejected", "stock_code": stock_code}

        return self._place(stock_code, stock_name, order_type, quantity, price, order_market, side, reason)

    @staticmethod
    def _place(stock_code, stock_name, order_type, quantity, price, order_market, side, reason) -> dict:
        try:
            from worker.clients.kiwoom_client import KiwoomClient
            result = KiwoomClient().place_order(
                stock_code,
                order_type,
                quantity,
                price=price,
                order_market=order_market or None,
            )
            ord_no = result.get("ord_no") or result.get("odno")
            logger.info(f"[Agent] {side} 체결: {stock_name}({stock_code}) {quantity}주 — {reason} (주문번호: {ord_no})")
            payload = {
                "status": "executed",
                "stock_code": stock_code,
                "order_type": side,
                "quantity": quantity,
                "order_no": ord_no,
            }
            ExecuteOrderTool._run_post_trade_pipeline(
                stock_code=stock_code,
                stock_name=stock_name,
                order_type=order_type,
                quantity=quantity,
                price=price,
                reason=reason,
                order_no=ord_no,
                result_payload=payload,
            )
            return payload
        except Exception as e:
            logger.error(f"[Agent] 주문 실패: {stock_name} {side} — {e}")
            return {"status": "error", "error": str(e)}

    @staticmethod
    def _run_post_trade_pipeline(
        *,
        stock_code: str,
        stock_name: str,
        order_type: str,
        quantity: int,
        price: int,
        reason: str,
        order_no: str | None,
        result_payload: dict,
    ) -> None:
        """Enforce mandatory post-order workflow in code (not prompt rules)."""
        pipeline_steps: list[str] = []

        try:
            from worker.clients.kiwoom_client import KiwoomClient
            from worker.portfolio_sync import sync_all
            sync_all(KiwoomClient())
            pipeline_steps.append("portfolio_sync")
        except Exception as e:
            logger.warning(f"[Agent] post-trade portfolio_sync 실패: {e}")

        try:
            from data.db import reset_cooldowns_for_stock, set_add_cooldown_after_trade
            cnt = reset_cooldowns_for_stock(stock_code)
            pipeline_steps.append(f"cooldown_reset:{cnt}")
            if order_type == "1":
                add_cnt = set_add_cooldown_after_trade(stock_code, suppress_minutes=60)
                pipeline_steps.append(f"add_cooldown:{add_cnt}")
        except Exception as e:
            logger.warning(f"[Agent] post-trade cooldown 처리 실패: {e}")

        if order_type == "1":
            try:
                from data.db import create_position_from_trade, get_position, update_position_field
                created = create_position_from_trade(stock_code, stock_name, price or 0, quantity)
                pipeline_steps.append(f"position_create:{'created' if created else 'exists'}")

                try:
                    from worker.claude_judge import judge_position_values
                    pos = get_position(stock_code) or {}
                    avg_price = int(pos.get("avg_price") or 0)
                    current_price = int(price or avg_price or 0)
                    ai = judge_position_values(stock_code, stock_name, avg_price, quantity, current_price=current_price)
                    if ai:
                        for k in ("target_price", "stop_loss_price", "add_buy_price"):
                            v = ai.get(k)
                            if v:
                                update_position_field(stock_code, k, v)
                        pipeline_steps.append("position_ai_update")
                        result_payload["position_ai"] = {
                            "target_price": ai.get("target_price", 0),
                            "stop_loss_price": ai.get("stop_loss_price", 0),
                            "add_buy_price": ai.get("add_buy_price", 0),
                        }
                except Exception as e:
                    logger.warning(f"[Agent] post-trade AI position 설정 실패: {e}")
            except Exception as e:
                logger.warning(f"[Agent] post-trade position 생성 실패: {e}")

        try:
            from data.db import save_strategy_note
            side = "매수" if order_type == "1" else "매도"
            save_strategy_note(
                "trade",
                f"{stock_name} {quantity}주 {side} (agent pipeline)",
                (
                    f"order_no={order_no or '-'}\n"
                    f"reason={reason or '-'}\n"
                    f"steps={' -> '.join(pipeline_steps) if pipeline_steps else '-'}"
                ),
            )
            pipeline_steps.append("strategy_log")
        except Exception as e:
            logger.warning(f"[Agent] post-trade strategy_note 저장 실패: {e}")

        result_payload["post_trade_pipeline"] = pipeline_steps
