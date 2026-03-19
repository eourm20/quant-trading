"""
신호/포트폴리오/전략 리포트 조회
claude.ai에서 호출하거나 직접 실행 가능

사용법:
  python worker/report.py signals          # 오늘 신호
  python worker/report.py signals --days 3 # 최근 3일 신호
  python worker/report.py portfolio        # 현재 포트폴리오
  python worker/report.py trades           # 최근 매매 내역
  python worker/report.py strategy         # 전략 노트
  python worker/report.py all              # 전체 요약
"""

import argparse
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

from data.db import (
    get_conn,
    get_portfolio,
    get_portfolio_updated_at,
    get_trades,
    get_strategy_notes,
)


def report_signals(days: int = 1) -> str:
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM signals WHERE created_at >= ? ORDER BY created_at DESC",
            (since,),
        ).fetchall()

    if not rows:
        period = "오늘" if days == 1 else f"최근 {days}일"
        return f"📭 {period} 발생한 신호 없음"

    lines = [f"📊 신호 내역 (최근 {days}일, 총 {len(rows)}건)\n"]
    for r in rows:
        r = dict(r)
        dt = r["created_at"][5:]  # MM-DD HH:MM
        opinion = ""
        if r.get("claude_opinion"):
            for tag in ["[매수]", "[매도]", "[홀드]"]:
                if tag in r["claude_opinion"]:
                    opinion = f" → AI {tag}"
                    break
        lines.append(
            f"• {dt} {r['stock_name']} {int(r['current_price']):,}원\n"
            f"  {r['triggered_conditions']}{opinion}"
        )
    return "\n".join(lines)


def report_portfolio() -> str:
    holdings = get_portfolio()
    updated_at = get_portfolio_updated_at()

    if not holdings:
        return "📭 포트폴리오 없음 (동기화 필요: python worker/portfolio_sync.py)"

    total_eval = sum(h["eval_amount"] for h in holdings)
    total_profit = sum(h["profit_loss"] for h in holdings)
    total_cost = total_eval - total_profit
    total_rate = total_profit / total_cost * 100 if total_cost else 0

    lines = [f"💼 포트폴리오 현황 (기준: {updated_at})\n"]
    for h in holdings:
        rate = h["profit_rate"]
        sign = "▲" if rate >= 0 else "▼"
        lines.append(
            f"• {h['stock_name']}: {h['quantity']}주 | "
            f"평단 {h['avg_price']:,}원 | 현재 {h['current_price']:,}원 | "
            f"{sign}{abs(rate):.2f}%"
        )
    lines.append(
        f"\n▶ 합계: 평가 {total_eval:,}원 | "
        f"손익 {total_profit:+,}원 ({total_rate:+.1f}%)"
    )
    return "\n".join(lines)


def report_trades(limit: int = 20) -> str:
    trades = get_trades(limit=limit)

    if not trades:
        return "📭 매매 내역 없음"

    lines = [f"🔄 최근 매매 내역 ({len(trades)}건)\n"]
    for t in trades:
        lines.append(
            f"• {t['executed_at']} {t['side']} {t['stock_name']} "
            f"{t['quantity']}주 @ {t['price']:,}원"
        )
    return "\n".join(lines)


def report_strategy(limit: int = 10) -> str:
    notes = get_strategy_notes(limit=limit)

    if not notes:
        return "📭 전략 노트 없음"

    emoji_map = {"trade": "💼", "watchlist": "⚙️", "general": "📋"}
    lines = [f"📋 전략 노트 (최근 {len(notes)}건)\n"]
    for n in notes:
        emoji = emoji_map.get(n["category"], "📌")
        dt = n["created_at"][5:16]
        lines.append(f"{emoji} [{dt}] #{n['id']} {n['summary']}")
        if n.get("detail"):
            lines.append(f"   └ {n['detail']}")
    return "\n".join(lines)


def report_all() -> str:
    sections = [
        report_portfolio(),
        "",
        report_signals(days=1),
        "",
        report_strategy(limit=5),
    ]
    return "\n".join(sections)


REPORTS = {
    "signals": report_signals,
    "portfolio": report_portfolio,
    "trades": report_trades,
    "strategy": report_strategy,
    "all": report_all,
}

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("type", choices=list(REPORTS.keys()), help="리포트 종류")
    parser.add_argument("--days", type=int, default=1, help="신호 조회 기간 (days)")
    parser.add_argument("--limit", type=int, default=20, help="조회 건수")
    args = parser.parse_args()

    fn = REPORTS[args.type]
    if args.type == "signals":
        print(fn(days=args.days))
    elif args.type in ("trades", "strategy"):
        print(fn(limit=args.limit))
    else:
        print(fn())
