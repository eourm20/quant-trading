"""
전략 노트 기록 & 텔레그램 발송
claude.ai에서 전략 결정 후 호출

사용법:
  python worker/strategy_log.py --category trade --summary "요약" --detail "상세 이유"
  python worker/strategy_log.py --category watchlist --summary "요약"
  python worker/strategy_log.py --category general --summary "요약"

category:
  trade     - 매수/매도 결정
  watchlist - 조건 변경 (손절가, RSI 등)
  general   - 전반적인 전략 변경
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

from data.db import save_strategy_note
from notifications.telegram import send_message

CATEGORY_EMOJI = {
    "trade":     "💼",
    "watchlist": "⚙️",
    "general":   "📋",
}

CATEGORY_LABEL = {
    "trade":     "매매 결정",
    "watchlist": "조건 변경",
    "general":   "전략 메모",
}


def log_strategy(category: str, summary: str, detail: str = "") -> bool:
    # DB 저장
    save_strategy_note(category, summary, detail)

    # 텔레그램 발송
    emoji = CATEGORY_EMOJI.get(category, "📌")
    label = CATEGORY_LABEL.get(category, category)
    text = f"{emoji} *[{label}]*\n\n{summary}"
    if detail:
        text += f"\n\n📝 {detail}"

    return send_message(text)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="전략 노트 기록 및 텔레그램 발송")
    parser.add_argument("--category", choices=["trade", "watchlist", "general"],
                        default="general", help="분류")
    parser.add_argument("--summary", required=True, help="한 줄 요약")
    parser.add_argument("--detail", default="", help="상세 이유 (선택)")
    args = parser.parse_args()

    ok = log_strategy(args.category, args.summary, args.detail)
    print(f"{'✅ 전송 완료' if ok else '❌ 전송 실패'}: [{args.category}] {args.summary}")
