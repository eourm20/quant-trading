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
    import logging
    _logger = logging.getLogger(__name__)
    if not BOT_TOKEN or not CHAT_ID:
        _logger.warning("[텔레그램] BOT_TOKEN 또는 CHAT_ID 미설정 — 알림 건너뜀")
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
        if resp.status_code == 200:
            return True

        body_preview = resp.text[:200]
        _logger.warning(f"[텔레그램] 발송 실패: status={resp.status_code} body={body_preview}")

        # Markdown entity parse error fallback: resend as plain text.
        if parse_mode and ("can't parse entities" in body_preview.lower() or "parse entities" in body_preview.lower()):
            retry_payload: dict = {"chat_id": CHAT_ID, "text": text}
            if reply_markup:
                retry_payload["reply_markup"] = reply_markup
            retry = httpx.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json=retry_payload,
                timeout=10,
            )
            if retry.status_code == 200:
                _logger.warning("[텔레그램] Markdown 파싱 실패로 plain text 재전송 성공")
                return True
            _logger.warning(f"[텔레그램] plain text 재전송 실패: status={retry.status_code} body={retry.text[:200]}")
        return False
    except Exception as e:
        _logger.warning(f"[텔레그램] 발송 예외: {e}")
        return False


def send_message(text: str) -> bool:
    if len(text) <= TELEGRAM_MAX_LEN:
        return _post(text)
    # 길이 초과 시 줄 단위로 분할 발송
    lines = text.split("\n")
    chunk = ""
    ok = True
    for line in lines:
        if chunk and len(chunk) + len(line) + 1 > TELEGRAM_MAX_LEN:
            ok = _post(chunk) and ok
            chunk = ""
        chunk = f"{chunk}\n{line}" if chunk else line
    if chunk:
        ok = _post(chunk) and ok
    return ok


def send_message_with_inline_buttons(text: str, buttons: list[list[dict]]) -> int | None:
    """인라인 버튼이 포함된 메시지 발송. 성공 시 message_id 반환."""
    if not BOT_TOKEN or not CHAT_ID:
        return None
    payload: dict = {
        "chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown",
        "reply_markup": {"inline_keyboard": buttons},
    }
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json=payload, timeout=10,
        )
        if resp.status_code == 200:
            return resp.json().get("result", {}).get("message_id")
        if "parse entities" in (resp.text or "").lower():
            retry_payload = {
                "chat_id": CHAT_ID,
                "text": text,
                "reply_markup": {"inline_keyboard": buttons},
            }
            retry = httpx.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json=retry_payload, timeout=10,
            )
            if retry.status_code == 200:
                return retry.json().get("result", {}).get("message_id")
    except Exception:
        pass
    return None


_FIELD_LABELS = {
    "rsi_oversold":          "RSI 과매도",
    "rsi_overbought":        "RSI 과매수",
    "rsi_oversold_intraday": "RSI 과매도(분봉)",
    "volume_surge_ratio":    "거래량 배율",
    "target_price":          "목표가",
    "stop_loss_price":       "손절가",
}


def send_threshold_proposal(
    stock_code: str,
    stock_name: str,
    condition_text: str,
    changes: list[dict],  # [{"field": str, "old": int, "new": int}, ...]
) -> int | None:
    """AI 홀드 시 임계값 변경 제안 메시지 발송. 성공 시 message_id 반환."""
    if not changes:
        return None

    lines = [f"*[{stock_name}] AI 홀드 — 임계값 변경 제안*"]
    if condition_text:
        lines.append(f"_전환조건: {condition_text}_")
    lines.append("")
    lines.append("변경 제안:")
    for ch in changes:
        label = _FIELD_LABELS.get(ch["field"], ch["field"])
        old_v = f"{ch['old']:,}" if isinstance(ch["old"], int) else str(ch["old"])
        new_v = f"{ch['new']:,}" if isinstance(ch["new"], int) else str(ch["new"])
        lines.append(f"  • {label}: {old_v} → *{new_v}*")

    text = "\n".join(lines)
    keyboard = _build_threshold_keyboard(stock_code, changes)

    if not BOT_TOKEN or not CHAT_ID:
        return None
    payload: dict = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown",
                     "reply_markup": {"inline_keyboard": keyboard}}
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json=payload, timeout=10,
        )
        if resp.status_code == 200:
            return resp.json().get("result", {}).get("message_id")
        if "parse entities" in (resp.text or "").lower():
            retry_payload = {
                "chat_id": CHAT_ID,
                "text": text,
                "reply_markup": {"inline_keyboard": keyboard},
            }
            retry = httpx.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json=retry_payload, timeout=10,
            )
            if retry.status_code == 200:
                return retry.json().get("result", {}).get("message_id")
    except Exception:
        pass
    return None


def _build_threshold_keyboard(stock_code: str, changes: list[dict]) -> list:
    """임계값 제안용 인라인 키보드 빌드."""
    keyboard = []
    for ch in changes:
        label = _FIELD_LABELS.get(ch["field"], ch["field"])
        keyboard.append([
            {"text": f"✅ {label} 적용", "callback_data": f"th_apply:{stock_code}:{ch['field']}:{ch['new']}"},
            {"text": "✏️ 수정", "callback_data": f"th_edit:{stock_code}:{ch['field']}:{ch['old']}"},
            {"text": "❌", "callback_data": f"th_reject:{stock_code}:{ch['field']}"},
        ])
    keyboard.append([{"text": "⬅️ 전체 취소", "callback_data": f"th_reject_all:{stock_code}"}])
    return keyboard


def _parse_order_type_rec(claude_opinion: str) -> str:
    """claude_opinion에서 [주문방식] 줄을 추출. 없으면 빈 문자열."""
    for line in claude_opinion.splitlines():
        if line.strip().startswith("[주문방식]"):
            return line.strip()
    return ""


def _parse_qty_rec(claude_opinion: str) -> int | None:
    """claude_opinion에서 [추천수량] N주를 파싱. 없으면 None."""
    import re
    for line in claude_opinion.splitlines():
        if line.strip().startswith("[추천수량]"):
            m = re.search(r"(\d+)\s*주", line)
            if m:
                return int(m.group(1))
    return None


def send_signal_alert(signal, claude_opinion: str | None = None, holdings: list | None = None, signal_id: int | None = None, auto_mode: bool = False) -> bool:
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

    # AI 주문방식/추천수량 파싱
    order_rec_line = ""
    rec_qty: int | None = None
    if claude_opinion:
        rec = _parse_order_type_rec(claude_opinion)
        if rec:
            order_rec_line = f"\n🤖 {rec}\n"
        rec_qty = _parse_qty_rec(claude_opinion)

    code = signal.stock_code
    name = signal.stock_name

    qty_rec_line = f"🎯 AI 추천 수량: *{rec_qty:,}주*\n" if rec_qty else ""

    signal_text = (
        f"🚨 *{name}* ({code}) 신호 감지\n\n"
        f"💰 현재가: *{signal.current_price:,}원*\n"
        f"{holding_line}"
        f"📊 RSI: {signal.rsi if signal.rsi else 'N/A'}\n"
        f"📦 거래량 배율: {signal.volume_ratio if signal.volume_ratio else 'N/A'}배\n\n"
        f"⚡ 트리거된 조건:\n{conditions_text}"
        f"{order_rec_line}"
        f"{qty_rec_line}\n"
        f"어떻게 하시겠습니까?"
    )

    # callback_data는 64바이트 제한이 있으므로 code/signal_id 중심으로 짧게 유지
    sid_num = int(signal_id) if isinstance(signal_id, int) else 0

    # 추천수량이 없으면 기존 콜백 → 클릭 후 수량 직접 입력
    if rec_qty:
        qs = f" ({rec_qty:,}주)"
        buy_market_cb  = f"rec_buy_market:{rec_qty}:{code}:{sid_num}"
        buy_limit_cb   = f"rec_buy_limit:{rec_qty}:{code}:{sid_num}"
        sell_market_cb = f"rec_sell_market:{rec_qty}:{code}:{sid_num}"
        sell_limit_cb  = f"rec_sell_limit:{rec_qty}:{code}:{sid_num}"
    else:
        qs = ""
        buy_market_cb  = f"buy_market:{code}:{sid_num}"
        buy_limit_cb   = f"buy_limit:{code}:{sid_num}"
        sell_market_cb = f"sell_market:{code}:{sid_num}"
        sell_limit_cb  = f"sell_limit:{code}:{sid_num}"

    reply_markup = {
        "inline_keyboard": [
            [
                {"text": f"📈 {name} 매수-시장가{qs}", "callback_data": buy_market_cb},
                {"text": f"📈 매수-지정가{qs}", "callback_data": buy_limit_cb},
            ],
            [
                {"text": f"📉 {name} 매도-시장가{qs}", "callback_data": sell_market_cb},
                {"text": f"📉 매도-지정가{qs}", "callback_data": sell_limit_cb},
            ],
            [
                {"text": f"⏸ {name} 홀드", "callback_data": f"hold:{code}:{sid_num}"},
            ],
        ]
    }
    # 자동 모드: 버튼 없이 신호 + AI 판단만 발송
    if auto_mode:
        ok = _post(signal_text, parse_mode="Markdown")
        if claude_opinion:
            opinion_body = "\n".join(
                l for l in claude_opinion.splitlines()
                if not l.strip().startswith("[주문방식]")
                and not l.strip().startswith("[추천수량]")
            ).strip()
            if opinion_body:
                opinion_text = f"🤖 AI 판단:\n\n{opinion_body}"
                if len(opinion_text) > TELEGRAM_MAX_LEN:
                    opinion_text = opinion_text[:TELEGRAM_MAX_LEN] + "\n\n...(이하 생략)"
                _post(opinion_text, parse_mode=None)
        return ok

    # 수동 모드: AI 판단이 있으면 버튼을 AI 판단 메시지에, 없으면 신호 메시지에 첨부
    if claude_opinion:
        ok = _post(signal_text, parse_mode="Markdown")
        opinion_body = "\n".join(
            l for l in claude_opinion.splitlines()
            if not l.strip().startswith("[주문방식]")
            and not l.strip().startswith("[추천수량]")
        ).strip()
        if opinion_body:
            opinion_text = f"🤖 AI 판단:\n\n{opinion_body}"
            if len(opinion_text) > TELEGRAM_MAX_LEN:
                opinion_text = opinion_text[:TELEGRAM_MAX_LEN] + "\n\n...(이하 생략)"
            _post(opinion_text, parse_mode=None, reply_markup=reply_markup)
    else:
        ok = _post(signal_text, parse_mode="Markdown", reply_markup=reply_markup)

    return ok
