"""
모의투자 체결내역(kt00007) 백필 스크립트.

사용 예:
  .venv/Scripts/python worker/backfill_mock_executions.py --start 2026-03-31
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta
import sqlite3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

from worker.clients.kiwoom_client import KiwoomClient
from data.db import init_db, upsert_trades_from_executions


def _iter_dates(start_ymd: str, end_ymd: str):
    d = datetime.strptime(start_ymd, "%Y%m%d").date()
    e = datetime.strptime(end_ymd, "%Y%m%d").date()
    while d <= e:
        yield d.strftime("%Y%m%d")
        d += timedelta(days=1)


def _count_matched_order_nos(order_nos: list[str]) -> int:
    if not order_nos:
        return 0
    conn = sqlite3.connect(os.path.join(os.path.dirname(__file__), "..", "data", "trading.db"))
    try:
        placeholders = ",".join("?" * len(order_nos))
        row = conn.execute(
            f"SELECT COUNT(*) FROM trades WHERE trade_id IN ({placeholders}) AND price > 0",
            order_nos,
        ).fetchone()
        return int(row[0] or 0) if row else 0
    finally:
        conn.close()


def run(start: str, end: str) -> dict:
    client = KiwoomClient()
    init_db()

    start_ymd = start.replace("-", "")
    end_ymd = end.replace("-", "")

    total_exec = 0
    total_upsert = 0
    total_matched = 0
    days = 0
    seen_order_no_dates: dict[str, str] = {}

    for ymd in _iter_dates(start_ymd, end_ymd):
        days += 1
        try:
            executions = client.get_executions(trade_date=ymd)
            exec_cnt = len(executions)
            up_cnt = upsert_trades_from_executions(executions, executed_at=ymd)
            order_nos = sorted({str(e.get("order_no") or "").strip() for e in executions if str(e.get("order_no") or "").strip()})
            matched = _count_matched_order_nos(order_nos)
            total_matched += matched
            # 동일 주문번호가 여러 날짜에서 반복되면 날짜 필터가 무시됐을 가능성이 큼
            for no in order_nos:
                prev = seen_order_no_dates.get(no)
                if prev and prev != ymd:
                    logging.warning("[백필] 주문번호 %s 가 날짜 %s/%s에 중복 발견 (kt00007 날짜필터 무시 가능성)", no, prev, ymd)
                else:
                    seen_order_no_dates[no] = ymd
            total_exec += exec_cnt
            total_upsert += up_cnt
            logging.info(
                "[백필] %s executions=%s upserted=%s matched_order_no=%s",
                ymd, exec_cnt, up_cnt, matched
            )
            time.sleep(0.5)
        except Exception as e:
            logging.warning("[백필] %s 실패: %s", ymd, e)

    return {
        "days": days,
        "executions": total_exec,
        "upserted": total_upsert,
        "matched_order_no": total_matched,
        "start": start_ymd,
        "end": end_ymd,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="모의투자 kt00007 체결내역 백필")
    parser.add_argument("--start", required=True, help="시작일 (YYYY-MM-DD 또는 YYYYMMDD)")
    parser.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"), help="종료일 (기본: 오늘)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    result = run(args.start, args.end)
    print(
        f"[완료] {result['start']}~{result['end']} "
        f"{result['days']}일 | 체결행 {result['executions']}건 | 저장/갱신 {result['upserted']}건 "
        f"| 주문번호 매칭 {result['matched_order_no']}건"
    )
