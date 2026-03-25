"""
포트폴리오 & 매매 내역 동기화
- 자동: 장 시작(09:01), 장 마감(15:36)
- 수동: python worker/portfolio_sync.py
"""

import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

from worker.clients.kiwoom_client import KiwoomClient
from data.db import (
    upsert_portfolio, upsert_trades, get_portfolio_updated_at,
    get_positions, get_position, create_position_from_trade,
    update_position_field, delete_position,
)

logger = logging.getLogger(__name__)


def sync_all(client: KiwoomClient | None = None) -> dict:
    """포트폴리오 + 매매 내역 동기화. 결과 요약 반환."""
    if client is None:
        client = KiwoomClient()

    result = {"portfolio": 0, "trades": 0, "errors": []}

    # 포트폴리오
    try:
        holdings = client.get_holdings()
        upsert_portfolio(holdings)
        result["portfolio"] = len(holdings)
        logger.info(f"포트폴리오 동기화 완료: {len(holdings)}개 종목")
    except Exception as e:
        result["errors"].append(f"포트폴리오: {e}")
        logger.error(f"포트폴리오 동기화 실패: {e}")

    # 매매 내역
    try:
        trades = client.get_trade_history(days=30)
        upsert_trades(trades)
        result["trades"] = len(trades)
        logger.info(f"매매 내역 동기화 완료: {len(trades)}건")
    except Exception as e:
        result["errors"].append(f"매매 내역: {e}")
        logger.error(f"매매 내역 동기화 실패: {e}")

    # ── positions 동기화 ──
    result["positions_created"] = 0
    result["positions_removed"] = 0
    try:
        existing_positions = {p["stock_code"]: p for p in get_positions()}
        current_holdings = {}

        for h in holdings:
            code = str(h.get("stk_cd") or "").replace("A", "").strip()
            if not code:
                continue
            current_holdings[code] = h

            if code not in existing_positions:
                # 신규 보유종목 → 포지션 자동 생성
                avg_price = abs(int(float(str(h.get("pur_pric", 0)).replace(",", "").strip() or "0")))
                qty = abs(int(float(str(h.get("rmnd_qty", 0)).replace(",", "").strip() or "0")))
                created = create_position_from_trade(code, h.get("stk_nm", ""), avg_price, qty)
                if created:
                    result["positions_created"] += 1
                    logger.info(f"포지션 자동 생성: {h.get('stk_nm', '')} ({code})")
            else:
                # 기존 포지션 — 평단가/수량 갱신
                pos = existing_positions[code]
                new_avg = abs(int(float(str(h.get("pur_pric", 0)).replace(",", "").strip() or "0")))
                new_qty = abs(int(float(str(h.get("rmnd_qty", 0)).replace(",", "").strip() or "0")))
                if pos["avg_price"] != new_avg and new_avg > 0:
                    update_position_field(code, "avg_price", new_avg)
                if pos["quantity"] != new_qty and new_qty > 0:
                    update_position_field(code, "quantity", new_qty)

        # 미보유 종목 포지션 삭제
        for old_code in set(existing_positions) - set(current_holdings):
            delete_position(old_code)
            result["positions_removed"] += 1
            logger.info(f"포지션 삭제 (미보유): {existing_positions[old_code].get('stock_name', '')} ({old_code})")
    except Exception as e:
        result["errors"].append(f"포지션 동기화: {e}")
        logger.error(f"포지션 동기화 실패: {e}")

    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    result = sync_all()
    print(f"\n동기화 완료")
    print(f"  포트폴리오: {result['portfolio']}개 종목")
    print(f"  매매 내역: {result['trades']}건")
    if result["errors"]:
        print(f"  오류: {result['errors']}")
    updated_at = get_portfolio_updated_at()
    print(f"  마지막 업데이트: {updated_at}")
