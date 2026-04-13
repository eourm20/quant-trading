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
from data.db import init_db, upsert_trades, upsert_trades_from_executions


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


def _sql_str(v: str) -> str:
    return "'" + str(v or "").replace("'", "''") + "'"


def _to_int(v) -> int:
    try:
        s = str(v or "").replace(",", "").strip()
        return int(float(s)) if s else 0
    except Exception:
        return 0


def _print_sql_from_trade_rows(rows: list[dict]) -> int:
    printed = 0
    for t in rows or []:
        trade_id = str(t.get("trde_no") or t.get("ord_no") or "").strip()
        code = str(t.get("stk_cd") or "").replace("A", "").strip()
        if not trade_id or not code:
            continue
        executed_at = str(t.get("trde_dt") or "").strip()
        if len(executed_at) == 8 and executed_at.isdigit():
            executed_at = f"{executed_at[:4]}-{executed_at[4:6]}-{executed_at[6:]}"

        io_tp = str(t.get("io_tp") or "")
        side = "매수" if io_tp == "2" else "매도"
        qty = _to_int(t.get("trde_qty_jwa_cnt"))
        price = _to_int(t.get("trde_unit"))
        amount = _to_int(t.get("trde_amt"))
        fee = _to_int(t.get("fee"))
        tax = _to_int(t.get("tax"))
        stock_name = str(t.get("stk_nm") or "").strip()

        if qty <= 0:
            continue
        # 수동 보정 목적: 가격/금액이 0인 값은 출력해도 의미가 적어 제외
        if price <= 0 and amount <= 0 and fee <= 0 and tax <= 0:
            continue

        print(
            "UPDATE trades SET "
            f"executed_at={_sql_str(executed_at)}, "
            f"stock_code={_sql_str(code)}, "
            f"stock_name={_sql_str(stock_name)}, "
            f"side={_sql_str(side)}, "
            f"quantity={qty}, "
            f"price={price}, "
            f"amount={amount}, "
            f"fee={fee}, "
            f"tax={tax} "
            f"WHERE trade_id={_sql_str(trade_id)};"
        )
        printed += 1
    return printed


def run(start: str, end: str, print_sql: bool = False) -> dict:
    client = KiwoomClient()
    init_db()

    start_ymd = start.replace("-", "")
    end_ymd = end.replace("-", "")

    total_exec = 0
    total_trades_rows = 0
    total_upsert = 0
    total_printed_sql = 0
    total_matched = 0
    total_skipped_duplicates = 0
    days = 0
    seen_order_no_dates: dict[str, str] = {}

    for ymd in _iter_dates(start_ymd, end_ymd):
        days += 1
        try:
            # 1) 주문/매매내역(kt00015) 우선 반영
            trade_rows = []
            try:
                trade_rows = client.get_trade_history_range(ymd, ymd)
            except Exception as e:
                logging.warning("[백필] %s kt00015 조회 실패: %s", ymd, e)
            if trade_rows:
                upsert_trades(trade_rows)
                total_trades_rows += len(trade_rows)
                if print_sql:
                    total_printed_sql += _print_sql_from_trade_rows(trade_rows)

            # 2) 체결상세(kt00007)로 추가 보정
            executions = client.get_executions(trade_date=ymd)
            order_nos_all = [str(e.get("order_no") or "").strip() for e in executions]
            # 날짜 필터 무시 대응: 과거 날짜에서 이미 본 주문번호는 스킵
            filtered_exec = []
            for e in executions:
                no = str(e.get("order_no") or "").strip()
                if no:
                    prev = seen_order_no_dates.get(no)
                    if prev and prev != ymd:
                        total_skipped_duplicates += 1
                        logging.warning(
                            "[백필] 주문번호 %s 가 날짜 %s/%s에 중복 발견 → 스킵 (kt00007 날짜필터 무시 가능성)",
                            no, prev, ymd
                        )
                        continue
                    seen_order_no_dates.setdefault(no, ymd)
                filtered_exec.append(e)

            exec_cnt = len(filtered_exec)
            up_cnt = upsert_trades_from_executions(filtered_exec, executed_at=ymd)
            order_nos = sorted({str(e.get("order_no") or "").strip() for e in filtered_exec if str(e.get("order_no") or "").strip()})
            matched = _count_matched_order_nos(order_nos)
            total_matched += matched
            total_exec += exec_cnt
            total_upsert += up_cnt
            logging.info(
                "[백필] %s trades=%s executions=%s(%s원본) upserted=%s matched_order_no=%s",
                ymd, len(trade_rows), exec_cnt, len(executions), up_cnt, matched
            )
            time.sleep(0.5)
        except Exception as e:
            logging.warning("[백필] %s 실패: %s", ymd, e)

    return {
        "days": days,
        "trade_rows": total_trades_rows,
        "executions": total_exec,
        "upserted": total_upsert,
        "printed_sql": total_printed_sql,
        "matched_order_no": total_matched,
        "skipped_duplicates": total_skipped_duplicates,
        "start": start_ymd,
        "end": end_ymd,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="모의투자 kt00007 체결내역 백필")
    parser.add_argument("--start", required=True, help="시작일 (YYYY-MM-DD 또는 YYYYMMDD)")
    parser.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"), help="종료일 (기본: 오늘)")
    parser.add_argument("--print-sql", action="store_true", help="수동 반영용 UPDATE SQL을 함께 출력")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    result = run(args.start, args.end, print_sql=args.print_sql)
    print(
        f"[완료] {result['start']}~{result['end']} "
        f"{result['days']}일 | 매매행 {result['trade_rows']}건 | 체결행 {result['executions']}건 | 저장/갱신 {result['upserted']}건 "
        f"| 주문번호 매칭 {result['matched_order_no']}건 | 중복스킵 {result['skipped_duplicates']}건 | SQL출력 {result['printed_sql']}건"
    )
