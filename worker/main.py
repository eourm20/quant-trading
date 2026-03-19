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

from worker.kiwoom_client import KiwoomClient
from worker.monitor import check_stock, load_conditions
from worker.claude_judge import get_trade_opinion
from worker.cooldown import filter_new_conditions, mark_sent
from worker.portfolio_sync import sync_all
from notifications.telegram import send_signal_alert, send_message
from notifications.telegram_bot import start_bot_thread
from data.db import init_db, save_signal, get_portfolio, get_watchlist, reset_all_cooldowns
from worker.daily_report import send_daily_report

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
                    claude_opinion = get_trade_opinion(signal, holdings, kospi, kosdaq, sector)
                    logger.info(f"[{signal.stock_name}] AI 판단: {claude_opinion[:80]}...")
                except Exception as e:
                    logger.error(f"AI API 오류: {e}")

            mark_sent(signal.stock_code, new_ids)
            save_signal(signal, claude_opinion, in_portfolio=signal.in_portfolio)
            send_signal_alert(signal, claude_opinion, holdings=holdings)

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
                      day_of_week="mon-fri", hour="8-18", minute="*/10",
                      id="sync_realtime")
    scheduler.add_job(send_daily_report, "cron", hour=15, minute=35, id="daily_report")
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
