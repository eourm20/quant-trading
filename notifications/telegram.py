"""
텔레그램 알림
"""

import os
import httpx
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

TELEGRAM_MAX_LEN = 4000  # 실제 한도 4096, 여유 두고 4000


def _post(text: str, parse_mode: str | None = "Markdown", reply_markup: dict | None = None) -> bool:
    if not BOT_TOKEN or not CHAT_ID:
        return False
    payload: dict = {"chat_id": CHAT_ID, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json=payload,
            timeout=10,
        )
        return resp.status_code == 200
    except Exception:
        return False


def send_message(text: str) -> bool:
    return _post(text)


def _parse_order_type_rec(claude_opinion: str) -> str:
    """claude_opinion에서 [주문방식] 줄을 추출. 없으면 빈 문자열."""
    for line in claude_opinion.splitlines():
        if line.strip().startswith("[주문방식]"):
            return line.strip()
    return ""


def send_signal_alert(signal, claude_opinion: str | None = None, holdings: list | None = None) -> bool:
    conditions_text = "\n".join(f"  • {c}" for c in signal.triggered_conditions)

    # 보유 중이면 매입가/수량/수익률 표시
    holding_line = ""
    if signal.in_portfolio and holdings:
        for h in holdings:
            code = str(h.get("stock_code", ""))
            if code == signal.stock_code:
                qty = h.get("quantity", 0)
                avg = h.get("avg_price", 0)
                rate = h.get("profit_rate", 0)
                if qty and avg:
                    holding_line = (
                        f"📂 보유: *{qty:,}주* | 매입 *{avg:,}원* | 수익률 *{rate:+.2f}%*\n"
                    )
                break

    # AI 주문방식 추천 파싱
    order_rec_line = ""
    if claude_opinion:
        rec = _parse_order_type_rec(claude_opinion)
        if rec:
            order_rec_line = f"\n🤖 {rec}\n"

    code = signal.stock_code
    name = signal.stock_name

    signal_text = (
        f"🚨 *{name}* ({code}) 신호 감지\n\n"
        f"💰 현재가: *{signal.current_price:,}원*\n"
        f"{holding_line}"
        f"📊 RSI: {signal.rsi if signal.rsi else 'N/A'}\n"
        f"📦 거래량 배율: {signal.volume_ratio if signal.volume_ratio else 'N/A'}배\n\n"
        f"⚡ 트리거된 조건:\n{conditions_text}"
        f"{order_rec_line}\n"
        f"어떻게 하시겠습니까?"
    )

    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "📈 매수-시장가", "callback_data": f"buy_market:{code}:{name}"},
                {"text": "📈 매수-지정가", "callback_data": f"buy_limit:{code}:{name}"},
            ],
            [
                {"text": "📉 매도-시장가", "callback_data": f"sell_market:{code}:{name}"},
                {"text": "📉 매도-지정가", "callback_data": f"sell_limit:{code}:{name}"},
            ],
            [
                {"text": "⏸ 홀드", "callback_data": f"hold:{code}:{name}"},
            ],
        ]
    }
    ok = _post(signal_text, parse_mode="Markdown", reply_markup=reply_markup)

    # 2번 메시지: AI 판단 전문 (주문방식 줄 제외)
    if claude_opinion:
        # [주문방식] 줄은 이미 1번 메시지에 포함됐으므로 제거
        opinion_body = "\n".join(
            l for l in claude_opinion.splitlines()
            if not l.strip().startswith("[주문방식]")
        ).strip()
        if opinion_body:
            opinion_text = f"🤖 AI 판단:\n\n{opinion_body}"
            if len(opinion_text) > TELEGRAM_MAX_LEN:
                opinion_text = opinion_text[:TELEGRAM_MAX_LEN] + "\n\n...(이하 생략)"
            _post(opinion_text, parse_mode=None)

    return ok
