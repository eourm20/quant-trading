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

from worker.kiwoom_client import KiwoomClient
from data.db import upsert_portfolio, upsert_trades, get_portfolio_updated_at

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
