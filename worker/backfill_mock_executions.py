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


def _sql_str(v: str) -> str:
    return "'" + str(v or "").replace("'", "''") + "'"


def _to_int(v) -> int:
    try:
        s = str(v or "").replace(",", "").strip()
        return int(float(s)) if s else 0
    except Exception:
        return 0


def _norm_ymd(v: str) -> str:
    s = str(v or "").replace("-", "").strip()
    return s if len(s) == 8 and s.isdigit() else ""


def _exec_fingerprint(e: dict, query_ymd: str) -> tuple[str, str, str, int, int, str]:
    order_no = str(e.get("order_no") or "").strip()
    code = str(e.get("stock_code") or "").replace("A", "").strip()
    side = str(e.get("side") or "").strip() or "매수"
    qty = _to_int(e.get("quantity"))
    price = _to_int(e.get("price"))
    exec_ymd = _norm_ymd(str(e.get("executed_date") or e.get("executed_at") or "")) or query_ymd
    time_str = str(e.get("time") or "").strip()
    return (order_no, code, side, qty, price, f"{exec_ymd}-{time_str}")


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

        print(
            "INSERT INTO trades (trade_id, executed_at, stock_code, stock_name, side, quantity, price, amount, fee, tax) "
            f"VALUES ({_sql_str(trade_id)}, {_sql_str(executed_at)}, {_sql_str(code)}, {_sql_str(stock_name)}, {_sql_str(side)}, {qty}, {price}, {amount}, {fee}, {tax}) "
            "ON CONFLICT(trade_id) DO UPDATE SET "
            "executed_at=excluded.executed_at, stock_code=excluded.stock_code, stock_name=excluded.stock_name, side=excluded.side, "
            "quantity=excluded.quantity, price=excluded.price, amount=excluded.amount, fee=excluded.fee, tax=excluded.tax;"
        )
        printed += 1
    return printed


def _print_sql_from_execution_rows(
    rows: list[dict],
    ymd: str,
    note: str = "",
    fixed_executed_day: str | None = None,
    allow_query_date_fallback: bool = True,
) -> int:
    printed = 0
    fallback_idx = 0
    default_day = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}" if len(ymd) == 8 and ymd.isdigit() else ymd
    executed_day = fixed_executed_day if fixed_executed_day else default_day
    for e in rows or []:
        code = str(e.get("stock_code") or "").replace("A", "").strip()
        stock_name = str(e.get("stock_name") or "").strip()
        side = str(e.get("side") or "").strip() or "매수"
        qty = _to_int(e.get("quantity"))
        price = _to_int(e.get("price"))
        order_no = str(e.get("order_no") or "").strip()
        raw_exec_date = str(e.get("executed_date") or e.get("executed_at") or "").replace("-", "").strip()
        if len(raw_exec_date) == 8 and raw_exec_date.isdigit():
            row_exec_day = f"{raw_exec_date[:4]}-{raw_exec_date[4:6]}-{raw_exec_date[6:]}"
        elif executed_day and allow_query_date_fallback:
            row_exec_day = executed_day
        else:
            if note:
                print(f"-- {note}")
            print("-- skip_sql_no_executed_date")
            continue
        if not code:
            continue
        if not order_no:
            fallback_idx += 1
            order_no = f"EXE-{ymd}-{code}-{side}-{fallback_idx}"
        amount = qty * price
        if note:
            print(f"-- {note}")
        print(
            "INSERT INTO trades (trade_id, executed_at, stock_code, stock_name, side, quantity, price, amount, fee, tax) "
            f"VALUES ({_sql_str(order_no)}, {_sql_str(row_exec_day)}, {_sql_str(code)}, {_sql_str(stock_name)}, {_sql_str(side)}, {qty}, {price}, {amount}, 0, 0) "
            "ON CONFLICT(trade_id) DO UPDATE SET "
            "executed_at=trades.executed_at, stock_code=excluded.stock_code, stock_name=excluded.stock_name, side=excluded.side, "
            "quantity=excluded.quantity, price=excluded.price, amount=excluded.amount;"
        )
        printed += 1
    return printed


def run(start: str, end: str, print_sql: bool = False, no_dedupe: bool = False) -> dict:
    client = KiwoomClient()
    init_db()
    db_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data", "trading.db"))
    logging.info("[백필] 대상 DB: %s", db_path)
    if no_dedupe:
        logging.warning("[백필] 중복 제거 비활성화(--no-dedupe): 동일 체결이 반복 저장/출력될 수 있습니다.")

    start_ymd = start.replace("-", "")
    end_ymd = end.replace("-", "")

    total_exec = 0
    total_upsert = 0
    total_printed_sql = 0
    total_matched = 0
    total_skipped_duplicates = 0
    total_skipped_date_mismatch = 0
    total_skipped_no_exec_date = 0
    days = 0
    seen_order_no_dates: dict[str, str] = {}
    seen_exec_fingerprints: set[tuple[str, str, str, int, int, str]] = set()

    for ymd in _iter_dates(start_ymd, end_ymd):
        days += 1
        try:
            # kt00007 전용 백필
            # 하위호환: 구버전 KiwoomClient에는 fill_missing_date 인자가 없을 수 있음
            try:
                executions = client.get_executions(trade_date=ymd, fill_missing_date=True)
            except TypeError:
                executions = client.get_executions(trade_date=ymd)
            # 날짜 필터 무시 대응: 과거 날짜에서 이미 본 주문번호는 스킵
            filtered_exec = []
            skipped_exec = []
            skipped_date_exec = []
            for e in executions:
                exec_ymd = _norm_ymd(str(e.get("executed_date") or e.get("executed_at") or ""))
                if not exec_ymd:
                    total_skipped_no_exec_date += 1
                    skipped_date_exec.append((e, "no_executed_date"))
                    continue
                if exec_ymd != ymd:
                    total_skipped_date_mismatch += 1
                    skipped_date_exec.append((e, f"date_mismatch exec={exec_ymd} query={ymd}"))
                    continue

                no = str(e.get("order_no") or "").strip()
                if not no_dedupe:
                    fp = _exec_fingerprint(e, ymd)
                    if fp in seen_exec_fingerprints:
                        total_skipped_duplicates += 1
                        skipped_exec.append(e)
                        prev = seen_order_no_dates.get(no, ymd) if no else ymd
                        logging.warning(
                            "[백필] 중복 체결행 스킵 order_no=%s prev=%s query=%s fp=%s",
                            no or "-", prev, ymd, fp[-1]
                        )
                        continue
                    seen_exec_fingerprints.add(fp)

                if no:
                    prev = seen_order_no_dates.get(no)
                    seen_order_no_dates.setdefault(no, ymd)
                filtered_exec.append(e)

            if print_sql and executions:
                print(f"-- {ymd} kt00007 rows={len(executions)} filtered={len(filtered_exec)}")
                total_printed_sql += _print_sql_from_execution_rows(filtered_exec, ymd)
                for se, reason in skipped_date_exec:
                    print(f"-- skip_by_date_filter reason={reason}")
                    total_printed_sql += _print_sql_from_execution_rows(
                        [se],
                        ymd,
                        note=f"skip_by_date_filter {reason}",
                        allow_query_date_fallback=False,
                    )
                if skipped_exec:
                    print(f"-- {ymd} kt00007 skipped_duplicates={len(skipped_exec)}")
                    for se in skipped_exec:
                        no = str(se.get("order_no") or "").strip()
                        first_ymd = seen_order_no_dates.get(no, ymd)
                        first_day = f"{first_ymd[:4]}-{first_ymd[4:6]}-{first_ymd[6:]}" if len(first_ymd) == 8 and first_ymd.isdigit() else first_ymd
                        total_printed_sql += _print_sql_from_execution_rows(
                            [se],
                            ymd,
                            note=f"duplicate_skipped_by_order_no query_date={ymd} first_seen={first_ymd}",
                            fixed_executed_day=first_day,
                        )

            exec_cnt = len(filtered_exec)
            up_cnt = upsert_trades_from_executions(filtered_exec, executed_at=ymd)
            order_nos = sorted({str(e.get("order_no") or "").strip() for e in filtered_exec if str(e.get("order_no") or "").strip()})
            matched = _count_matched_order_nos(order_nos)
            total_matched += matched
            total_exec += exec_cnt
            total_upsert += up_cnt
            logging.info(
                "[백필] %s executions=%s(%s원본) upserted=%s matched_order_no=%s skip_date_mismatch=%s skip_no_date=%s",
                ymd, exec_cnt, len(executions), up_cnt, matched,
                total_skipped_date_mismatch, total_skipped_no_exec_date
            )
            time.sleep(0.5)
        except Exception as e:
            logging.warning("[백필] %s 실패: %s", ymd, e)

    return {
        "days": days,
        "executions": total_exec,
        "upserted": total_upsert,
        "printed_sql": total_printed_sql,
        "matched_order_no": total_matched,
        "skipped_duplicates": total_skipped_duplicates,
        "skipped_date_mismatch": total_skipped_date_mismatch,
        "skipped_no_exec_date": total_skipped_no_exec_date,
        "start": start_ymd,
        "end": end_ymd,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="모의투자 kt00007 체결내역 백필 (kt00007 only)")
    parser.add_argument("--start", required=True, help="시작일 (YYYY-MM-DD 또는 YYYYMMDD)")
    parser.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"), help="종료일 (기본: 오늘)")
    parser.add_argument("--print-sql", action="store_true", help="수동 반영용 UPDATE SQL을 함께 출력")
    parser.add_argument("--no-dedupe", action="store_true", help="중복 체결 제거를 하지 않음")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    result = run(args.start, args.end, print_sql=args.print_sql, no_dedupe=args.no_dedupe)
    print(
        f"[완료] {result['start']}~{result['end']} "
        f"{result['days']}일 | 체결행 {result['executions']}건 | 저장/갱신 {result['upserted']}건 "
        f"| 주문번호 매칭 {result['matched_order_no']}건 | 중복스킵 {result['skipped_duplicates']}건 "
        f"| 날짜불일치스킵 {result['skipped_date_mismatch']}건 | 무체결일스킵 {result['skipped_no_exec_date']}건 "
        f"| SQL출력 {result['printed_sql']}건"
    )
