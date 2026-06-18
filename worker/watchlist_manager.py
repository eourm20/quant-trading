"""
Watchlist 관리: 트레일링 스탑, 장기 미발동 알림, 자동 제거.
"""

import logging
from datetime import datetime

from data.db import (
    delete_stock,
    get_portfolio,
    get_positions,
    get_watchlist,
    get_cooldown,
    set_cooldown,
    save_strategy_note,
    update_position_field,
)
from notifications.telegram import send_message
from worker.worker_utils import _now_kst

logger = logging.getLogger(__name__)

# worker.yaml의 worker.watchlist_management 섹션 — main.py의 init()으로 주입
_worker_config: dict = {}


def init(worker_config: dict) -> None:
    """main() 시작 시 한 번 호출해 WORKER_CONFIG를 주입한다."""
    global _worker_config
    _worker_config = worker_config


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------

def check_trailing_stops():
    """보유 종목 수익률 구간별 손절가 자동 상향 (트레일링 스탑).
    +5%  → 손절가를 평단가(본전)로 상향
    +10% → 손절가를 평단가 +5%로 상향
    +15% → 손절가를 평단가 +10%로 상향
    이미 설정된 손절가보다 낮으면 변경 안 함 (손절가는 항상 올리기만).
    positions 테이블에서 손절가를 읽고 업데이트.
    """
    holdings = get_portfolio()
    positions_map = {p["stock_code"]: p for p in get_positions()}

    for h in holdings:
        code = str(h.get("stock_code", ""))
        pos = positions_map.get(code)
        if not pos:
            continue

        avg_price = h.get("avg_price", 0)
        current_price = h.get("current_price", 0)
        if not avg_price or not current_price:
            continue

        current_sl = pos.get("stop_loss_price") or 0
        profit_pct = (current_price - avg_price) / avg_price * 100

        if profit_pct >= 15:
            candidate = int(avg_price * 1.10)
        elif profit_pct >= 10:
            candidate = int(avg_price * 1.05)
        elif profit_pct >= 5:
            candidate = avg_price
        else:
            continue

        if candidate <= current_sl:
            continue

        update_position_field(code, "stop_loss_price", candidate)
        save_strategy_note(
            "watchlist",
            f"{pos['stock_name']} 손절가 트레일링 상향: {current_sl:,} → {candidate:,}원",
            f"수익률 {profit_pct:+.1f}% 도달, 평단 {avg_price:,}원 기준 자동 상향",
        )
        send_message(
            f"📈 *{pos['stock_name']}* 손절가 트레일링 상향\n"
            f"수익률 *{profit_pct:+.1f}%* | {current_sl:,}원 → *{candidate:,}원*"
        )
        logger.info(f"[트레일링] {pos['stock_name']} 손절가 {current_sl:,} → {candidate:,}원 (수익률 {profit_pct:+.1f}%)")


def check_inactive_stocks():
    """30일 이상 신호 미발동 종목 주 1회 텔레그램 알림."""
    from data.db import get_conn
    _wm = _worker_config.get("watchlist_management", {})
    INACTIVE_DAYS = int(_wm.get("inactive_days_alert", 30))
    ALERT_INTERVAL_DAYS = int(_wm.get("alert_interval_days", 7))

    stocks = [s for s in get_watchlist() if s.get("enabled")]
    alerts = []

    for stock in stocks:
        code = stock["code"]
        name = stock["name"]

        key = f"{code}:inactive_alert"
        next_allowed_at = get_cooldown(key)
        if next_allowed_at and _now_kst() < next_allowed_at:
            continue

        with get_conn() as conn:
            row = conn.execute(
                "SELECT MAX(created_at) as last_signal FROM signals WHERE stock_code = ?",
                (code,)
            ).fetchone()

        last_signal = row["last_signal"] if row and row["last_signal"] else None
        if last_signal:
            days_since = (_now_kst() - datetime.strptime(last_signal[:10], "%Y-%m-%d")).days
        else:
            created_at = stock.get("created_at", "")
            if created_at:
                try:
                    created_dt = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S")
                    days_since = (_now_kst() - created_dt).days
                except ValueError:
                    continue
            else:
                continue

        if days_since >= INACTIVE_DAYS:
            alerts.append((name, days_since))
            set_cooldown(key, cooldown_minutes=ALERT_INTERVAL_DAYS * 24 * 60)

    if alerts:
        lines = "\n".join(f"  • {name}: {days}일째 신호 없음" for name, days in alerts)
        send_message(f"⚠️ *장기 미발동 종목 알림*\n\n{lines}\n\n_조건 검토 또는 모니터링 해제 고려_")
        logger.info(f"[미발동 알림] {len(alerts)}개 종목: {[n for n, _ in alerts]}")


def check_removal_candidates():
    """미보유 종목 자동 삭제.
    - 신규 등록: 7일 이내 매매 없으면 삭제
    - 청산 후: 2일 이내 재진입 없으면 삭제
    """
    _wm = _worker_config.get("watchlist_management", {})
    NO_TRADE_DAYS = int(_wm.get("no_trade_days_removal", 7))
    POST_LIQ_DAYS = int(_wm.get("no_trade_days_after_liquidation", 2))

    holdings = {str(h.get("stock_code", "")): h for h in get_portfolio()}
    stocks = [s for s in get_watchlist() if s.get("enabled")]

    for stock in stocks:
        code = stock["code"]
        name = stock["name"]

        if code in holdings:
            continue

        is_post_liq = bool(stock.get("post_liquidation"))
        threshold = POST_LIQ_DAYS if is_post_liq else NO_TRADE_DAYS

        created_at = stock.get("created_at", "")
        if not created_at:
            continue
        try:
            created_dt = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue

        days_since = (_now_kst() - created_dt).days
        if days_since >= threshold:
            reason = f"청산 후 {days_since}일 재진입 없음" if is_post_liq else f"등록 후 {days_since}일 매매 없음"
            delete_stock(code)
            send_message(
                f"🗑 *[자동 제거] {name}* ({code})\n"
                f"{reason}\n"
                f"관심종목에서 삭제했습니다."
            )
            save_strategy_note(
                "watchlist",
                f"{name} 관심종목 자동 삭제 ({reason})",
                f"watchlist 삭제 기준: {'청산 후' if is_post_liq else '신규 등록 후'} {days_since}일 경과",
            )
            logger.info(f"[자동 제거] {name}({code}) {reason}")