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
from datetime import datetime, time as dtime, timezone, timedelta as _td

import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

_KST = timezone(_td(hours=9))


def _now_kst() -> datetime:
    """UTC/로컬 관계없이 항상 KST 현재 시각 반환 (naive — 기존 코드 호환)."""
    return datetime.now(_KST).replace(tzinfo=None)


# --env 인자를 imports 전에 미리 파싱 (모듈 레벨 코드가 올바른 환경변수를 읽도록)
_project_root = os.path.join(os.path.dirname(__file__), '..')
_pre_parser = argparse.ArgumentParser(add_help=False)
_pre_parser.add_argument("--env", type=str, default=None)
_pre_args, _ = _pre_parser.parse_known_args()
if _pre_args.env:
    _env_path = os.path.join(_project_root, _pre_args.env) if not os.path.isabs(_pre_args.env) else _pre_args.env
    os.environ["ENV_FILE"] = _env_path

_env_file = os.getenv("ENV_FILE", os.path.join(_project_root, '.env'))
load_dotenv(dotenv_path=_env_file, override=True)

from worker.clients.kiwoom_client import KiwoomClient
from worker.monitor import check_stock, load_conditions
from worker.claude_judge import get_trade_opinion, judge_position_values, get_dip_buy_opinion, get_last_agent_trace
from worker.cooldown import filter_new_conditions, mark_sent
from worker.stock_analyzer import run_daily_screening, run_intraday_scan, run_daily_review, reassess_watchlist
from worker.portfolio_sync import sync_all
from notifications.telegram import send_signal_alert, send_message
from notifications.telegram_bot import start_bot_thread
from data.db import (init_db, save_signal, get_portfolio, get_watchlist, reset_all_cooldowns,
                     update_signal_result, update_signal_agent_trace, update_stock_field, save_strategy_note,
                     get_cooldown, set_cooldown, get_last_signal_date, delete_stock,
                     get_positions, get_position, update_position_field, create_position_from_trade)

_log_prefix = os.getenv("LOG_PREFIX", "worker")
log_dir = os.path.join(os.path.dirname(__file__), '..', 'logs')
os.makedirs(log_dir, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.handlers.TimedRotatingFileHandler(
            os.path.join(log_dir, f"{_log_prefix}.log"),
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

# 매수 직후 entry 재발동 방지 (stock_code → 해제 시각)
_post_buy_lock: dict[str, datetime] = {}

# KRX 거래 세션 (규정값)
_SESSIONS: dict[str, tuple[dtime, dtime]] = {
    "premarket":  (dtime(8, 30),  dtime(9, 0)),    # 장전 시간외 (trde_tp 61)
    "main":       (dtime(9, 0),   dtime(15, 30)),   # 정규장 (trde_tp 0/3)
    "aftermarket":(dtime(15, 40), dtime(16, 0)),    # 장후 시간외 (trde_tp 81)
    "offhours":   (dtime(16, 0),  dtime(18, 0)),    # 시간외 단일가 (trde_tp 62)
}


def get_current_session() -> str | None:
    """현재 거래 가능 세션 반환. 장외 시간이면 None."""
    now = _now_kst()
    if now.weekday() >= 5:
        return None
    t = now.time()
    for session, (start, end) in _SESSIONS.items():
        if start <= t <= end:
            return session
    return None


def _rag_index_signal(
    signal,
    signal_id: int,
    claude_opinion: str | None = None,
    dart_summary: str | None = None,
    news_summary: str | None = None,
) -> None:
    """신호 저장 후 FAISS RAG 인덱싱 (백그라운드). db.py 의존성 분리용."""
    try:
        from worker.agents.tools.rag_tools import index_signal as _idx
        from data.db import build_indicator_snapshot, extract_verdict
        import threading
        verdict = extract_verdict(claude_opinion)
        indicator_snapshot = build_indicator_snapshot(signal)
        threading.Thread(
            target=_idx,
            kwargs={
                "signal_id": signal_id,
                "stock_name": signal.stock_name,
                "signal_type": getattr(signal, "signal_type", "") or "",
                "verdict": verdict,
                "result_3d": None,
                "triggered_conditions": ", ".join(signal.triggered_conditions),
                "dart_summary": dart_summary,
                "news_summary": news_summary,
                "indicator_snapshot": indicator_snapshot,
            },
            daemon=True,
        ).start()
    except Exception:
        pass


def _maybe_save_hold_conditions(signal, opinion: str):
    """AI가 홀드 판단 시 [전환조건]/[임계값] 파싱.

    전환조건: 항상 strategy_notes에 저장 → 이후 Agent 판단 시 get_entry_reason으로 참조
    임계값: 별도로 사용자 승인 후 watchlist 반영 (전략 원칙에 따라 허용 필드 제한)
    """
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
            allowed = {"rsi_overbought", "rsi_oversold_intraday", "volume_surge_ratio"}
            for match in re.finditer(r"(\w+)\s*=\s*([\d,]+(?:\.\d+)?)", content):
                field, value_str = match.group(1), match.group(2).replace(",", "")
                if field not in allowed:
                    logger.warning(f"[{signal.stock_name}] 임계값 허용되지 않은 필드 무시: {field}={value_str}")
                    continue
                new_val = float(value_str) if field == "volume_surge_ratio" else int(float(value_str))
                from data.db import get_watchlist
                stock = next((s for s in get_watchlist() if s["code"] == signal.stock_code), None)
                old_val = 0
                if stock:
                    old_val = int(stock.get(field) or 0)
                if new_val != old_val:
                    threshold_changes.append({"field": field, "old": old_val, "new": new_val})

    # ── 전환조건: 임계값 유무와 무관하게 항상 저장 ──────────────────────────────
    if condition_text:
        save_strategy_note(
            "watchlist",
            f"{signal.stock_name} 홀드 전환조건",
            f"신호: {', '.join(signal.triggered_conditions)}\n전환조건: {condition_text}",
        )
        logger.info(f"[{signal.stock_name}] 전환조건 저장: {condition_text}")

    # ── 임계값: 허용 필드만, 별도 승인 흐름 ────────────────────────────────────
    if not threshold_changes:
        return

    if AUTO_TRADE:
        applied = []
        for change in threshold_changes:
            ok = update_stock_field(signal.stock_code, change["field"], change["new"])
            if ok:
                applied.append(f"{change['field']}: {change['old']} → {change['new']}")
        if applied:
            detail = f"AI 홀드 판단에 따른 임계값 자동 적용\n전환조건: {condition_text}\n변경: {', '.join(applied)}"
            save_strategy_note("watchlist", f"{signal.stock_name} 임계값 자동 적용", detail)
            from notifications.telegram import send_message
            send_message(f"⚙️ *{signal.stock_name} 임계값 자동 적용*\n\n{chr(10).join(applied)}\n\n_전환조건: {condition_text}_")
            logger.info(f"[{signal.stock_name}] 임계값 자동 적용: {applied}")
    else:
        from notifications.telegram import send_threshold_proposal
        from notifications.telegram_bot import store_threshold_proposal
        msg_id = send_threshold_proposal(signal.stock_code, signal.stock_name, condition_text, threshold_changes)
        if msg_id:
            store_threshold_proposal(signal.stock_code, msg_id, signal.stock_name, condition_text, threshold_changes)
        logger.info(f"[{signal.stock_name}] 임계값 변경 제안 발송: {threshold_changes}")


def run_weekly_performance_report():
    """매주 월요일 09:00 — 지난주 AI 신호 성과 리포트를 전략 노트에 기록하고 텔레그램 발송."""
    from data.db import get_weekly_performance_report, save_strategy_note
    from notifications.telegram import send_message

    logger.info("[성과리포트] 주간 성과 분석 시작")
    try:
        rpt = get_weekly_performance_report(days=7)
    except Exception as e:
        logger.warning(f"[성과리포트] 데이터 조회 실패: {e}")
        return

    if not rpt or rpt.get("rated_count", 0) == 0:
        logger.info("[성과리포트] 최근 7일 평가 가능 신호 없음 — 스킵")
        return

    rated = rpt["rated_count"]
    total = rpt["signal_count"]
    wr = rpt["win_rate_3d"]
    avg3 = rpt["avg_return_3d"]
    avg1 = rpt["avg_return_1d"]
    avg5 = rpt["avg_return_5d"]
    best = rpt.get("best_stock") or {}
    worst = rpt.get("worst_stock") or {}

    # ── 판정별 요약 ──
    vbd = rpt.get("verdict_breakdown", {})
    vbd_lines = []
    for v, s in vbd.items():
        vbd_lines.append(
            f"  [{v}] {s['count']}건 | 승률 {s['win_rate']}% | 평균 {s['avg_return']:+.2f}%"
        )

    # ── 모의투자 요약 ──
    paper = rpt.get("paper_summary")
    paper_line = ""
    if paper and paper.get("count"):
        paper_line = (
            f"\n📋 모의투자: {paper['count']}건 | 승률 {paper.get('win_rate', '-')}% | "
            f"평균 {paper.get('avg_return', 0):+.2f}%"
        )

    summary = (
        f"주간 성과 리포트 — 신호 {total}건 / 평가 {rated}건 / "
        f"승률 {wr}% / 3일평균 {avg3:+.2f}%"
    )

    detail_parts = [
        f"## 주간 성과 요약 (최근 7일)",
        f"- 총 신호: {total}건 (평가 완료: {rated}건)",
        f"- 3일 승률: {wr}% | 평균 수익: {avg3:+.2f}%",
    ]
    if avg1 is not None:
        detail_parts.append(f"- 1일 평균: {avg1:+.2f}%")
    if avg5 is not None:
        detail_parts.append(f"- 5일 평균: {avg5:+.2f}%")
    if best:
        detail_parts.append(f"- 최고 수익: {best.get('name','?')} ({best.get('return', 0):+.2f}%)")
    if worst:
        detail_parts.append(f"- 최대 손실: {worst.get('name','?')} ({worst.get('return', 0):+.2f}%)")
    if vbd_lines:
        detail_parts.append("\n## 판정별 성과")
        detail_parts.extend(vbd_lines)
    if paper_line:
        detail_parts.append(paper_line)

    detail = "\n".join(detail_parts)

    try:
        save_strategy_note(category="general", summary=summary, detail=detail)
        logger.info(f"[성과리포트] 전략 노트 저장 완료")
    except Exception as e:
        logger.warning(f"[성과리포트] 전략 노트 저장 실패: {e}")
        return

    # 텔레그램 발송
    try:
        msg = (
            f"📈 *주간 성과 리포트*\n\n"
            f"신호 {total}건 (평가 {rated}건)\n"
            f"승률 {wr}% | 3일 평균 {avg3:+.2f}%\n"
        )
        if avg1 is not None:
            msg += f"1일 평균 {avg1:+.2f}%"
        if avg5 is not None:
            msg += f" | 5일 평균 {avg5:+.2f}%"
        msg += "\n"
        for line in vbd_lines:
            msg += f"\n{line.strip()}"
        if best:
            msg += f"\n\n🏆 최고: {best.get('name','?')} {best.get('return', 0):+.2f}%"
        if worst:
            msg += f"\n💀 최악: {worst.get('name','?')} {worst.get('return', 0):+.2f}%"
        if paper_line:
            msg += f"\n{paper_line.strip()}"
        send_message(msg)
    except Exception as e:
        logger.debug(f"[성과리포트] 텔레그램 발송 실패: {e}")


def run_weekly_self_correction():
    """매주 월요일 장 시작 직후 — 최근 30일 조건별·판정별 적중률을 전략 노트에 기록."""
    from data.db import get_condition_accuracy, get_verdict_accuracy, save_strategy_note
    from notifications.telegram import send_message

    logger.info("[자기보정] 주간 적중률 분석 시작")
    try:
        condition_stats = get_condition_accuracy(days=30, min_count=3)
        verdict_stats = get_verdict_accuracy(days=30)
    except Exception as e:
        logger.warning(f"[자기보정] 통계 조회 실패: {e}")
        return

    if not condition_stats and not verdict_stats:
        logger.info("[자기보정] 데이터 부족 — 스킵")
        return

    # ── 판정별 요약 ──
    verdict_lines = []
    for v, s in sorted(verdict_stats.items()):
        hr = s.get("hit_rate_3d")
        avg3 = s.get("avg_3d")
        cnt = s.get("count", 0)
        verdict_lines.append(
            f"  [{v}] {cnt}건 | 적중률 {hr:.0f}% | 3일평균 {avg3:+.2f}%"
            if hr is not None and avg3 is not None
            else f"  [{v}] {cnt}건 (데이터 부족)"
        )

    # ── 조건별 저성과 ──
    low_perf = [c for c in condition_stats if (c.get("hit_rate_3d") or 0) < 40]
    low_lines = []
    for c in low_perf:
        low_lines.append(
            f"  ⚠️ {c['condition']} | {c['count']}건 | 적중률 {c['hit_rate_3d']:.0f}% | "
            f"3일평균 {c.get('avg_3d', 0):+.2f}%"
        )

    summary = f"주간 자기보정 — 판정 {len(verdict_stats)}종 / 저성과 조건 {len(low_perf)}개"
    detail_parts = ["## 판정별 적중률 (최근 30일)"]
    detail_parts.extend(verdict_lines or ["  데이터 없음"])
    if low_perf:
        detail_parts.append("\n## 저성과 조건 (적중률 40% 미만)")
        detail_parts.extend(low_lines)
    detail = "\n".join(detail_parts)

    try:
        save_strategy_note(category="general", summary=summary, detail=detail)
        logger.info(f"[자기보정] 전략 노트 저장 완료: {summary}")
    except Exception as e:
        logger.warning(f"[자기보정] 전략 노트 저장 실패: {e}")
        return

    # 텔레그램 발송
    try:
        msg_lines = [f"📊 *{summary}*", ""]
        msg_lines.extend(verdict_lines[:5])
        if low_perf:
            msg_lines.append("")
            msg_lines.extend(low_lines[:3])
        send_message("\n".join(msg_lines))
    except Exception as e:
        logger.debug(f"[자기보정] 텔레그램 발송 실패: {e}")


# 뉴스 위험 키워드 분류
_NEWS_CRITICAL = [
    "거래정지", "상장폐지", "감사의견거절", "횡령", "배임", "분식회계",
    "검찰 수사", "구속영장", "대표이사 구속",
]
_NEWS_WARNING = [
    "유상증자", "주주배정", "실적쇼크", "영업손실 전환", "영업정지",
    "대표이사 사임", "대표 교체", "주요주주 매도", "블록딜",
]

# 뉴스 알림 쿨다운: {stock_code: 마지막_알림_시각}
_news_alert_cooldown: dict = {}
_NEWS_COOLDOWN_HOURS = 4


def run_news_monitor():
    """보유 종목 뉴스 30분 주기 스캔 — 위험 키워드 감지 시 알림/exit 트리거."""
    from worker.clients.news_client import search_news, NAVER_CLIENT_ID
    from notifications.telegram import send_message

    if not NAVER_CLIENT_ID:
        return

    try:
        holdings = get_portfolio()
    except Exception as e:
        logger.warning(f"[뉴스모니터] 잔고 조회 실패: {e}")
        return

    if not holdings:
        return

    now = _now_kst()
    for h in holdings:
        code = str(h.get("stock_code", "")).strip()
        name = str(h.get("stock_name", "") or h.get("stk_nm", "")).strip()
        if not name or not code:
            continue

        # 쿨다운 체크 (같은 종목 4시간 이내 재알림 방지)
        last_alert = _news_alert_cooldown.get(code)
        if last_alert and (now - last_alert).total_seconds() < _NEWS_COOLDOWN_HOURS * 3600:
            continue

        try:
            news_items = search_news(name, display=5, sort="date")
            time.sleep(0.3)
        except Exception as e:
            logger.debug(f"[뉴스모니터] {name} 조회 실패: {e}")
            continue

        for item in news_items:
            full_text = f"{item.get('title', '')} {item.get('description', '')}".lower()
            title = item.get("title", "")[:80]
            pub = item.get("pub_date", "")

            critical_matched = [kw for kw in _NEWS_CRITICAL if kw in full_text]
            warning_matched  = [kw for kw in _NEWS_WARNING  if kw in full_text]

            if critical_matched:
                # CRITICAL: exit 신호 트리거 + AI 판단
                logger.warning(f"[뉴스모니터] CRITICAL 감지: {name} — {critical_matched}")
                _news_alert_cooldown[code] = now
                try:
                    from worker.monitor import Signal
                    from worker.claude_judge import get_trade_opinion
                    cur_data = kiwoom.get_current_price(code)
                    cur_price = abs(int(str(cur_data.get("cur_prc") or cur_data.get("stk_prpr") or "0").replace(",", "")))
                    fake_signal = Signal(
                        stock_code=code, stock_name=name,
                        current_price=cur_price,
                        triggered_conditions=[f"뉴스위험-{','.join(critical_matched)}"],
                        triggered_ids=["news_critical"],
                        rsi=None, volume_ratio=None, chart=None,
                        in_portfolio=True, signal_type="exit",
                    )
                    holdings_full = kiwoom.get_holdings()
                    opinion = get_trade_opinion(fake_signal, holdings_full, {}, {}, {})
                    signal_id = save_signal(fake_signal, opinion, in_portfolio=True)
                    _rag_index_signal(fake_signal, signal_id, opinion)
                    send_message(
                        f"🚨 *뉴스 위험 감지 — {name}*\n"
                        f"키워드: `{'`, `'.join(critical_matched)}`\n"
                        f"{title}\n_{pub}_\n\n"
                        f"🤖 *AI 판단*: {opinion.splitlines()[0] if opinion else '조회 실패'}"
                    )
                except Exception as _e:
                    logger.error(f"[뉴스모니터] {name} exit 트리거 실패: {_e}")
                    send_message(
                        f"🚨 *뉴스 위험 감지 — {name}*\n"
                        f"키워드: `{'`, `'.join(critical_matched)}`\n"
                        f"{title}\n_{pub}_"
                    )
                break  # 종목당 1건만 처리

            elif warning_matched:
                # WARNING: 알림만
                logger.info(f"[뉴스모니터] WARNING 감지: {name} — {warning_matched}")
                _news_alert_cooldown[code] = now
                send_message(
                    f"⚠️ *뉴스 주의 — {name}*\n"
                    f"키워드: `{'`, `'.join(warning_matched)}`\n"
                    f"{title}\n_{pub}_\n\n"
                    f"매도 여부는 직접 판단하세요."
                )
                break

    logger.debug(f"[뉴스모니터] {len(holdings)}개 종목 스캔 완료")


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
        cutoff_from = (_now_kst() - timedelta(days=days_before)).strftime("%Y-%m-%d")
        cutoff_to = (_now_kst() - timedelta(days=days_after)).strftime("%Y-%m-%d")
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
                    # 3d 결과 확정 시 FAISS 재인덱싱 (verdict + 수익률 포함)
                    if period_name == "3d":
                        try:
                            from worker.agents.tools.rag_tools import index_signal as _idx
                            import threading as _thr
                            with get_conn() as _conn:
                                _sig_row = _conn.execute(
                                    "SELECT stock_name, signal_type, verdict, triggered_conditions, "
                                    "dart_summary, news_summary, indicator_snapshot FROM signals WHERE id=?",
                                    (row["id"],)
                                ).fetchone()
                            if _sig_row:
                                _thr.Thread(
                                    target=_idx,
                                    kwargs={
                                        "signal_id": row["id"],
                                        "stock_name": _sig_row["stock_name"] or "",
                                        "signal_type": _sig_row["signal_type"] or "",
                                        "verdict": _sig_row["verdict"],
                                        "result_3d": round(pct, 2),
                                        "triggered_conditions": _sig_row["triggered_conditions"] or "",
                                        "dart_summary": _sig_row["dart_summary"],
                                        "news_summary": _sig_row["news_summary"],
                                        "indicator_snapshot": _sig_row["indicator_snapshot"],
                                    },
                                    daemon=True,
                                ).start()
                        except Exception:
                            pass
                time.sleep(0.5)
            except Exception as e:
                logger.warning(f"[결과 {period_name} 실패] {row['stock_name']}: {e}")


def update_paper_results():
    """모의투자 1일/3일/5일 수익률 업데이트."""
    from datetime import timedelta
    from data.db import get_conn, update_paper_result

    periods = [("1d", 1, 2, "result_1d"), ("3d", 3, 4, "result_3d"), ("5d", 5, 6, "result_5d")]
    for period_name, days_after, days_before, col_name in periods:
        cutoff_from = (_now_kst() - timedelta(days=days_before)).strftime("%Y-%m-%d")
        cutoff_to   = (_now_kst() - timedelta(days=days_after)).strftime("%Y-%m-%d")
        with get_conn() as conn:
            rows = conn.execute(
                f"SELECT id, stock_code, stock_name, price FROM paper_trades "
                f"WHERE {col_name} IS NULL AND created_at >= ? AND created_at < ?",
                (cutoff_from, cutoff_to),
            ).fetchall()
        for row in rows:
            try:
                pd = kiwoom.get_current_price(row["stock_code"])
                now_price = abs(int(str(pd.get("cur_prc") or pd.get("stk_prpr") or "0").replace(",", "")))
                if now_price and row["price"]:
                    pct = (now_price - row["price"]) / row["price"] * 100
                    update_paper_result(row["id"], round(pct, 2), period=period_name)
                    logger.debug(f"[모의투자 결과 {period_name}] {row['stock_name']}: {pct:+.2f}%")
                time.sleep(0.5)
            except Exception as e:
                logger.warning(f"[모의투자 결과 실패] {row['stock_name']}: {e}")


def update_screening_results():
    """스크리닝 종목의 7일/30일 후 수익률 자동 업데이트."""
    from datetime import timedelta
    from data.db import get_conn, update_screening_result

    periods = [
        ("7d", 7, 8),
        ("30d", 30, 31),
    ]

    for period_name, days_after, days_before in periods:
        cutoff_from = (_now_kst() - timedelta(days=days_before)).strftime("%Y-%m-%d")
        cutoff_to = (_now_kst() - timedelta(days=days_after)).strftime("%Y-%m-%d")
        col = "result_7d" if period_name == "7d" else "result_30d"
        with get_conn() as conn:
            rows = conn.execute(
                f"SELECT id, stock_code, stock_name, current_price "
                f"FROM screening_log "
                f"WHERE {col} IS NULL AND current_price IS NOT NULL AND created_at >= ? AND created_at < ?",
                (cutoff_from, cutoff_to),
            ).fetchall()

        for row in rows:
            try:
                pd = kiwoom.get_current_price(row["stock_code"])
                now_price = abs(int(str(
                    pd.get("cur_prc") or pd.get("stk_prpr") or "0"
                ).replace(",", "")))
                base = row["current_price"]
                if not base:
                    continue
                if now_price and base:
                    pct = (now_price - base) / base * 100
                    update_screening_result(row["id"], round(pct, 2), period=period_name)
                    logger.debug(f"[스크리닝 결과 {period_name}] {row['stock_name']} #{row['id']}: {pct:+.2f}%")
                time.sleep(0.5)
            except Exception as e:
                logger.warning(f"[스크리닝 결과 {period_name} 실패] {row['stock_name']}: {e}")


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
    _wm = _WORKER_CONFIG.get("watchlist_management", {})
    INACTIVE_DAYS = int(_wm.get("inactive_days_alert", 30))
    ALERT_INTERVAL_DAYS = int(_wm.get("alert_interval_days", 7))

    stocks = [s for s in get_watchlist() if s.get("enabled")]
    alerts = []

    for stock in stocks:
        code = stock["code"]
        name = stock["name"]

        # 주 1회 알림 쿨다운 체크
        key = f"{code}:inactive_alert"
        next_allowed_at = get_cooldown(key)
        if next_allowed_at and _now_kst() < next_allowed_at:
            continue

        # 마지막 신호 날짜 조회
        with get_conn() as conn:
            row = conn.execute(
                "SELECT MAX(created_at) as last_signal FROM signals WHERE stock_code = ?",
                (code,)
            ).fetchone()

        last_signal = row["last_signal"] if row and row["last_signal"] else None
        if last_signal:
            days_since = (_now_kst() - datetime.strptime(last_signal[:10], "%Y-%m-%d")).days
        else:
            days_since = 999

        if days_since >= INACTIVE_DAYS:
            alerts.append((name, days_since))
            set_cooldown(key, cooldown_minutes=ALERT_INTERVAL_DAYS * 24 * 60)

    if alerts:
        lines = "\n".join(f"  • {name}: {days}일째 신호 없음" for name, days in alerts)
        send_message(f"⚠️ *장기 미발동 종목 알림*\n\n{lines}\n\n_조건 검토 또는 모니터링 해제 고려_")
        logger.info(f"[미발동 알림] {len(alerts)}개 종목: {[n for n, _ in alerts]}")


def check_removal_candidates():
    """미보유 종목 중 90일 미신호 → 관심종목 자동 삭제."""
    _wm = _WORKER_CONFIG.get("watchlist_management", {})
    INACTIVE_DAYS = int(_wm.get("inactive_days_removal", 90))
    ALERT_INTERVAL_DAYS = int(_wm.get("alert_interval_days", 7))

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
        next_allowed_at = get_cooldown(key)
        if next_allowed_at and _now_kst() < next_allowed_at:
            continue

        last_signal_dt = get_last_signal_date(code)
        if last_signal_dt:
            days_since = (_now_kst() - last_signal_dt).days
        else:
            # 신호 이력 없으면 등록일 기준 (등록일도 없으면 삭제 안 함)
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
            delete_stock(code)
            set_cooldown(key, cooldown_minutes=ALERT_INTERVAL_DAYS * 24 * 60)
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


def _paper_execute(signal, claude_opinion: str, signal_id: int | None) -> None:
    """AUTO_TRADE=false 시 AI 판단을 모의투자 기록으로 저장 (실제 주문 없음)."""
    import re
    first_line = claude_opinion.strip().splitlines()[0] if claude_opinion.strip() else ""
    if "[매수]" in first_line or "[추가매수" in first_line or "[물타기" in first_line:
        order_type, side = "buy", "매수"
    elif "[매도]" in first_line:
        order_type, side = "sell", "매도"
    else:
        return

    qty = None
    for line in claude_opinion.splitlines():
        if line.strip().startswith("[추천수량]"):
            m = re.search(r"(\d+)\s*주", line)
            if m:
                qty = int(m.group(1))
                break
    if not qty:
        return

    from data.db import save_paper_trade, extract_verdict
    verdict = extract_verdict(claude_opinion)
    paper_id = save_paper_trade(
        stock_code=signal.stock_code,
        stock_name=signal.stock_name,
        order_type=order_type,
        quantity=qty,
        price=signal.current_price,
        signal_id=signal_id,
        verdict=verdict,
    )
    logger.info(f"[모의투자] {signal.stock_name} {side} {qty}주 @ {signal.current_price:,}원 기록 (paper_id={paper_id})")
    send_message(
        f"📝 *모의투자 기록* (실제 주문 없음)\n"
        f"종목: *{signal.stock_name}* | {side} {qty:,}주\n"
        f"AI 판단: {first_line[:60]}"
    )


def _auto_execute(signal, claude_opinion: str, signal_id: int | None, deposit: int = 0, buy_budget: int = 0) -> None:
    """AI 판단이 매수/매도이고 추천수량이 있으면 자동 주문 실행. 추천수량 없으면 홀드."""
    import re
    first_line = claude_opinion.strip().splitlines()[0] if claude_opinion.strip() else ""
    if "[매수]" in first_line or "[추가매수" in first_line or "[물타기" in first_line:
        order_type, side = "1", "매수"
    elif "[매도]" in first_line:
        order_type, side = "2", "매도"
    else:
        logger.info(f"[{signal.stock_name}] 자동 모드: AI 홀드 — 스킵")
        return

    qty = None
    order_market = None
    for line in claude_opinion.splitlines():
        if line.strip().startswith("[추천수량]"):
            m = re.search(r"(\d+)\s*주", line)
            if m:
                qty = int(m.group(1))
                break
    for line in claude_opinion.splitlines():
        if line.strip().startswith("[주문시장]"):
            m = re.search(r"(KRX|NXT|SOR)", line.upper())
            if m:
                order_market = m.group(1)
            break

    if not qty:
        logger.info(f"[{signal.stock_name}] 자동 모드: 추천수량 없음 — 홀드")
        return

    # 하드캡: 매수 — 실질 매수 여력 초과 방지
    if order_type == "1" and signal.current_price > 0:
        budget = buy_budget if buy_budget > 0 else deposit
        max_qty = budget // signal.current_price
        if qty > max_qty:
            logger.warning(
                f"[{signal.stock_name}] 추천수량 {qty}주 → {max_qty}주로 조정 "
                f"(매수여력 {budget:,}원 / 현재가 {signal.current_price:,}원)"
            )
            qty = max_qty
        if qty <= 0:
            logger.info(f"[{signal.stock_name}] 매수 여력 부족으로 스킵")
            return

    # 하드캡: 매도 — 보유 수량 초과 방지
    if order_type == "2":
        holdings = get_portfolio()
        held_qty = next(
            (int(h.get("quantity") or 0) for h in holdings
             if str(h.get("stock_code", "")) == signal.stock_code),
            0,
        )
        if held_qty <= 0:
            logger.info(f"[{signal.stock_name}] 미보유 종목 매도 스킵")
            return
        if qty > held_qty:
            logger.warning(
                f"[{signal.stock_name}] 매도 추천수량 {qty}주 → 보유수량 {held_qty}주로 조정"
            )
            qty = held_qty

    try:
        result = kiwoom.place_order(signal.stock_code, order_type, qty, order_market=order_market)
        ord_no = result.get("ord_no") or result.get("order_no") or "-"
        logger.info(
            f"[{signal.stock_name}] 자동 {side}: {qty}주, 주문시장 {order_market or '기본값'}, 주문번호 {ord_no}"
        )
        send_message(
            f"🤖 *자동 {side} 주문 접수*\n"
            f"종목: *{signal.stock_name}* (`{signal.stock_code}`)\n"
            f"수량: *{qty:,}주* (시장가)\n"
            f"주문시장: *{order_market or '기본값'}*\n"
            f"주문번호: `{ord_no}`"
        )
        # 모의투자: 체결내역 API 미지원 → trades 테이블에 직접 기록
        if getattr(kiwoom, "_is_mock", False):
            try:
                from data.db import insert_trade_direct
                insert_trade_direct(
                    trade_id=ord_no,
                    stock_code=signal.stock_code,
                    stock_name=signal.stock_name,
                    side=side,
                    quantity=qty,
                    price=0,  # 시장가 주문, 실제 체결가는 수동 입력
                )
            except Exception as _e:
                logger.warning(f"[{signal.stock_name}] 모의투자 체결 DB 저장 실패: {_e}")

        from data.db import reset_cooldowns_for_stock, update_signal_action, save_strategy_note, set_add_cooldown_after_trade
        reset_cooldowns_for_stock(signal.stock_code)
        if order_type == "1":
            set_add_cooldown_after_trade(signal.stock_code)
            # 매수 직후 1시간 동안 entry 신호 재발동 방지
            _post_buy_lock[signal.stock_code] = _now_kst() + _td(hours=1)
            # 매수 후 포지션 자동 생성 (portfolio_sync에서 정확한 평단가로 갱신됨)
            create_position_from_trade(signal.stock_code, signal.stock_name, signal.current_price, qty)
        if signal_id is not None:
            update_signal_action(signal_id, side)
        save_strategy_note("trade", f"{signal.stock_name} {qty}주 {side} (자동 매매)")
        from worker.portfolio_sync import sync_all as _sync
        _sync(kiwoom)

        # 매수 후 AI 포지션 판단 (목표가/손절가/추가매수가 설정)
        if order_type == "1":
            _set_position_by_ai(signal.stock_code, signal.stock_name, signal.current_price, qty)
    except Exception as e:
        logger.error(f"[{signal.stock_name}] 자동 주문 실패: {e}")
        send_message(f"❌ 자동 주문 실패: *{signal.stock_name}* — `{e}`")


def check_market_dip():
    """시장 급락 감지 시 watchlist 미보유 종목 전체를 AI로 반등 매수 평가."""
    if not AUTO_TRADE:
        return
    session = "main" if TEST_MODE else get_current_session()
    if session != "main":
        return

    try:
        kospi = kiwoom.get_market_index("kospi")
        time.sleep(0.5)
        kosdaq = kiwoom.get_market_index("kosdaq")
    except Exception as e:
        logger.warning(f"[급락 감지] 지수 조회 실패: {e}")
        return

    def _rate(d: dict) -> float:
        try:
            return float(str(d.get("flu_rt") or d.get("prdy_ctrt") or "0").replace(",", ""))
        except (ValueError, TypeError):
            return 0.0

    kospi_rate = _rate(kospi)
    kosdaq_rate = _rate(kosdaq)
    threshold = float(_WORKER_CONFIG.get("dip_buy_threshold", -1.5))

    if kospi_rate > threshold and kosdaq_rate > threshold:
        return  # 급락 아님

    # 쿨다운 (설정된 시간마다 최대 1회)
    cooldown_hours = int(_WORKER_CONFIG.get("dip_buy_cooldown_hours", 2))
    now_kst = _now_kst()
    dip_key = f"dip_buy:{now_kst.strftime('%Y-%m-%d-%H')}"
    next_allowed_at = get_cooldown(dip_key)
    if next_allowed_at and now_kst < next_allowed_at:
        return
    set_cooldown(dip_key, cooldown_minutes=cooldown_hours * 60)

    logger.info(f"[시장 급락] KOSPI {kospi_rate:+.2f}% / KOSDAQ {kosdaq_rate:+.2f}% — 반등 매수 스캔 시작")
    send_message(
        f"📉 *시장 급락 감지*\n"
        f"KOSPI {kospi_rate:+.2f}% / KOSDAQ {kosdaq_rate:+.2f}%\n"
        f"watchlist 미보유 종목 반등 매수 후보 스캔 중..."
    )

    stocks = [s for s in get_watchlist() if s.get("enabled", False)]
    holdings = get_portfolio()
    holding_codes = {str(h.get("stock_code", "")) for h in holdings}
    candidates = [s for s in stocks if s["code"] not in holding_codes]

    if not candidates:
        send_message("📊 급락 스캔: 미보유 watchlist 종목 없음")
        return

    # 예수금 + 매수 여력 계산
    deposit = 0
    try:
        deposit_info = kiwoom.get_deposit()
        deposit = deposit_info.get("order_available", 0)
    except Exception as e:
        logger.warning(f"[급락 스캔] 예수금 조회 실패: {e}")

    buy_budget = deposit
    try:
        from data.db import get_positions as _gp
        _positions = {p["stock_code"]: p for p in _gp()}
        _reserve = 0
        for h in holdings:
            _code = str(h.get("stock_code", ""))
            _pos = _positions.get(_code)
            if _pos and _pos.get("add_buy_price"):
                _reserve += _pos["add_buy_price"] * int(int(h.get("quantity") or 0) * 0.5)
            else:
                _reserve += int((h.get("eval_amount") or 0) * 0.15)
        buy_budget = max(0, deposit - int(_reserve))
    except Exception:
        pass

    total_eval = sum(int(h.get("eval_amount") or 0) for h in holdings)
    total_portfolio = total_eval + deposit

    from worker.monitor import Signal, _parse_price
    from worker.indicators import calculate_rsi, calculate_chart_summary

    max_buys = int(_WORKER_CONFIG.get("dip_buy_max_stocks", 2))
    bought = 0
    results = []

    for stock in candidates:
        if bought >= max_buys:
            break

        code = stock["code"]
        name = stock.get("name", code)
        time.sleep(1)

        try:
            price_data = kiwoom.get_current_price(code)
            current_price = _parse_price(
                price_data.get("cur_prc") or price_data.get("stk_prpr") or price_data.get("prpr")
            )
            if not current_price:
                continue

            daily_data = kiwoom.get_daily_ohlcv(code, period=90)
            closes, highs, lows, opens, vols = [], [], [], [], []
            for d in daily_data:
                cp = _parse_price(d.get("cur_prc"))
                hp = _parse_price(d.get("high_pric"))
                lp = _parse_price(d.get("lwst_pric") or d.get("low_pric"))
                op = _parse_price(d.get("strt_pric") or d.get("opn_pric"))
                vl = _parse_price(d.get("trde_qty"))
                if cp: closes.append(cp)
                if hp: highs.append(hp)
                if lp: lows.append(lp)
                if op: opens.append(op)
                if vl: vols.append(vl)

            rsi = calculate_rsi(closes) if len(closes) >= 15 else None
            chart = calculate_chart_summary(
                closes, highs, current_price,
                low_prices=lows, open_prices=opens, volumes=vols,
            ) if len(closes) >= 5 else None

            opinion = get_dip_buy_opinion(
                stock=stock,
                current_price=current_price,
                rsi=rsi,
                chart=chart,
                kospi_rate=kospi_rate,
                kosdaq_rate=kosdaq_rate,
                deposit=deposit,
                buy_budget=buy_budget,
                total_portfolio=total_portfolio,
                holdings=holdings,
            )

            first_line = opinion.strip().splitlines()[0] if opinion.strip() else ""
            logger.info(f"[급락 스캔] {name}: {first_line[:80]}")

            if "[매수]" in first_line:
                fake_signal = Signal(
                    stock_code=code,
                    stock_name=name,
                    current_price=current_price,
                    triggered_conditions=["시장 급락 반등 매수 (AI 판단)"],
                    triggered_ids=["dip_buy"],
                    rsi=rsi,
                    volume_ratio=None,
                    chart=chart,
                    in_portfolio=False,
                    signal_type="entry",
                )
                signal_id = save_signal(fake_signal, opinion, in_portfolio=False)
                _rag_index_signal(fake_signal, signal_id, opinion)
                _auto_execute(fake_signal, opinion, signal_id, deposit=deposit, buy_budget=buy_budget)
                bought += 1
                results.append(f"✅ {name} 매수")
            else:
                results.append(f"⏭ {name} 패스")

        except Exception as e:
            logger.error(f"[급락 스캔] {name} 오류: {e}")
            results.append(f"❌ {name} 오류")

    send_message(f"📊 *급락 스캔 완료* ({bought}종목 매수)\n" + "\n".join(results))


def _set_position_by_ai(stock_code: str, stock_name: str, current_price: int, qty: int):
    """매수 체결 후 AI가 포지션 관리값 판단 → positions 테이블 업데이트 + 텔레그램 알림."""
    try:
        # portfolio_sync로 갱신된 평단가 사용
        from data.db import get_position
        pos = get_position(stock_code)
        avg_price = pos["avg_price"] if pos and pos.get("avg_price") else current_price

        result = judge_position_values(stock_code, stock_name, avg_price, qty, current_price)
        if not result:
            logger.warning(f"[{stock_name}] AI 포지션 판단 실패 — 기본값 유지")
            return

        tp = result.get("target_price", 0)
        sl = result.get("stop_loss_price", 0)
        ab = result.get("add_buy_price", 0)

        if tp:
            update_position_field(stock_code, "target_price", tp)
        if sl:
            update_position_field(stock_code, "stop_loss_price", sl)
        if ab:
            update_position_field(stock_code, "add_buy_price", ab)

        # 전략 노트 기록
        detail_parts = []
        if tp:
            detail_parts.append(f"목표가 {tp:,}원 — {result.get('target_reason', '')}")
        if sl:
            detail_parts.append(f"손절가 {sl:,}원 — {result.get('stop_loss_reason', '')}")
        if ab:
            detail_parts.append(f"추가매수가 {ab:,}원 — {result.get('add_buy_reason', '')}")

        save_strategy_note(
            "watchlist",
            f"{stock_name} 포지션 AI 설정 (평단 {avg_price:,}원)",
            "\n".join(detail_parts),
        )

        # 텔레그램 알림
        msg_lines = [f"📌 *{stock_name}* 포지션 AI 설정\n평단 *{avg_price:,}원* | {qty}주\n"]
        if tp:
            tp_pct = (tp - avg_price) / avg_price * 100
            msg_lines.append(f"• 목표가 *{tp:,}원* ({tp_pct:+.1f}%) — {result.get('target_reason', '')}")
        if sl:
            sl_pct = (sl - avg_price) / avg_price * 100
            msg_lines.append(f"• 손절가 *{sl:,}원* ({sl_pct:+.1f}%) — {result.get('stop_loss_reason', '')}")
        if ab:
            ab_pct = (ab - avg_price) / avg_price * 100
            msg_lines.append(f"• 추가매수 *{ab:,}원* ({ab_pct:+.1f}%) — {result.get('add_buy_reason', '')}")
        if tp and sl:
            upside = (tp - avg_price) / avg_price * 100
            downside = (avg_price - sl) / avg_price * 100
            rr = upside / downside if downside else 0
            msg_lines.append(f"\nR/R *{rr:.1f}:1*")

        send_message("\n".join(msg_lines))
        logger.info(f"[{stock_name}] AI 포지션 설정: 목표={tp:,} 손절={sl:,} 추매={ab:,}")

    except Exception as e:
        logger.error(f"[{stock_name}] AI 포지션 설정 실패: {e}")


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

    deposit = 0
    try:
        deposit_info = kiwoom.get_deposit()
        deposit = deposit_info.get("order_available", 0)
    except Exception as e:
        logger.warning(f"예수금 조회 실패: {e}")

    # 물타기 예비금 계산 → 실질 매수 여력 산출 (하드캡 기준)
    buy_budget = deposit
    try:
        from data.db import get_positions as _get_positions
        _positions = {p["stock_code"]: p for p in _get_positions()}
        _add_reserve = 0
        for h in holdings:
            code = str(h.get("stock_code", ""))
            pos = _positions.get(code)
            if pos and pos.get("add_buy_price"):
                qty = int(h.get("quantity") or 0)
                _add_reserve += pos["add_buy_price"] * int(qty * 0.5)
            else:
                _add_reserve += int((h.get("eval_amount") or 0) * 0.15)
        buy_budget = max(0, deposit - int(_add_reserve))
    except Exception as e:
        logger.warning(f"물타기 예비금 계산 실패, deposit 전액 사용: {e}")

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

            # 매수 직후 entry 재발동 방지
            if signal.stock_code in _post_buy_lock:
                if _now_kst() < _post_buy_lock[signal.stock_code]:
                    if not signal.in_portfolio:
                        logger.info(f"[{signal.stock_name}] 최근 매수 후 entry 재발동 방지 — 스킵")
                        continue
                else:
                    del _post_buy_lock[signal.stock_code]

            signal.triggered_conditions = new_conditions
            signal.triggered_ids = new_ids
            logger.info(f"[{signal.stock_name}] 신호 감지: {new_conditions}")

            claude_opinion = None
            if use_claude:
                try:
                    sector = kiwoom.get_sector_index(signal.sector_code) if signal.sector_code else {}
                    claude_opinion = get_trade_opinion(signal, holdings, kospi, kosdaq, sector, signal.recent_trades, deposit=deposit)
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

            # RAG용: 뉴스 요약
            news_summary = None
            try:
                from worker.clients.news_client import format_news_for_ai, NAVER_CLIENT_ID
                if NAVER_CLIENT_ID:
                    news_summary = format_news_for_ai(signal.stock_name, max_items=5)
            except Exception:
                pass

            # RAG용: 시장 스냅샷
            market_snapshot = None
            try:
                parts = []
                if kospi:
                    parts.append(f"KOSPI {kospi.get('cur_prc','?')} ({kospi.get('flu_rt','?')}%)")
                if kosdaq:
                    parts.append(f"KOSDAQ {kosdaq.get('cur_prc','?')} ({kosdaq.get('flu_rt','?')}%)")
                if parts:
                    market_snapshot = " / ".join(parts)
            except Exception:
                pass

            # RAG용: 포트폴리오 스냅샷
            portfolio_snapshot = None
            try:
                portfolio_snapshot = f"보유 {len(holdings)}종목"
            except Exception:
                pass

            mark_sent(signal.stock_code, new_ids)
            signal_id = save_signal(
                signal, claude_opinion, in_portfolio=signal.in_portfolio,
                dart_summary=dart_summary, news_summary=news_summary,
                market_snapshot=market_snapshot, portfolio_snapshot=portfolio_snapshot,
            )
            # Agent 모드 실행 시 tool_sequence + reasoning_chain 저장
            if claude_opinion:
                try:
                    trace = get_last_agent_trace()
                    if trace.get("tool_sequence"):
                        update_signal_agent_trace(
                            signal_id,
                            trace["tool_sequence"],
                            trace.get("reasoning_chain", []),
                        )
                except Exception as _e:
                    logger.debug(f"[AgentTrace] 저장 실패: {_e}")
            _rag_index_signal(signal, signal_id, claude_opinion,
                              dart_summary=dart_summary, news_summary=news_summary)
            send_signal_alert(signal, claude_opinion, holdings=holdings, signal_id=signal_id, auto_mode=AUTO_TRADE)

            if claude_opinion:
                _maybe_save_hold_conditions(signal, claude_opinion)

            if claude_opinion:
                if AUTO_TRADE:
                    _auto_execute(signal, claude_opinion, signal_id, deposit=deposit, buy_budget=buy_budget)
                else:
                    _paper_execute(signal, claude_opinion, signal_id)

    logger.info("=== 조건 체크 완료 ===")


def main():
    interval = _WORKER_CONFIG.get("interval_seconds", 60)
    init_db()

    logger.info("포트폴리오 초기 동기화 중...")
    sync_all(kiwoom)

    logger.info("텔레그램 봇 시작...")
    start_bot_thread(kiwoom_client=kiwoom)

    interval_min = max(1, interval // 60)
    _trade_env = "모의투자" if kiwoom._is_mock else "실전투자"
    _trade_mode = "자동매매" if AUTO_TRADE else "수동(알림)"
    logger.info(f"워커 시작 - {interval}초 간격으로 실행 (평일 08:00~18:00)")
    send_message(f"✅ 워커 시작 [{_trade_env} | {_trade_mode}]")

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
    scheduler.add_job(update_screening_results, "cron",
                      day_of_week="mon-fri", hour="9-18", minute="*/30",
                      id="screening_result_update")
    scheduler.add_job(check_trailing_stops, "cron",
                      day_of_week="mon-fri", hour="9-15", minute="*/30",
                      id="trailing_stops")
    scheduler.add_job(lambda: reassess_watchlist(kiwoom), "cron",
                      day_of_week="mon-fri", hour=9, minute=15,
                      id="reassess_watchlist")
    scheduler.add_job(check_inactive_stocks, "cron",
                      day_of_week="mon-fri", hour=8, minute=30,
                      id="inactive_alert")
    scheduler.add_job(check_removal_candidates, "cron",
                      day_of_week="mon-fri", hour="9-15", minute="*/30",
                      id="removal_check")
    scheduler.add_job(check_market_dip, "cron",
                      day_of_week="mon-fri", hour="9-14", minute="*/30",
                      id="dip_buy")
    scheduler.add_job(run_intraday_scan, "cron",
                      day_of_week="mon-fri", hour=11, minute=0,
                      id="intraday_scan")
    scheduler.add_job(run_daily_screening, "cron",
                      day_of_week="mon-fri", hour=15, minute=40,
                      id="daily_screening")
    scheduler.add_job(run_daily_review, "cron",
                      day_of_week="mon-fri", hour=16, minute=10,
                      id="daily_review")
    scheduler.add_job(run_weekly_performance_report, "cron",
                      day_of_week="mon", hour=9, minute=0,
                      id="weekly_performance_report")
    scheduler.add_job(run_weekly_self_correction, "cron",
                      day_of_week="mon", hour=9, minute=5,
                      id="weekly_self_correction")
    scheduler.add_job(update_paper_results, "cron",
                      day_of_week="mon-fri", hour="9-18", minute="*/30",
                      id="paper_result_update")
    scheduler.add_job(run_news_monitor, "cron",
                      day_of_week="mon-fri", hour="9-15", minute="*/30",
                      id="news_monitor")
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
    parser.add_argument("--env", type=str, default=None, help=".env 파일 경로 (예: .env.real)")
    args = parser.parse_args()

    # if args.test:
    TEST_MODE = True
    logger.info("=== 테스트 모드 ===")

    from data.db import DB_PATH as _db_path
    _trade_env = "모의투자" if kiwoom._is_mock else "실전투자"
    _trade_mode = "자동매매" if AUTO_TRADE else "수동(알림)"
    logger.info(f"========== 워커 초기화 ==========")
    logger.info(f"환경: {_trade_env} | 모드: {_trade_mode}")
    logger.info(f"ENV: {_env_file}")
    logger.info(f"DB:  {_db_path}")
    logger.info(f"==================================")
    main()
