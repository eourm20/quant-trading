"""
장 마감 후 일일 리포트 텔레그램 발송
"""

from datetime import datetime
from data.db import get_today_signals, get_strategy_notes
from notifications.telegram import send_message


def send_daily_report():
    signals = get_today_signals()
    today = datetime.now().strftime("%Y-%m-%d")

    lines = [f"📋 *{today} 일일 리포트*\n"]

    # 신호 섹션
    if not signals:
        lines.append("*[신호]* 오늘 발생한 신호 없음")
    else:
        lines.append(f"*[신호]* 총 {len(signals)}건")
        for s in signals:
            time_str = s["created_at"][11:16]
            opinion_short = ""
            if s["claude_opinion"]:
                for tag in ["[매수]", "[매도]", "[홀드]"]:
                    if tag in s["claude_opinion"]:
                        opinion_short = f" → {tag}"
                        break
            lines.append(
                f"• {time_str} *{s['stock_name']}* {s['current_price']:,}원"
                f"\n  {s['triggered_conditions']}{opinion_short}"
            )

    # 전략 노트 섹션 (오늘 기록된 것만, 카테고리별 분리)
    notes = [n for n in get_strategy_notes(limit=50)
             if n["created_at"].startswith(today)]
    if notes:
        category_cfg = [
            ("trade",      "💼", "매매"),
            ("watchlist",  "⚙️", "조건 변경"),
            ("general",    "📋", "전략 메모"),
        ]
        by_cat: dict[str, list] = {}
        for n in notes:
            by_cat.setdefault(n["category"], []).append(n)

        lines.append("")
        for cat_key, emoji, label in category_cfg:
            cat_notes = by_cat.pop(cat_key, [])
            if not cat_notes:
                continue
            lines.append(f"*[{label}]* {len(cat_notes)}건")
            for n in cat_notes:
                time_str = n["created_at"][11:16]
                lines.append(f"• {time_str} {emoji} {n['summary']}")
        # 알 수 없는 카테고리
        for cat_key, cat_notes in by_cat.items():
            lines.append(f"*[{cat_key}]* {len(cat_notes)}건")
            for n in cat_notes:
                time_str = n["created_at"][11:16]
                lines.append(f"• {time_str} 📌 {n['summary']}")

    send_message("\n".join(lines))
