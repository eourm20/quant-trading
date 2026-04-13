"""
백필 실행 스크립트 (scripts 엔트리포인트).

실제 로직은 worker/backfill_mock_executions.py를 사용한다.
"""

from __future__ import annotations

import argparse
from datetime import datetime

from worker.backfill_mock_executions import run


def main() -> None:
    parser = argparse.ArgumentParser(description="모의투자 kt00007 체결내역 백필 (scripts entrypoint)")
    parser.add_argument("--start", required=True, help="시작일 (YYYY-MM-DD 또는 YYYYMMDD)")
    parser.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"), help="종료일 (기본: 오늘)")
    parser.add_argument("--print-sql", action="store_true", help="수동 반영용 UPDATE SQL을 함께 출력")
    parser.add_argument("--no-dedupe", action="store_true", help="중복 체결 제거를 하지 않음")
    args = parser.parse_args()

    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    result = run(args.start, args.end, print_sql=args.print_sql, no_dedupe=args.no_dedupe)
    print(
        f"[완료] {result['start']}~{result['end']} "
        f"{result['days']}일 | 체결행 {result['executions']}건 | 저장/갱신 {result['upserted']}건 "
        f"| 주문번호 매칭 {result['matched_order_no']}건 | 중복스킵 {result['skipped_duplicates']}건 "
        f"| 날짜불일치스킵 {result['skipped_date_mismatch']}건 | 무체결일스킵 {result['skipped_no_exec_date']}건 "
        f"| SQL출력 {result['printed_sql']}건"
    )


if __name__ == "__main__":
    main()
