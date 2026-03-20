"""
백그라운드 워커 메인 진입점
- 장 운영 시간 동안 주기적으로 종목 조건 체크
- 조건 충족 시 Claude API 판단 → 텔레그램 알림
- 종목/조건 설정은 DB에서 실시간 로드 (변경 즉시 반영)
"""

import argparse
import logging
import logging.handlers
import os
import sys
import time
from datetime import datetime, time as dtime

import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

from worker.clients.kiwoom_client import KiwoomClient
from worker.monitor import check_stock, load_conditions
from worker.claude_judge import get_trade_opinion
from worker.cooldown import filter_new_conditions, mark_sent
from worker.stock_analyzer import run_daily_screening, run_intraday_scan
from worker.portfolio_sync import sync_all
from notifications.telegram import send_signal_alert, send_message
from notifications.telegram_bot import start_bot_thread
from data.db import (init_db, save_signal, get_portfolio, get_watchlist, reset_all_cooldowns,
                     update_signal_result, update_stock_field, save_strategy_note,
                     get_cooldown, set_cooldown, get_last_signal_date, delete_stock)

log_dir = os.path.join(os.path.dirname(__file__), '..', 'logs')
os.makedirs(log_dir, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.handlers.TimedRotatingFileHandler(
            os.path.join(log_dir, "worker.log"),
            when="midnight",
            backupCount=1,
            encoding="utf-8",
        ),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

CONFIG_PATH = os.path.join(os.path.dirname(__file__), '..', 'config', 'worker.yaml')
TEST_MODE = False

# worker 설정만 yaml에서 로드 (장 시간, 인터벌 등)
with open(CONFIG_PATH, encoding="utf-8") as f:
    _WORKER_CONFIG = yaml.safe_load(f).get("worker", {})

kiwoom = KiwoomClient()

AUTO_TRADE = os.getenv("AUTO_TRADE", "false").lower() == "true"

# KRX 거래 세션 (규정값)
_SESSIONS: dict[str, tuple[dtime, dtime]] = {
    "premarket":  (dtime(8, 30),  dtime(9, 0)),    # 장전 시간외 (trde_tp 61)
    "main":       (dtime(9, 0),   dtime(15, 30)),   # 정규장 (trde_tp 0/3)
    "aftermarket":(dtime(15, 40), dtime(16, 0)),    # 장후 시간외 (trde_tp 81)
    "offhours":   (dtime(16, 0),  dtime(18, 0)),    # 시간외 단일가 (trde_tp 62)
}


def get_current_session() -> str | None:
    """현재 거래 가능 세션 반환. 장외 시간이면 None."""
    now = datetime.now()
    if now.weekday() >= 5:
        return None
    t = now.time()
    for session, (start, end) in _SESSIONS.items():
        if start <= t <= end:
            return session
    return None


def _maybe_save_hold_conditions(signal, opinion: str):
    """AI가 홀드 판단 시 [전환조건]/[임계값] 파싱 → 전략 노트 저장 + 텔레그램 변경 제안."""
    import re
    if not opinion:
        return
    first_line = opinion.strip().splitlines()[0] if opinion.strip() else ""
    if "홀드" not in first_line:
        return

    condition_text = ""
    threshold_changes = []

    for line in opinion.splitlines():
        stripped = line.strip()
        if stripped.startswith("[전환조건]"):
            condition_text = stripped[len("[전환조건]"):].strip()
        elif stripped.startswith("[임계값]"):
            content = stripped[len("[임계값]"):].strip()
            allowed = {"rsi_oversold", "rsi_overbought", "rsi_oversold_intraday", "volume_surge_ratio", "target_price", "stop_loss_price"}
            for match in re.finditer(r"(\w+)\s*=\s*(\d+(?:\.\d+)?)", content):
                field, value_str = match.group(1), match.group(2)
                if field in allowed:
                    # volume_surge_ratio는 소수점 유지, 나머지는 정수
                    new_val = float(value_str) if field == "volume_surge_ratio" else int(float(value_str))
                    # 현재 watchlist 값 조회
                    from data.db import get_watchlist
                    stock = next((s for s in get_watchlist() if s["code"] == signal.stock_code), None)
                    old_val = 0
                    if stock:
                        import json as _json
                        cond_data = _json.loads(stock.get("conditions", "{}")) if isinstance(stock.get("conditions"), str) else stock.get("conditions", {})
                        old_val = int(cond_data.get(field) or 0)
                    if new_val != old_val:  # 실제 변경이 있는 경우만 포함
                        threshold_changes.append({"field": field, "old": old_val, "new": new_val})

    # 전략 노트는 임계값이 실제로 적용될 때만 저장됨 (_apply_threshold_change 참고)
    if threshold_changes:
        from notifications.telegram import send_threshold_proposal
        from notifications.telegram_bot import store_threshold_proposal
        msg_id = send_threshold_proposal(signal.stock_code, signal.stock_name, condition_text, threshold_changes)
        if msg_id:
            store_threshold_proposal(signal.stock_code, msg_id, signal.stock_name, condition_text, threshold_changes)
        logger.info(f"[{signal.stock_name}] 임계값 변경 제안 발송: {threshold_changes}")


def update_signal_results():
    """신호 발생 후 1일/3일/5일/10일 결과 수익률을 현재가 기준으로 업데이트."""
    from datetime import timedelta
    from data.db import get_conn

    periods = [
        ("1d", 1, 2, "result_1d"),
        ("3d", 3, 4, "result_pct"),
        ("5d", 5, 6, "result_5d"),
        ("10d", 10, 11, "result_10d"),
    ]

    for period_name, days_after, days_before, col_name in periods:
        cutoff_from = (datetime.now() - timedelta(days=days_before)).strftime("%Y-%m-%d")
        cutoff_to = (datetime.now() - timedelta(days=days_after)).strftime("%Y-%m-%d")
        with get_conn() as conn:
            rows = conn.execute(
                f"SELECT id, stock_code, stock_name, current_price FROM signals "
                f"WHERE {col_name} IS NULL AND created_at >= ? AND created_at < ?",
                (cutoff_from, cutoff_to),
            ).fetchall()

        for row in rows:
            try:
                pd = kiwoom.get_current_price(row["stock_code"])
                now_price = abs(int(str(
                    pd.get("cur_prc") or pd.get("stk_prpr") or pd.get("prpr") or "0"
                ).replace(",", "")))
                if now_price and row["current_price"]:
                    pct = (now_price - row["current_price"]) / row["current_price"] * 100
                    update_signal_result(row["id"], round(pct, 2), period=period_name)
                    logger.debug(f"[결과 {period_name}] {row['stock_name']} #{row['id']}: {pct:+.2f}%")
                time.sleep(0.5)
            except Exception as e:
                logger.warning(f"[결과 {period_name} 실패] {row['stock_name']}: {e}")


def check_trailing_stops():
    """보유 종목 수익률 구간별 손절가 자동 상향 (트레일링 스탑).
    +5%  → 손절가를 평단가(본전)로 상향
    +10% → 손절가를 평단가 +5%로 상향
    +15% → 손절가를 평단가 +10%로 상향
    이미 설정된 손절가보다 낮으면 변경 안 함 (손절가는 항상 올리기만).
    """
    holdings = get_portfolio()
    watchlist = {s["code"]: s for s in get_watchlist() if s.get("enabled")}

    for h in holdings:
        code = str(h.get("stock_code", ""))
        if code not in watchlist:
            continue

        avg_price = h.get("avg_price", 0)
        current_price = h.get("current_price", 0)
        if not avg_price or not current_price:
            continue

        stock = watchlist[code]
        cond = stock.get("conditions", {})
        current_sl = cond.get("stop_loss_price") or 0

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

        update_stock_field(code, "stop_loss_price", candidate)
        save_strategy_note(
            "watchlist",
            f"{stock['name']} 손절가 트레일링 상향: {current_sl:,} → {candidate:,}원",
            f"수익률 {profit_pct:+.1f}% 도달, 평단 {avg_price:,}원 기준 자동 상향",
        )
        send_message(
            f"📈 *{stock['name']}* 손절가 트레일링 상향\n"
            f"수익률 *{profit_pct:+.1f}%* | {current_sl:,}원 → *{candidate:,}원*"
        )
        logger.info(f"[트레일링] {stock['name']} 손절가 {current_sl:,} → {candidate:,}원 (수익률 {profit_pct:+.1f}%)")


def check_inactive_stocks():
    """30일 이상 신호 미발동 종목 주 1회 텔레그램 알림."""
    from data.db import get_conn
    INACTIVE_DAYS = 30
    ALERT_INTERVAL_DAYS = 7

    stocks = [s for s in get_watchlist() if s.get("enabled")]
    alerts = []

    for stock in stocks:
        code = stock["code"]
        name = stock["name"]

        # 주 1회 알림 쿨다운 체크
        key = f"{code}:inactive_alert"
        last = get_cooldown(key)
        if last and (datetime.now() - last).days < ALERT_INTERVAL_DAYS:
            continue

        # 마지막 신호 날짜 조회
        with get_conn() as conn:
            row = conn.execute(
                "SELECT MAX(created_at) as last_signal FROM signals WHERE stock_code = ?",
                (code,)
            ).fetchone()

        last_signal = row["last_signal"] if row and row["last_signal"] else None
        if last_signal:
            days_since = (datetime.now() - datetime.strptime(last_signal[:10], "%Y-%m-%d")).days
        else:
            days_since = 999

        if days_since >= INACTIVE_DAYS:
            alerts.append((name, days_since))
            set_cooldown(key)

    if alerts:
        lines = "\n".join(f"  • {name}: {days}일째 신호 없음" for name, days in alerts)
        send_message(f"⚠️ *장기 미발동 종목 알림*\n\n{lines}\n\n_조건 검토 또는 모니터링 해제 고려_")
        logger.info(f"[미발동 알림] {len(alerts)}개 종목: {[n for n, _ in alerts]}")


def check_removal_candidates():
    """미보유 종목 중 90일 미신호 → 관심종목 자동 삭제."""
    INACTIVE_DAYS = 90
    ALERT_INTERVAL_DAYS = 7

    holdings = {str(h.get("stock_code", "")): h for h in get_portfolio()}
    stocks = [s for s in get_watchlist() if s.get("enabled")]

    for stock in stocks:
        code = stock["code"]
        name = stock["name"]

        # 보유 종목은 삭제 대상 아님
        if code in holdings:
            continue

        # ── 미보유 종목: 90일 미신호 → 삭제 ──────────────────────────
        key = f"{code}:removal_check"
        last = get_cooldown(key)
        if last and (datetime.now() - last).days < ALERT_INTERVAL_DAYS:
            continue

        last_signal_dt = get_last_signal_date(code)
        if last_signal_dt:
            days_since = (datetime.now() - last_signal_dt).days
        else:
            days_since = 999

        if days_since >= INACTIVE_DAYS:
            delete_stock(code)
            set_cooldown(key)
            send_message(
                f"🗑 *[자동 제거] {name}* ({code})\n"
                f"미보유 상태로 {days_since}일간 신호 없음\n"
                f"관심종목에서 삭제했습니다."
            )
            save_strategy_note(
                "watchlist",
                f"{name} 관심종목 자동 삭제 (미보유 {days_since}일 미신호)",
                f"미보유 상태에서 {days_since}일간 신호 미발동으로 삭제",
            )
            logger.info(f"[자동 제거] {name}({code}) 미보유 {days_since}일 미신호 삭제")


def _auto_execute(signal, claude_opinion: str, signal_id: int | None) -> None:
    """AI 판단이 매수/매도이고 추천수량이 있으면 자동 주문 실행. 추천수량 없으면 홀드."""
    import re
    first_line = claude_opinion.strip().splitlines()[0] if claude_opinion.strip() else ""
    if "[매수]" in first_line:
        order_type, side = "1", "매수"
    elif "[매도]" in first_line:
        order_type, side = "2", "매도"
    else:
        logger.info(f"[{signal.stock_name}] 자동 모드: AI 홀드 — 스킵")
        return

    qty = None
    for line in claude_opinion.splitlines():
        if line.strip().startswith("[추천수량]"):
            m = re.search(r"(\d+)\s*주", line)
            if m:
                qty = int(m.group(1))
                break

    if not qty:
        logger.info(f"[{signal.stock_name}] 자동 모드: 추천수량 없음 — 홀드")
        return

    try:
        result = kiwoom.place_order(signal.stock_code, order_type, qty)
        ord_no = result.get("ord_no") or result.get("order_no") or "-"
        logger.info(f"[{signal.stock_name}] 자동 {side}: {qty}주, 주문번호 {ord_no}")
        send_message(
            f"🤖 *자동 {side} 주문 접수*\n"
            f"종목: *{signal.stock_name}* (`{signal.stock_code}`)\n"
            f"수량: *{qty:,}주* (시장가)\n"
            f"주문번호: `{ord_no}`"
        )
        from data.db import reset_cooldowns_for_stock, update_signal_action, save_strategy_note, set_add_cooldown_after_trade
        reset_cooldowns_for_stock(signal.stock_code)
        if order_type == "1":
            set_add_cooldown_after_trade(signal.stock_code)
        if signal_id is not None:
            update_signal_action(signal_id, side)
        save_strategy_note("trade", f"{signal.stock_name} {qty}주 {side} (자동 매매)")
        from worker.portfolio_sync import sync_all as _sync
        _sync(kiwoom)
    except Exception as e:
        logger.error(f"[{signal.stock_name}] 자동 주문 실패: {e}")
        send_message(f"❌ 자동 주문 실패: *{signal.stock_name}* — `{e}`")


def run_check():
    session = "main" if TEST_MODE else get_current_session()
    if session is None:
        logger.debug("장 운영 시간 외 - 스킵")
        return

    # DB에서 최신 종목/조건 로드 (MCP로 변경 시 즉시 반영)
    stocks = [s for s in get_watchlist() if s.get("enabled", False)]
    conditions = load_conditions()

    # 정규장 외 세션: entry 조건 제외 (exit/add/both만)
    if session != "main":
        conditions = [c for c in conditions if c.get("signal_type", "both") != "entry"]

    logger.info(f"=== 조건 체크 시작 [{session}] ({len(stocks)}개 종목, {len(conditions)}개 조건) ===")

    use_claude = _WORKER_CONFIG.get("use_claude_api", True)
    holdings = get_portfolio()

    kospi = kiwoom.get_market_index("kospi")
    time.sleep(1)
    kosdaq = kiwoom.get_market_index("kosdaq")
    time.sleep(1)

    for stock in stocks:
        time.sleep(1)
        signal = check_stock(kiwoom, stock, conditions, holdings)
        if signal:
            new_ids, new_conditions = filter_new_conditions(
                signal.stock_code, signal.triggered_ids, signal.triggered_conditions, conditions
            )

            if not new_conditions:
                logger.info(f"[{signal.stock_name}] 신호 감지됐으나 쿨다운 중 — 스킵")
                continue

            signal.triggered_conditions = new_conditions
            signal.triggered_ids = new_ids
            logger.info(f"[{signal.stock_name}] 신호 감지: {new_conditions}")

            claude_opinion = None
            if use_claude:
                try:
                    sector = kiwoom.get_sector_index(signal.sector_code) if signal.sector_code else {}
                    claude_opinion = get_trade_opinion(signal, holdings, kospi, kosdaq, sector, signal.recent_trades)
                    logger.info(f"[{signal.stock_name}] AI 판단: {claude_opinion[:80]}...")
                except Exception as e:
                    logger.error(f"AI API 오류: {e}")

            # DART 공시 요약 (AI에게 전달된 것과 동일한 내용 저장)
            dart_summary = None
            try:
                from worker.clients.dart_client import format_full_context_for_ai, DART_API_KEY
                if DART_API_KEY:
                    dart_summary = format_full_context_for_ai(signal.stock_code)
            except Exception:
                pass

            mark_sent(signal.stock_code, new_ids)
            signal_id = save_signal(signal, claude_opinion, in_portfolio=signal.in_portfolio, dart_summary=dart_summary)
            send_signal_alert(signal, claude_opinion, holdings=holdings, signal_id=signal_id, auto_mode=AUTO_TRADE)

            if claude_opinion:
                _maybe_save_hold_conditions(signal, claude_opinion)

            if AUTO_TRADE and claude_opinion:
                _auto_execute(signal, claude_opinion, signal_id)

    logger.info("=== 조건 체크 완료 ===")


def main():
    interval = _WORKER_CONFIG.get("interval_seconds", 60)
    init_db()

    logger.info("포트폴리오 초기 동기화 중...")
    sync_all(kiwoom)

    logger.info("텔레그램 봇 시작...")
    start_bot_thread(kiwoom_client=kiwoom)

    interval_min = max(1, interval // 60)
    logger.info(f"워커 시작 - {interval}초 간격으로 실행 (평일 08:00~18:00)")
    send_message("✅ Quant Trading 워커가 시작되었습니다.")

    def auto_sync():
        logger.info("포트폴리오 자동 동기화")
        sync_all(kiwoom)

    scheduler = BackgroundScheduler(timezone="Asia/Seoul")
    # 평일 08:00~18:59 사이에만 실행
    scheduler.add_job(run_check, "cron",
                      day_of_week="mon-fri", hour="8-18", minute=f"*/{interval_min}",
                      id="monitor")
    scheduler.add_job(reset_all_cooldowns, "cron", hour=9, minute=0, id="reset_cooldowns")
    scheduler.add_job(auto_sync, "cron", hour=8, minute=30, id="sync_premarket")
    scheduler.add_job(auto_sync, "cron", hour=9, minute=1, id="sync_open")
    scheduler.add_job(auto_sync, "cron", hour=18, minute=5, id="sync_close")
    scheduler.add_job(auto_sync, "cron",
                      day_of_week="mon-fri", hour="8-18", minute="*/2",
                      id="sync_realtime")
    scheduler.add_job(update_signal_results, "cron",
                      day_of_week="mon-fri", hour="9-18", minute="*/30",
                      id="result_update")
    scheduler.add_job(check_trailing_stops, "cron",
                      day_of_week="mon-fri", hour="9-15", minute="*/30",
                      id="trailing_stops")
    scheduler.add_job(check_inactive_stocks, "cron",
                      day_of_week="mon-fri", hour=8, minute=30,
                      id="inactive_alert")
    scheduler.add_job(check_removal_candidates, "cron",
                      day_of_week="mon-fri", hour="9-15", minute="*/30",
                      id="removal_check")
    scheduler.add_job(run_intraday_scan, "cron",
                      day_of_week="mon-fri", hour="10,13", minute=0,
                      id="intraday_scan")
    scheduler.add_job(run_daily_screening, "cron",
                      day_of_week="mon-fri", hour=15, minute=46,
                      id="daily_screening")
    run_check()

    scheduler.start()
    try:
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown(wait=False)
        logger.info("워커 종료")
        send_message("🛑 Quant Trading 워커가 종료되었습니다.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="장 시간 체크 무시하고 즉시 실행")
    args = parser.parse_args()

    if args.test:
        TEST_MODE = True
        logger.info("=== 테스트 모드 ===")

    main()
