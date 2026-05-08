"""
백그라운드 워커 메인 진입점
- 장 운영 시간 동안 주기적으로 종목 조건 체크
- 조건 충족 시 Claude API 판단 → 텔레그램 알림
- 종목/조건 설정은 DB에서 실시간 로드 (변경 즉시 반영)
"""

import argparse
import json
import logging
import logging.handlers
import os
import re
import signal
import sys
import threading
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

# Force process timezone for localtime-based log rotation.
# TimedRotatingFileHandler(when="midnight") uses local time.
_log_tz = os.getenv("LOG_TZ", "Asia/Seoul").strip() or "Asia/Seoul"
os.environ["TZ"] = _log_tz
if hasattr(time, "tzset"):
    try:
        time.tzset()
    except Exception:
        pass

from worker.clients.kiwoom_client import KiwoomClient
from worker.monitor import check_stock, load_conditions
from worker.claude_judge import (
    get_trade_opinion,
    judge_position_values,
    get_dip_buy_opinion,
    get_news_risk_assessment,
    get_last_agent_trace,
    get_judgment_runtime_meta,
)
from worker.cooldown import filter_new_conditions, mark_sent
from worker.stock_analyzer import run_daily_screening, run_intraday_scan, run_daily_review, reassess_watchlist
from worker.portfolio_sync import sync_all
from notifications.telegram import send_signal_alert, send_message
from notifications.telegram_bot import start_bot_thread
from data.db import (init_db, save_signal, get_portfolio, get_watchlist, reset_all_cooldowns,
                     update_signal_result, update_signal_agent_trace, save_agent_action_log, purge_agent_action_logs,
                     update_stock_field, save_strategy_note, save_market_report, get_latest_market_report,
                     get_cooldown, set_cooldown, get_last_signal_date, delete_stock,
                     get_positions, get_position, update_position_field, create_position_from_trade,
                     get_recent_daily_reviews,
                     save_realized_pnl_snapshot)

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
_ai_judgment_cache: dict[str, tuple[datetime, str]] = {}
_rag_pending_signal_ids: set[int] = set()
_pending_weak_exit_confirm: dict[str, datetime] = {}
_shutdown_event = threading.Event()
_daily_review_guardrail_context: dict = {}
_daily_review_execution_events: list[dict] = []
_daily_review_event_date: str = ""


def _handle_shutdown_signal(signum, _frame):
    logger.info(f"[종료신호] signal={signum}")
    _shutdown_event.set()

# KRX 거래 세션 (규정값)
_SESSIONS: dict[str, tuple[dtime, dtime]] = {
    "premarket":  (dtime(8, 30),  dtime(9, 0)),    # 장전 시간외 (trde_tp 61)
    "main":       (dtime(9, 0),   dtime(15, 30)),   # 정규장 (trde_tp 0/3)
    "aftermarket":(dtime(15, 40), dtime(16, 0)),    # 장후 시간외 (trde_tp 81)
    "offhours":   (dtime(16, 0),  dtime(18, 0)),    # 시간외 단일가 (trde_tp 62)
}

_MARKET_CALENDAR_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "krx_holidays.yaml")
_MARKET_CALENDAR_CACHE: tuple[float, set[str]] | None = None


def _load_krx_holiday_dates() -> set[str]:
    """Load KRX holiday dates (YYYY-MM-DD) from config file."""
    global _MARKET_CALENDAR_CACHE
    try:
        mtime = os.path.getmtime(_MARKET_CALENDAR_PATH)
    except Exception:
        mtime = -1.0
    if _MARKET_CALENDAR_CACHE and _MARKET_CALENDAR_CACHE[0] == mtime:
        return _MARKET_CALENDAR_CACHE[1]

    dates: set[str] = set()
    try:
        with open(_MARKET_CALENDAR_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        for item in (data.get("closed_dates") or []):
            text = str(item or "").strip()
            if text:
                dates.add(text)
    except Exception as e:
        logger.warning(f"[market-calendar] KRX holiday config load failed: {e}")

    _MARKET_CALENDAR_CACHE = (mtime, dates)
    return dates


def _is_krx_closed_day(dt: datetime) -> tuple[bool, str]:
    """Return (closed, reason) for a KST date."""
    if dt.weekday() >= 5:
        return True, "weekend"
    ymd = dt.strftime("%Y-%m-%d")
    if ymd in _load_krx_holiday_dates():
        return True, "holiday_calendar"
    return False, "open_day"


def _get_market_event_context(now_kst: datetime | None = None) -> dict:
    """System-level market event flags used by execution and AI context."""
    now_kst = now_kst or _now_kst()
    tomorrow = now_kst + _td(days=1)
    today_closed, today_reason = _is_krx_closed_day(now_kst)
    tomorrow_closed, tomorrow_reason = _is_krx_closed_day(tomorrow)
    return {
        "today": now_kst.strftime("%Y-%m-%d"),
        "tomorrow": tomorrow.strftime("%Y-%m-%d"),
        "today_closed": today_closed,
        "today_closed_reason": today_reason,
        "tomorrow_closed": tomorrow_closed,
        "tomorrow_closed_reason": tomorrow_reason,
    }


def _run_on_open_day(job_name: str, fn, *args, **kwargs):
    """Run scheduled job only when today is an open KRX day."""
    ctx = _get_market_event_context()
    if ctx.get("today_closed"):
        logger.info(
            f"[market-event] today={ctx.get('today')} closed "
            f"(reason={ctx.get('today_closed_reason')}) - {job_name} skip"
        )
        return None
    return fn(*args, **kwargs)


def _build_core3_from_daily_review(review: dict | None) -> list[str]:
    """Build fixed 3-line summary from latest daily_review meta/guidance."""
    if not review:
        return []
    lines: list[str] = []
    date = str(review.get("created_at", ""))[:10] or "N/A"
    lines.append(f"{date} 복기 반영")
    try:
        meta = json.loads(review.get("meta_json") or "{}")
    except Exception:
        meta = {}
    guidance = (((meta.get("next_day_policy") or {}).get("agent_guidance")) or [])
    if isinstance(guidance, list) and guidance:
        for g in guidance[:2]:
            txt = str(g or "").strip()
            if txt:
                lines.append(txt[:120])
    while len(lines) < 3:
        lines.append("복기 지침 없음")
    return lines[:3]


def _resolve_guardrails_from_daily_review(review: dict | None) -> dict:
    """Infer intraday guardrails from latest daily_review guidance."""
    policy = {
        "source_date": "",
        "allow_new_entry": True,
        "qty_multiplier": 1.0,
        "stop_loss_sensitivity": "유지",  # 완화/유지/강화
        "reason": "default",
        "daily_review_core3": _build_core3_from_daily_review(review),
    }
    if not review:
        policy["reason"] = "no_daily_review"
        return policy

    policy["source_date"] = str(review.get("created_at", ""))[:10]
    try:
        meta = json.loads(review.get("meta_json") or "{}")
    except Exception:
        meta = {}
    guidance = (((meta.get("next_day_policy") or {}).get("agent_guidance")) or [])
    gtext = " ".join(str(x or "") for x in guidance).lower()

    reasons: list[str] = []
    if any(k in gtext for k in ("신규진입 금지", "신규 진입 금지", "신규매수 금지", "신규 매수 금지", "신규진입 차단", "신규 매수 차단")):
        policy["allow_new_entry"] = False
        reasons.append("entry_block")

    m = re.search(r"(0\.\d+|1\.0)\s*배", gtext)
    if m:
        try:
            q = float(m.group(1))
            policy["qty_multiplier"] = min(1.0, max(0.1, q))
            reasons.append(f"qty={policy['qty_multiplier']:.2f}")
        except Exception:
            pass
    elif any(k in gtext for k in ("수량 축소", "비중 축소", "포지션 축소", "보수적으로")):
        policy["qty_multiplier"] = 0.7
        reasons.append("qty=0.70")

    if any(k in gtext for k in ("손절 강화", "손절 민감도 강화", "손절 타이트", "손절 엄격")):
        policy["stop_loss_sensitivity"] = "강화"
        reasons.append("sl=강화")
    elif any(k in gtext for k in ("손절 완화", "손절 민감도 완화", "손절 완충")):
        policy["stop_loss_sensitivity"] = "완화"
        reasons.append("sl=완화")

    policy["reason"] = ", ".join(reasons) if reasons else "guidance_parsed_no_override"
    return policy


def _log_daily_review_event(stock_code: str, stock_name: str, status: str, reason: str, extra: dict | None = None) -> None:
    event = {
        "time": _now_kst().strftime("%Y-%m-%d %H:%M:%S"),
        "stock_code": stock_code,
        "stock_name": stock_name,
        "status": status,  # applied / ignored / skipped
        "reason": reason,
        "extra": extra or {},
    }
    _daily_review_execution_events.append(event)
    try:
        src = (_daily_review_guardrail_context or {}).get("source_date") or "N/A"
    except Exception:
        src = "N/A"
    logger.info(
        "[daily_review][event] "
        f"source_daily_review_date={src} "
        f"applied_or_ignored={status} "
        f"reason={reason} "
        f"stock={stock_name}({stock_code}) "
        f"extra={event.get('extra', {})}"
    )


def _save_daily_review_execution_checklist() -> int:
    """Persist end-of-day checklist: applied/ignored with reasons."""
    if not _daily_review_guardrail_context:
        logger.info("[daily_review] checklist skip: empty guardrail context")
        return 0
    today = _now_kst().strftime("%Y-%m-%d")
    applied = [e for e in _daily_review_execution_events if e.get("status") == "applied"]
    ignored = [e for e in _daily_review_execution_events if e.get("status") == "ignored"]
    summary = f"{today} daily_review 반영 체크리스트"
    core3 = _daily_review_guardrail_context.get("daily_review_core3") or []
    lines = [
        "[Checklist Summary]",
        f"- source_date: {_daily_review_guardrail_context.get('source_date') or 'N/A'}",
        f"- allow_new_entry: {_daily_review_guardrail_context.get('allow_new_entry')}",
        f"- qty_multiplier: {_daily_review_guardrail_context.get('qty_multiplier')}",
        f"- stop_loss_sensitivity: {_daily_review_guardrail_context.get('stop_loss_sensitivity')}",
        f"- applied: {len(applied)} / ignored: {len(ignored)} / total_events: {len(_daily_review_execution_events)}",
        "[Daily Review Core3]",
    ]
    for c in core3[:3]:
        lines.append(f"- {c}")
    lines.append("[Applied]")
    if applied:
        for e in applied[:30]:
            lines.append(f"- {e['time']} {e['stock_name']}({e['stock_code']}): {e['reason']}")
    else:
        lines.append("- 없음")
    lines.append("[Ignored]")
    if ignored:
        for e in ignored[:30]:
            lines.append(f"- {e['time']} {e['stock_name']}({e['stock_code']}): {e['reason']}")
    else:
        lines.append("- 없음")
    detail = "\n".join(lines)
    try:
        note_id = save_strategy_note(
            "general",
            summary,
            detail,
            meta={
                "type": "daily_review_execution_checklist",
                "review_source_date": _daily_review_guardrail_context.get("source_date"),
                "guardrails": _daily_review_guardrail_context,
                "events": _daily_review_execution_events,
            },
        )
        logger.info(f"[daily_review] execution checklist saved note_id={note_id} summary='{summary}'")
        return int(note_id or 0)
    except Exception as e:
        logger.warning(f"[daily_review] execution checklist save failed: {e}")
        return 0


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


def _safe_int_price(value) -> int:
    try:
        return abs(int(str(value or "0").replace(",", "").strip()))
    except Exception:
        return 0


def _safe_float(value) -> float:
    try:
        return float(str(value or "").replace(",", "").strip())
    except Exception:
        return 0.0


def _extract_change_pct(payload: dict | None) -> float:
    if not payload:
        return 0.0
    for key in ("prdy_ctrt", "flu_rt", "change_rate", "chg_rt"):
        if key in payload:
            v = _safe_float(payload.get(key))
            if v != 0.0:
                return v
    return 0.0


def _extract_trade_value(payload: dict | None) -> float:
    if not payload:
        return 0.0
    for key in ("acml_tr_pbmn", "acc_trdval", "tot_tr_amt"):
        if key in payload:
            v = _safe_float(payload.get(key))
            if v > 0:
                return v
    return 0.0


def _extract_first_number(payload: dict | None, keys: tuple[str, ...]) -> float:
    if not payload:
        return 0.0
    for key in keys:
        if key in payload:
            v = _safe_float(payload.get(key))
            if v != 0.0:
                return v
    return 0.0


def _fmt_pct_or_na(value: float | None) -> str:
    if value is None:
        return "N/A"
    try:
        v = float(value)
    except Exception:
        return "N/A"
    if abs(v) <= 1e-9:
        return "N/A"
    return f"{v:+.2f}%"


def _fmt_num_or_na(value: float | int | None, digits: int = 0) -> str:
    if value is None:
        return "N/A"
    try:
        v = float(value)
    except Exception:
        return "N/A"
    if abs(v) <= 1e-9:
        return "N/A"
    if digits <= 0:
        return f"{v:,.0f}"
    return f"{v:.{digits}f}"


def _extract_decision_confidence(
    claude_opinion: str | None,
    trace: dict | None = None,
) -> int | None:
    """Extract confidence(0~100) from trace or opinion text."""
    try:
        tv = (trace or {}).get("confidence")
        if tv is not None:
            c = int(float(tv))
            return max(0, min(100, c))
    except Exception:
        pass

    if not claude_opinion:
        return None
    try:
        import re
        for line in str(claude_opinion).splitlines():
            if "신뢰도" in line or "confidence" in line.lower():
                m = re.search(r"(\d{1,3})\s*%?", line)
                if m:
                    c = int(m.group(1))
                    return max(0, min(100, c))
    except Exception:
        pass
    return None


def _build_stock_feature_snapshot(
    signal,
    stock_meta: dict | None,
    price_payload: dict | None,
) -> str:
    chart = getattr(signal, "chart", None)
    snapshot = {
        "captured_at": _now_kst().strftime("%Y-%m-%d %H:%M:%S"),
        "stock_code": getattr(signal, "stock_code", None),
        "stock_name": getattr(signal, "stock_name", None),
        "current_price": getattr(signal, "current_price", None),
        "change_pct": _extract_change_pct(price_payload),
        "trade_value": _extract_trade_value(price_payload),
        "trade_volume": _extract_first_number(price_payload, ("acml_vol", "acc_trdvol", "tot_tr_qty")),
        "open_price": _extract_first_number(price_payload, ("open_pric", "stck_oprc", "opn_pric")),
        "high_price": _extract_first_number(price_payload, ("high_pric", "stck_hgpr")),
        "low_price": _extract_first_number(price_payload, ("lwst_pric", "low_pric", "stck_lwpr")),
        "rsi": getattr(signal, "rsi", None),
        "volume_ratio": getattr(signal, "volume_ratio", None),
        "ma5": getattr(chart, "ma5", None) if chart else None,
        "ma20": getattr(chart, "ma20", None) if chart else None,
        "macd_line": getattr(chart, "macd_line", None) if chart else None,
        "macd_signal": getattr(chart, "macd_signal", None) if chart else None,
        "sector_code": (stock_meta or {}).get("sector_code"),
        "signal_type": getattr(signal, "signal_type", None),
        "triggered_conditions": list(getattr(signal, "triggered_conditions", []) or []),
        "raw_price_payload": price_payload or {},
    }
    cleaned = {k: v for k, v in snapshot.items() if v not in (None, "", [])}
    return json.dumps(cleaned, ensure_ascii=False)


def _label_market_regime(avg_change_pct: float) -> str:
    if avg_change_pct >= 0.8:
        return "risk_on"
    if avg_change_pct <= -0.8:
        return "risk_off"
    return "neutral"


def _label_trend(avg_change_pct: float) -> str:
    if avg_change_pct >= 0.4:
        return "bullish"
    if avg_change_pct <= -0.4:
        return "bearish"
    return "sideways"


def _label_volatility(abs_moves: list[float]) -> str:
    if not abs_moves:
        return "medium"
    m = sum(abs_moves) / len(abs_moves)
    if m >= 1.5:
        return "high"
    if m <= 0.5:
        return "low"
    return "medium"


def _label_aggressiveness(market_regime: str, volatility: str) -> str:
    if market_regime == "risk_on" and volatility != "high":
        return "high"
    if market_regime == "risk_off" or volatility == "high":
        return "low"
    return "medium"


def _parse_hhmm(value: str, default_h: int, default_m: int) -> tuple[int, int]:
    s = str(value or "").strip()
    if ":" not in s:
        return default_h, default_m
    hh, mm = s.split(":", 1)
    try:
        return max(0, min(23, int(hh))), max(0, min(59, int(mm)))
    except Exception:
        return default_h, default_m


def _fetch_yahoo_quote(symbol: str) -> dict:
    try:
        import httpx
        url = "https://query1.finance.yahoo.com/v7/finance/quote"
        resp = httpx.get(
            url,
            params={"symbols": symbol, "fields": "regularMarketPrice,regularMarketChangePercent"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=6,
        )
        resp.raise_for_status()
        rows = ((resp.json() or {}).get("quoteResponse") or {}).get("result") or []
        if not rows:
            return {}
        row = rows[0]
        return {
            "price": _safe_float(row.get("regularMarketPrice")),
            "change_pct": _safe_float(row.get("regularMarketChangePercent")),
        }
    except Exception:
        return {}


def run_premarket_report():
    """Generate premarket report (based on overnight/global context) and save structured labels."""
    today = _now_kst().strftime("%Y-%m-%d")
    try:
        from worker.clients.global_market import get_global_indices
        from worker.clients.news_client import get_macro_news_for_ai

        kospi = kiwoom.get_market_index("kospi") or {}
        time.sleep(0.5)
        kosdaq = kiwoom.get_market_index("kosdaq") or {}
        global_idx = get_global_indices() or {}
        nasdaq = global_idx.get("나스닥", {})
        spx = global_idx.get("S&P500", {})
        usdkrw = global_idx.get("달러/원", {})

        nq_fut = _fetch_yahoo_quote("NQ=F")
        es_fut = _fetch_yahoo_quote("ES=F")
        us10y = _fetch_yahoo_quote("^TNX")
        wti = _fetch_yahoo_quote("CL=F")
        gold = _fetch_yahoo_quote("GC=F")
        macro_news = get_macro_news_for_ai(max_per_keyword=2, max_total=6) or "수집된 거시 뉴스 없음"

        base_moves = [
            _safe_float(nasdaq.get("change_pct")),
            _safe_float(spx.get("change_pct")),
            _safe_float(nq_fut.get("change_pct")),
            _safe_float(es_fut.get("change_pct")),
        ]
        base_moves = [v for v in base_moves if abs(v) > 0]
        avg_move = (sum(base_moves) / len(base_moves)) if base_moves else 0.0

        market_regime = _label_market_regime(avg_move)
        trend = _label_trend(avg_move)
        volatility = _label_volatility([abs(v) for v in base_moves])
        recommended_aggr = _label_aggressiveness(market_regime, volatility)

        aggressive_entry = recommended_aggr == "high"
        increase_cash = market_regime == "risk_off" or volatility == "high"
        agent_policy = {
            "priority": "premarket",
            "report_type": "premarket",
            "report_date": today,
            "generated_at": _now_kst().strftime("%Y-%m-%d %H:%M:%S"),
            "market_regime": market_regime,
            "volatility": volatility,
            "trend": trend,
            "recommended_aggressiveness": recommended_aggr,
            "aggressive_entry": aggressive_entry,
            "increase_cash": increase_cash,
        }
        avoid_targets = "갭 과열 추격 매수, 저유동성 급등주"

        agent_policy["avoid_targets"] = avoid_targets

        summary = (
            f"장전 브리프 {today} | regime={market_regime}, vol={volatility}, "
            f"trend={trend}, aggr={recommended_aggr}"
        )
        detail_lines = [
            f"[전일 국내장] KOSPI {_extract_change_pct(kospi):+.2f}% / KOSDAQ {_extract_change_pct(kosdaq):+.2f}%",
            f"[전일 미국장/글로벌] 나스닥 {nasdaq.get('change_pct', 0):+.2f}% / S&P500 {spx.get('change_pct', 0):+.2f}%",
            f"[선물] NQ {nq_fut.get('change_pct', 0):+.2f}% / ES {es_fut.get('change_pct', 0):+.2f}%",
            f"[환율] USD/KRW {usdkrw.get('price', 0):,.0f} ({usdkrw.get('change_pct', 0):+.2f}%)",
            f"[금리] 미국 10년물 {us10y.get('price', 0):.2f} ({us10y.get('change_pct', 0):+.2f}%)",
            f"[원자재] WTI {wti.get('price', 0):.2f} ({wti.get('change_pct', 0):+.2f}%), Gold {gold.get('price', 0):.2f} ({gold.get('change_pct', 0):+.2f}%)",
            f"[주요 뉴스/이벤트]\n{macro_news}",
            f"[시장 분위기] {market_regime}",
            f"[오늘의 매매 강도] {recommended_aggr}",
            "",
            f"오늘 agent는 공격적으로 진입해도 되는가? {'예' if aggressive_entry else '아니오'}",
            f"어떤 섹터/종목은 피해야 하는가? {avoid_targets}",
            f"현금 비중을 높여야 하는가? {'예' if increase_cash else '아니오'}",
        ]
        detail = "\n".join(detail_lines)

        save_market_report(
            report_date=today,
            report_type="premarket",
            summary=summary,
            detail=detail,
            market_regime=market_regime,
            volatility=volatility,
            trend=trend,
            recommended_aggressiveness=recommended_aggr,
            aggressive_entry=aggressive_entry,
            avoid_targets=avoid_targets,
            increase_cash=increase_cash,
            meta={
                "domestic_indices": {"kospi": kospi, "kosdaq": kosdaq},
                "global_indices": global_idx,
                "futures": {"nq": nq_fut, "es": es_fut},
                "rates": {"us10y": us10y},
                "commodities": {"wti": wti, "gold": gold},
                "agent_policy": agent_policy,
            },
        )
        send_message(f"📘 *장 시작 전 리포트*\n\n{detail}")
        logger.info(f"[market_report] premarket saved: {summary}")
    except Exception as e:
        logger.warning(f"[market_report] premarket failed: {e}", exc_info=True)


def run_opening_report():
    """Generate opening report (actual market check after open) and save structured labels."""
    today = _now_kst().strftime("%Y-%m-%d")
    try:
        kospi = kiwoom.get_market_index("kospi") or {}
        time.sleep(0.5)
        kosdaq = kiwoom.get_market_index("kosdaq") or {}
        k_moves = [_extract_change_pct(kospi), _extract_change_pct(kosdaq)]
        k_moves_nz = [v for v in k_moves if abs(v) > 0]
        avg_move = (sum(k_moves_nz) / len(k_moves_nz)) if k_moves_nz else 0.0
        market_regime = _label_market_regime(avg_move)
        trend = _label_trend(avg_move)
        volatility = _label_volatility([abs(v) for v in k_moves_nz])
        recommended_aggr = _label_aggressiveness(market_regime, volatility)

        enabled = [s for s in get_watchlist() if s.get("enabled")]
        movers = []
        total_turnover = 0.0
        for stock in enabled[:30]:
            code = str(stock.get("code", ""))
            name = str(stock.get("name", code))
            if not code:
                continue
            pd = kiwoom.get_current_price(code) or {}
            pct = _extract_change_pct(pd)
            turnover = _extract_trade_value(pd)
            total_turnover += turnover
            movers.append({
                "code": code,
                "name": name,
                "pct": pct,
                "sector_code": stock.get("sector_code"),
            })
            time.sleep(0.1)

        top_up = sorted(movers, key=lambda x: x["pct"], reverse=True)[:3]
        top_dn = sorted(movers, key=lambda x: x["pct"])[:3]
        gap_up = [m for m in movers if m["pct"] >= 2.0]
        gap_dn = [m for m in movers if m["pct"] <= -2.0]

        sector_score: dict[str, int] = {}
        for m in top_up:
            key = str(m.get("sector_code") or "unknown")
            sector_score[key] = sector_score.get(key, 0) + 1
        lead_sector = max(sector_score.items(), key=lambda x: x[1])[0] if sector_score else "unknown"

        pre = get_latest_market_report(report_type="premarket", report_date=today) or {}
        expected = str(pre.get("market_regime") or "")
        diff_text = "예상과 유사"
        if expected and expected != market_regime:
            diff_text = f"예상({expected}) 대비 실제({market_regime})로 차이 발생"

        aggressive_entry = recommended_aggr == "high"
        avoid_targets = ", ".join(m["name"] for m in top_dn) or "급락/저유동성 종목"
        increase_cash = market_regime == "risk_off" or volatility == "high"
        has_conflict = bool(expected and expected != market_regime)
        agent_policy = {
            "priority": "open",
            "report_type": "open",
            "report_date": today,
            "generated_at": _now_kst().strftime("%Y-%m-%d %H:%M:%S"),
            "market_regime": market_regime,
            "volatility": volatility,
            "trend": trend,
            "recommended_aggressiveness": recommended_aggr,
            "aggressive_entry": aggressive_entry,
            "avoid_targets": avoid_targets,
            "increase_cash": increase_cash,
            "premarket_expected_regime": expected or None,
            "regime_conflict": has_conflict,
            "conflict_resolution": "open_first" if has_conflict else "aligned",
        }

        up_txt = ", ".join(f"{m['name']} {m['pct']:+.2f}%" for m in top_up) or "없음"
        dn_txt = ", ".join(f"{m['name']} {m['pct']:+.2f}%" for m in top_dn) or "없음"
        detail_lines = [
            f"[갭] 상승 {len(gap_up)}개 / 하락 {len(gap_dn)}개 (표본 {len(movers)}개)",
            f"[지수 초반 방향] KOSPI {_extract_change_pct(kospi):+.2f}% / KOSDAQ {_extract_change_pct(kosdaq):+.2f}%",
            f"[거래대금(표본)] {total_turnover:,.0f}",
            f"[주도 섹터] {lead_sector}",
            f"[급등 종목] {up_txt}",
            f"[급락 종목] {dn_txt}",
            f"[예상과 실제] {diff_text}",
            f"[전략 업데이트] regime={market_regime}, volatility={volatility}, aggressiveness={recommended_aggr}",
            "",
            f"오늘 agent는 공격적으로 진입해도 되는가? {'예' if aggressive_entry else '아니오'}",
            f"어떤 섹터/종목은 피해야 하는가? {avoid_targets}",
            f"현금 비중을 높여야 하는가? {'예' if increase_cash else '아니오'}",
        ]
        detail = "\n".join(detail_lines)
        summary = (
            f"장초 체크 {today} | regime={market_regime}, vol={volatility}, "
            f"trend={trend}, aggr={recommended_aggr}"
        )

        save_market_report(
            report_date=today,
            report_type="open",
            summary=summary,
            detail=detail,
            market_regime=market_regime,
            volatility=volatility,
            trend=trend,
            recommended_aggressiveness=recommended_aggr,
            aggressive_entry=aggressive_entry,
            avoid_targets=avoid_targets,
            increase_cash=increase_cash,
            meta={
                "kospi": kospi,
                "kosdaq": kosdaq,
                "movers_top_up": top_up,
                "movers_top_down": top_dn,
                "premarket_expected_regime": expected,
                "agent_policy": agent_policy,
            },
        )
        send_message(f"📗 *장 시작 직후 리포트*\n\n{detail}")
        logger.info(f"[market_report] open saved: {summary}")
    except Exception as e:
        logger.warning(f"[market_report] open failed: {e}", exc_info=True)


def _build_ai_cache_key(signal) -> str:
    """Stable cache key for AI judgment reuse."""
    ids = sorted(str(x) for x in (getattr(signal, "triggered_ids", []) or []))
    return f"{signal.stock_code}|{signal.signal_type}|{','.join(ids)}|{int(bool(signal.in_portfolio))}"


def _get_cached_ai_opinion(signal, ttl_minutes: int) -> str | None:
    if ttl_minutes <= 0:
        return None
    key = _build_ai_cache_key(signal)
    row = _ai_judgment_cache.get(key)
    if not row:
        return None
    cached_at, opinion = row
    if (_now_kst() - cached_at).total_seconds() > (ttl_minutes * 60):
        _ai_judgment_cache.pop(key, None)
        return None
    return opinion


def _set_cached_ai_opinion(signal, opinion: str) -> None:
    if not opinion:
        return
    key = _build_ai_cache_key(signal)
    _ai_judgment_cache[key] = (_now_kst(), opinion)


def _get_strong_exit_condition_ids() -> set[str]:
    raw = _WORKER_CONFIG.get(
        "strong_exit_condition_ids",
        "stop_loss_price,target_price,ma20_support_break,death_cross,macd_death_cross,"
        "ichimoku_death_cross,ichimoku_cloud_breakdown,stochastic_death_cross",
    )
    if isinstance(raw, list):
        return {str(x).strip() for x in raw if str(x).strip()}
    return {x.strip() for x in str(raw or "").split(",") if x.strip()}


def _is_strong_exit_signal(signal) -> bool:
    ids = {str(x).strip() for x in (getattr(signal, "triggered_ids", []) or [])}
    if ids & _get_strong_exit_condition_ids():
        return True

    # Fallback: condition text hints for hard exits.
    # "목표가" 제외: 목표가 도달은 AI가 모멘텀 보고 부분/전량 판단 (hard_keywords에서 분리)
    text = " ".join(str(x) for x in (getattr(signal, "triggered_conditions", []) or []))
    hard_keywords = ("손절", "데드크로스", "하향 이탈", "구름대 이탈")
    return any(k in text for k in hard_keywords)


def _confirm_weak_exit_ready(stock_code: str) -> bool:
    minutes = max(0, int(_WORKER_CONFIG.get("weak_exit_confirm_minutes", 20)))
    if minutes <= 0:
        return True

    now = _now_kst()
    first_seen = _pending_weak_exit_confirm.get(stock_code)
    if first_seen and (now - first_seen).total_seconds() <= minutes * 60:
        _pending_weak_exit_confirm.pop(stock_code, None)
        return True

    _pending_weak_exit_confirm[stock_code] = now
    return False


def _parse_trade_dt(value: str) -> datetime | None:
    s = str(value or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            pass
    return None


def _has_recent_buy_trade(stock_code: str, within_minutes: int) -> bool:
    if within_minutes <= 0:
        return False
    try:
        from data.db import get_recent_trades_for_stock

        now = _now_kst()
        trades = get_recent_trades_for_stock(stock_code, days=3) or []
        for t in trades:
            if str(t.get("side") or "").strip() != "매수":
                continue
            dt = _parse_trade_dt(t.get("executed_at"))
            if not dt:
                continue
            if (now - dt).total_seconds() <= within_minutes * 60:
                return True
    except Exception:
        return False
    return False


def _parse_ymd(value: str) -> str:
    s = str(value or "").replace("-", "").strip()
    return s if len(s) == 8 and s.isdigit() else ""


def _today_soft_avoid_hit(stock_code: str, stock_name: str) -> tuple[bool, str]:
    """Return whether stock is in today's avoid targets (open first, then premarket)."""
    try:
        today = _now_kst().strftime("%Y-%m-%d")
        rep = get_latest_market_report(report_type="open", report_date=today) or {}
        if not rep:
            rep = get_latest_market_report(report_type="premarket", report_date=today) or {}
        avoid_text = str(rep.get("avoid_targets") or "").strip()
        if not avoid_text:
            return False, ""
        code = str(stock_code or "").strip()
        name = str(stock_name or "").strip()
        tokens = [t.strip() for t in avoid_text.split(",") if t.strip()]
        code_hit = code and any(code == t for t in tokens)
        name_hit = name and any(name in t or t in name for t in tokens)
        return bool(code_hit or name_hit), avoid_text
    except Exception:
        return False, ""


def _business_days_elapsed(start_dt, end_dt) -> int:
    """start_dt(당일 포함) 다음 거래일~end_dt까지의 평일 개수.
    한국 휴일 캘린더는 미반영, 주말만 제외.
    """
    if not start_dt or not end_dt or end_dt <= start_dt:
        return 0
    days = 0
    cur = start_dt + _td(days=1)
    while cur <= end_dt:
        if cur.weekday() < 5:
            days += 1
        cur += _td(days=1)
    return days


def _resolve_eval_price(stock_code: str, target_dt, today_dt):
    """성과 평가 가격 결정.
    - target이 오늘이고 정규장(main) 진행 중이면 현재가
    - 그 외에는 target일(없으면 직전 영업일) 종가
    """
    target_ymd = target_dt.strftime("%Y%m%d")
    today_ymd = today_dt.strftime("%Y%m%d")

    if target_ymd == today_ymd and get_current_session() == "main":
        pd = kiwoom.get_current_price(stock_code)
        now_price = _safe_int_price(pd.get("cur_prc") or pd.get("stk_prpr") or pd.get("prpr"))
        return now_price, "current"

    daily = kiwoom.get_daily_ohlcv(stock_code, period=40) or []
    candidate_price = 0
    candidate_ymd = ""
    for d in daily:
        row_ymd = (
            _parse_ymd(d.get("dt"))
            or _parse_ymd(d.get("trde_dt"))
            or _parse_ymd(d.get("stck_bsop_date"))
            or _parse_ymd(d.get("bsop_date"))
            or _parse_ymd(d.get("date"))
        )
        if not row_ymd or row_ymd > target_ymd:
            continue
        cp = _safe_int_price(d.get("cur_prc") or d.get("stck_clpr") or d.get("close"))
        if cp <= 0:
            continue
        if not candidate_ymd or row_ymd > candidate_ymd:
            candidate_ymd = row_ymd
            candidate_price = cp

    return candidate_price, (f"close:{candidate_ymd}" if candidate_ymd else "close:none")


def _rag_index_signal(
    signal,
    signal_id: int,
    claude_opinion: str | None = None,
    dart_summary: str | None = None,
    news_summary: str | None = None,
) -> None:
    """신호 저장 후 FAISS RAG 인덱싱 (백그라운드). db.py 의존성 분리용."""
    try:
        realtime_index = bool(_WORKER_CONFIG.get("rag_realtime_index", False))
        if not realtime_index:
            _rag_pending_signal_ids.add(int(signal_id))
            return

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


def run_rag_batch_index():
    """큐에 쌓인 signal_id를 배치로 RAG 인덱싱."""
    global _rag_pending_signal_ids
    if not _rag_pending_signal_ids:
        return

    batch_size = max(1, int(_WORKER_CONFIG.get("rag_batch_size", 100)))
    max_signals = max(1, int(_WORKER_CONFIG.get("rag_batch_max_signals", 300)))
    pending = sorted(_rag_pending_signal_ids)[:max_signals]
    _rag_pending_signal_ids = _rag_pending_signal_ids - set(pending)

    try:
        from worker.agents.tools.rag_tools import bulk_index_signals_by_ids
        count = bulk_index_signals_by_ids(pending, batch_size=batch_size)
        logger.info(f"[RAG] 배치 인덱싱 완료: {count}/{len(pending)}건 (batch_size={batch_size})")
    except Exception as e:
        # 실패 시 큐 복원
        _rag_pending_signal_ids.update(pending)
        logger.warning(f"[RAG] 배치 인덱싱 실패(큐 복원): {e}")


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


def _extract_last_hold_transition_condition(stock_code: str) -> str:
    """최근 7일 내 동일 종목 홀드 의견의 [전환조건] 텍스트를 1건 조회."""
    try:
        from data.db import get_conn
        from datetime import date, timedelta

        cutoff = (date.today() - timedelta(days=7)).strftime("%Y-%m-%d")
        with get_conn() as conn:
            row = conn.execute(
                "SELECT claude_opinion FROM signals "
                "WHERE stock_code = ? AND created_at >= ? "
                "AND claude_opinion IS NOT NULL AND claude_opinion LIKE '%[홀드]%' "
                "ORDER BY created_at DESC LIMIT 1",
                (stock_code, cutoff),
            ).fetchone()
        if not row:
            return ""
        opinion = str(row["claude_opinion"] or "")
        for line in opinion.splitlines():
            txt = line.strip()
            if txt.startswith("[전환조건]"):
                return txt[len("[전환조건]"):].strip()
        return ""
    except Exception:
        return ""


def _evaluate_transition_condition(signal, condition_text: str) -> tuple[bool, str, int]:
    """가격/RSI 기반 전환조건 충족 판정. (ok, rule_info, parsed_rule_count)"""
    import re

    if not condition_text:
        return False, "empty", 0
    cond = str(condition_text)
    low = cond.lower()

    price = int(getattr(signal, "current_price", 0) or 0)
    rsi = getattr(signal, "rsi", None)

    checks: list[tuple[bool, str]] = []

    price_match = re.search(r"([0-9][0-9,]{2,})\s*원", cond)
    if price_match and price > 0:
        p = int(price_match.group(1).replace(",", ""))
        if any(k in low for k in ["이상", "돌파", "상향"]):
            checks.append((price >= p, f"price>={p}"))
        elif any(k in low for k in ["이하", "이탈", "하향"]):
            checks.append((price <= p, f"price<={p}"))

    rsi_match = re.search(r"rsi[^0-9]*([0-9]+(?:\\.[0-9]+)?)", low)
    if rsi_match and rsi is not None:
        rv = float(rsi_match.group(1))
        if any(k in low for k in ["이상", ">=", "초과", "상향"]):
            checks.append((float(rsi) >= rv, f"rsi>={rv}"))
        elif any(k in low for k in ["이하", "<=", "미만", "하향"]):
            checks.append((float(rsi) <= rv, f"rsi<={rv}"))

    if not checks:
        return False, "no_parsable_rule", 0
    ok = all(flag for flag, _ in checks)
    return ok, ",".join(rule for _, rule in checks), len(checks)


def _check_semiforce_transition(signal, claude_opinion: str | None) -> dict | None:
    """준강제 전환 후보를 평가하고, 재판단 힌트(dict)를 반환."""
    if not isinstance(claude_opinion, str) or not claude_opinion.strip():
        return None
    first = claude_opinion.strip().splitlines()[0].strip().lower()
    if "홀드" not in first and "hold" not in first:
        return None

    condition_text = _extract_last_hold_transition_condition(getattr(signal, "stock_code", ""))
    if not condition_text:
        return None

    # AUTO_TRADE에서는 명시적으로 켜지지 않으면 준강제 자동 전환 비활성(안전 기본값).
    semi_force_cfg = bool(_WORKER_CONFIG.get("semi_force_transition_auto_trade", False))
    if AUTO_TRADE and not semi_force_cfg:
        logger.info(f"[{signal.stock_name}] 준강제 전환 스킵")
        return None

    cond_low = condition_text.lower()
    # 명시적 지시가 없으면 전환 금지 (예: "[매수 전환]" 또는 "매수 전환")
    if "매수 전환" in cond_low:
        target = "매수"
    elif "매도 전환" in cond_low:
        target = "매도"
    else:
        return None

    ok, rule_info, parsed_count = _evaluate_transition_condition(signal, condition_text)
    if not ok:
        return None
    # 부분 파싱 오동작 방지: 최소 2개 규칙 이상 파싱되어야 전환 허용
    if parsed_count < 2:
        logger.info(
            f"[{signal.stock_name}] 준강제 전환 스킵: 파싱 규칙 부족(parsed={parsed_count})"
        )
        return None

    trigger_ids = set(getattr(signal, "triggered_ids", []) or [])
    critical_ids = {"stop_loss_price", "rsi_critical", "bollinger_critical_below"}
    has_critical_risk = bool(trigger_ids & critical_ids)

    if has_critical_risk and target == "매수":
        logger.info(f"[{signal.stock_name}] 준강제 전환 차단(critical risk): {condition_text}")
        return None
    if target == "매도" and not bool(getattr(signal, "in_portfolio", False)):
        return None

    logger.info(f"[{signal.stock_name}] 준강제 재판단 트리거: hold -> {target} ({condition_text})")
    return {
        "target": target,
        "condition_text": condition_text,
        "rule_info": rule_info,
        "parsed_count": parsed_count,
    }


def run_weekly_performance_report():
    """매주 월요일 09:00 - 지난주 AI 신호 성과 리포트를 전략 노트에 기록하고 텔레그램 발송."""
    from data.db import get_weekly_performance_report, save_strategy_note
    from notifications.telegram import send_message

    logger.info("[성과리포트] 주간 성과 분석 시작")
    try:
        rpt = get_weekly_performance_report(days=7)
    except Exception as e:
        logger.warning(f"[성과리포트] 데이터 조회 실패: {e}")
        return

    if not rpt or rpt.get("rated_count", 0) == 0:
        logger.info("[성과리포트] 최근 7일 평가 가능 신호 없음 - 스킵")
        return

    def _section_lines(title: str, block: dict) -> list[str]:
        total = int(block.get("signal_count") or 0)
        rated = int(block.get("rated_count") or 0)
        lines = [f"## {title}", f"- 신호 {total}건 (평가 {rated}건)"]

        if rated == 0:
            lines.append("- 평가 완료된 신호 없음")
            return lines

        wr = block.get("win_rate_3d")
        avg3 = block.get("avg_return_3d")
        avg1 = block.get("avg_return_1d")
        avg5 = block.get("avg_return_5d")
        lines.append(f"- 승률 {wr}% | 3일 평균 {avg3:+.2f}%")
        if avg1 is not None:
            lines.append(f"- 1일 평균 {avg1:+.2f}%")
        if avg5 is not None:
            lines.append(f"- 5일 평균 {avg5:+.2f}%")

        vbd = block.get("verdict_breakdown", {})
        if vbd:
            lines.append("- 판정별")
            for verdict, stat in vbd.items():
                lines.append(
                    f"  [{verdict}] {stat['count']}건 | 승률 {stat['win_rate']}% | 평균 {stat['avg_return']:+.2f}%"
                )

        best = block.get("best_stock") or {}
        worst = block.get("worst_stock") or {}
        if best:
            lines.append(f"- 최고: {best.get('name', '?')} ({best.get('return', 0):+.2f}%)")
        if worst:
            lines.append(f"- 최악: {worst.get('name', '?')} ({worst.get('return', 0):+.2f}%)")
        return lines

    total_block = {
        "signal_count": rpt.get("signal_count"),
        "rated_count": rpt.get("rated_count"),
        "win_rate_3d": rpt.get("win_rate_3d"),
        "avg_return_3d": rpt.get("avg_return_3d"),
        "avg_return_1d": rpt.get("avg_return_1d"),
        "avg_return_5d": rpt.get("avg_return_5d"),
        "best_stock": rpt.get("best_stock"),
        "worst_stock": rpt.get("worst_stock"),
        "verdict_breakdown": rpt.get("verdict_breakdown") or {},
    }
    portfolio_block = rpt.get("portfolio") or {}
    watchlist_block = rpt.get("watchlist") or {}

    summary = (
        f"주간 성과 리포트(통합/포트/워치) - 통합 신호 {total_block['signal_count']}건 / "
        f"평가 {total_block['rated_count']}건 / 승률 {total_block['win_rate_3d']}% / "
        f"3일평균 {total_block['avg_return_3d']:+.2f}%"
    )

    detail_parts = [f"## 주간 성과 요약 (최근 {int(rpt.get('period_days') or 7)}일)"]
    detail_parts.extend(_section_lines("통합", total_block))
    detail_parts.append("")
    detail_parts.extend(_section_lines("Portfolio (보유 종목 기반)", portfolio_block))
    detail_parts.append("")
    detail_parts.extend(_section_lines("Watchlist (비보유 종목 기반)", watchlist_block))

    paper = rpt.get("paper_summary")
    if paper and paper.get("count"):
        detail_parts.append("")
        detail_parts.append(
            f"- 모의투자: {paper['count']}건 | 승률 {paper.get('win_rate', '-')}% | 평균 {paper.get('avg_return', 0):+.2f}%"
        )

    detail = "\n".join(detail_parts)

    try:
        save_strategy_note(category="general", summary=summary, detail=detail)
        logger.info("[성과리포트] 전략 노트 저장 완료")
    except Exception as e:
        logger.warning(f"[성과리포트] 전략 노트 저장 실패: {e}")
        return

    try:
        msg_lines = ["📈 *주간 성과 리포트*", ""]

        def _telegram_section(title: str, block: dict):
            total = int(block.get("signal_count") or 0)
            rated = int(block.get("rated_count") or 0)
            msg_lines.append(f"*{title}*")
            msg_lines.append(f"신호 {total}건 (평가 {rated}건)")
            if rated > 0:
                msg_lines.append(
                    f"승률 {block.get('win_rate_3d')}% | 3일 평균 {block.get('avg_return_3d'):+.2f}%"
                )
                vbd = block.get("verdict_breakdown", {})
                for verdict, stat in vbd.items():
                    msg_lines.append(
                        f"{verdict} {stat['count']}건 | 승률 {stat['win_rate']}% | 평균 {stat['avg_return']:+.2f}%"
                    )
            msg_lines.append("")

        _telegram_section("통합", total_block)
        _telegram_section("Portfolio", portfolio_block)
        _telegram_section("Watchlist", watchlist_block)

        send_message("\n".join(msg_lines).strip())
    except Exception as e:
        logger.debug(f"[성과리포트] 텔레그램 발송 실패: {e}")


def run_weekly_self_correction():
    """Run weekly self-correction summary for verdict/condition/screening performance."""
    from data.db import (
        get_condition_accuracy,
        get_screening_accuracy,
        get_verdict_accuracy,
        save_strategy_note,
    )
    from notifications.telegram import send_message

    logger.info("[self-correct] weekly accuracy analysis start")
    try:
        condition_stats = get_condition_accuracy(days=30, min_count=3)
        verdict_stats = get_verdict_accuracy(days=30)
        screening_stats = get_screening_accuracy(days=30)
    except Exception as e:
        logger.warning(f"[self-correct] failed to load stats: {e}")
        return

    if not condition_stats and not verdict_stats and not screening_stats:
        logger.info("[self-correct] no data - skip")
        return

    verdict_lines = []
    for verdict, stat in sorted(verdict_stats.items()):
        hit_rate = stat.get("hit_rate_3d")
        avg_3d = stat.get("avg_3d")
        count = stat.get("count", 0)
        verdict_lines.append(
            f"  [{verdict}] {count} | hit {hit_rate:.0f}% | avg3 {avg_3d:+.2f}%"
            if hit_rate is not None and avg_3d is not None
            else f"  [{verdict}] {count} (insufficient data)"
        )

    low_perf = [c for c in condition_stats if (c.get("hit_rate_3d") or 0) < 40]
    low_lines = []
    for c in low_perf:
        low_lines.append(
            f"  ! {c['condition']} | {c['count']} | hit {c['hit_rate_3d']:.0f}% | avg3 {c.get('avg_3d', 0):+.2f}%"
        )

    total_reco = int((screening_stats or {}).get("total") or 0)
    summary = (
        f"weekly self-correction - verdicts {len(verdict_stats)} / "
        f"low-perf conditions {len(low_perf)} / recommendations {total_reco}"
    )

    detail_parts = ["## Verdict Accuracy (last 30d)"]
    detail_parts.extend(verdict_lines or ["  no data"])

    if low_perf:
        detail_parts.append("\n## Low Performance Conditions (hit rate < 40%)")
        detail_parts.extend(low_lines)

    if screening_stats:
        detail_parts.append("\n## Stock Recommendation Performance (last 30d)")
        detail_parts.append(f"- total registered: {screening_stats.get('total', 0)}")
        if screening_stats.get("hit_7d") is not None:
            detail_parts.append(
                f"- 7d hit {screening_stats['hit_7d']}% | 7d avg {screening_stats.get('avg_7d', 0):+.2f}%"
            )
        if screening_stats.get("hit_30d") is not None:
            detail_parts.append(
                f"- 30d hit {screening_stats['hit_30d']}% | 30d avg {screening_stats.get('avg_30d', 0):+.2f}%"
            )

    detail = "\n".join(detail_parts)

    try:
        save_strategy_note(category="general", summary=summary, detail=detail)
        logger.info(f"[self-correct] strategy note saved: {summary}")
    except Exception as e:
        logger.warning(f"[self-correct] failed to save strategy note: {e}")
        return

    try:
        msg_lines = [f"🧭 *{summary}*", ""]
        msg_lines.extend(verdict_lines[:5])
        if low_perf:
            msg_lines.append("")
            msg_lines.extend(low_lines[:3])
        if screening_stats:
            msg_lines.append("")
            msg_lines.append(
                f"recommendations {screening_stats.get('total', 0)} | "
                f"7d {screening_stats.get('hit_7d', '-')}% | 30d {screening_stats.get('hit_30d', '-')}%"
            )
        send_message("\n".join(msg_lines))
    except Exception as e:
        logger.debug(f"[self-correct] telegram send failed: {e}")


def run_reflection_policy_cycle():
    """Reflection -> Policy update 자동 루프 실행."""
    try:
        from worker.strategy_reflection import run_policy_update_cycle, get_recent_policy_cycle_logs
        cfg = _WORKER_CONFIG.get("strategy_tuning", {}) if isinstance(_WORKER_CONFIG, dict) else {}
        min_samples = int(cfg.get("min_samples", 10))
        auto_apply_low_risk = bool(cfg.get("auto_apply_low_risk", True))
        out = run_policy_update_cycle(min_samples=min_samples, auto_apply_low_risk=auto_apply_low_risk)
        reflection_written = int((out.get("queued", 0) + out.get("applied", 0)) > 0)
        recent = get_recent_policy_cycle_logs(limit=8)
        reason_lines = []
        for r in recent:
            reason_lines.append(
                f"{r.get('agent_type')}:{r.get('outcome')}:{r.get('reason_code')}"
            )
        reason_text = ", ".join(reason_lines[:6]) if reason_lines else "none"
        logger.info(
            f"[PolicyLoop] preflight_pass=1 missing_fields=[] "
            f"policy_version=system reflection_written={reflection_written} "
            f"queued={out.get('queued',0)} applied={out.get('applied',0)} skipped={out.get('skipped',0)} "
            f"min_samples={min_samples} auto_apply_low_risk={auto_apply_low_risk} "
            f"recent_reasons=[{reason_text}]"
        )
    except Exception as e:
        logger.warning(f"[PolicyLoop] 실패: {e}", exc_info=True)



# 뉴스 알림 쿨다운: {stock_code: 마지막_알림_시각}
def run_daily_review_with_checklist():
    """Persist daily_review guardrail execution checklist, then run daily review."""
    note_id = 0
    try:
        note_id = _save_daily_review_execution_checklist()
    except Exception as e:
        logger.warning(f"[daily_review] checklist flush failed: {e}")
    try:
        # verification checkpoint: confirm today's checklist row is visible in DB
        from data.db import get_strategy_notes
        today = _now_kst().strftime("%Y-%m-%d")
        notes = get_strategy_notes(limit=50) or []
        ok = False
        for n in notes:
            if str(n.get("category", "")) != "general":
                continue
            s = str(n.get("summary", ""))
            if today in s and "daily_review 반영 체크리스트" in s:
                ok = True
                break
        logger.info(
            f"[daily_review] checklist verification "
            f"note_id={note_id} found_today={int(ok)} date={today}"
        )
    except Exception as e:
        logger.warning(f"[daily_review] checklist verification failed: {e}")
    try:
        run_daily_review()
    except Exception as e:
        logger.warning(f"[daily_review] run failed: {e}", exc_info=True)


def run_agent_action_log_refresh():
    """Weekly rolling refresh for agent_action_logs."""
    keep_days = max(1, int(_WORKER_CONFIG.get("agent_action_log_keep_days", 7)))
    try:
        deleted = purge_agent_action_logs(days=keep_days)
        logger.info(f"[AgentTrace] rolling refresh complete: deleted={deleted}, keep_days={keep_days}")
    except Exception as e:
        logger.warning(f"[AgentTrace] rolling refresh failed: {e}")


_news_alert_cooldown: dict = {}
_NEWS_COOLDOWN_HOURS = int(_WORKER_CONFIG.get("news_cooldown_hours", 8))


def run_news_monitor():
    """Monitor held-stock news and let AI judge risk severity."""
    from worker.clients.news_client import search_news, NAVER_CLIENT_ID
    from notifications.telegram import send_message
    from worker.monitor import Signal

    if not NAVER_CLIENT_ID:
        return

    try:
        holdings = get_portfolio()
    except Exception as e:
        logger.warning(f"[news_monitor] failed to load holdings: {e}")
        return

    if not holdings:
        return

    now = _now_kst()
    news_ai_max_calls_per_run = max(0, int(_WORKER_CONFIG.get("news_ai_max_calls_per_run", 2)))
    news_max_age_hours = max(1, int(_WORKER_CONFIG.get("news_max_age_hours", 48)))
    news_ai_calls = 0

    def _is_fresh_news(pub_date_text: str) -> bool:
        text = str(pub_date_text or "").strip()
        if not text:
            return False
        try:
            # news_client currently normalizes to YYYY-MM-DD.
            pub_dt = datetime.strptime(text[:10], "%Y-%m-%d").replace(tzinfo=now.tzinfo)
            age_hours = (now - pub_dt).total_seconds() / 3600.0
            return 0 <= age_hours <= news_max_age_hours
        except Exception:
            return False

    for h in holdings:
        code = str(h.get("stock_code", "")).strip()
        name = str(h.get("stock_name", "") or h.get("stk_nm", "")).strip()
        if not name or not code:
            continue

        last_alert = _news_alert_cooldown.get(code)
        if last_alert and (now - last_alert).total_seconds() < _NEWS_COOLDOWN_HOURS * 3600:
            continue

        try:
            news_items = search_news(name, display=5, sort="date")
            time.sleep(0.3)
        except Exception as e:
            logger.debug(f"[news_monitor] news fetch failed for {name}: {e}")
            continue

        if not news_items:
            continue

        fresh_items = [n for n in news_items if _is_fresh_news(n.get("pub_date", ""))]
        if not fresh_items:
            logger.info(f"[news_monitor] no fresh news within {news_max_age_hours}h: {name}")
            continue
        news_items = fresh_items

        if news_ai_calls >= news_ai_max_calls_per_run:
            logger.info(
                f"[news_monitor] skip remaining symbols: ai budget reached "
                f"({news_ai_calls}/{news_ai_max_calls_per_run})"
            )
            break

        try:
            top_items = news_items[:3]
            digest_lines = []
            for idx, item in enumerate(top_items, start=1):
                title = str(item.get("title", "")).replace("\n", " ").strip()
                desc = str(item.get("description", "")).replace("\n", " ").strip()
                pub = str(item.get("pub_date", "")).strip()
                digest_lines.append(f"{idx}. {title} | {desc} | {pub}")
            digest = "\n".join(digest_lines)

            cur_data = kiwoom.get_current_price(code)
            cur_price = abs(int(str(cur_data.get("cur_prc") or cur_data.get("stk_prpr") or "0").replace(",", "")))

            fake_signal = Signal(
                stock_code=code,
                stock_name=name,
                current_price=cur_price,
                triggered_conditions=[f"news_event\n{digest}"],
                triggered_ids=["news_ai"],
                rsi=None,
                volume_ratio=None,
                chart=None,
                in_portfolio=True,
                signal_type="exit",
            )

            assess = get_news_risk_assessment(
                stock_code=code,
                stock_name=name,
                current_price=cur_price,
                news_digest=digest,
            )
            news_ai_calls += 1
            label = str((assess or {}).get("risk_level") or "ignore").strip().lower()
            if label == "incomplete_context":
                logger.info(f"[news_monitor] JSON risk assessment incomplete_context: {name}")
                continue

            reason = str((assess or {}).get("reason") or "").strip()
            action = str((assess or {}).get("action") or "").strip()
            confidence = int((assess or {}).get("confidence") or 0)
            if label == "critical":
                opinion = f"[매도]\n• 근거1: {reason or '중대한 뉴스 리스크 감지'}\n• 근거2: 조치 제안 - {action or '즉시 점검'}"
            elif label == "warning":
                opinion = f"[홀드]\n• 근거1: {reason or '주의 뉴스 감지'}\n• 근거2: 조치 제안 - {action or '관찰/모니터링'}"

            head_title = str(top_items[0].get("title", ""))[:100]
            head_pub = str(top_items[0].get("pub_date", ""))

            if label == "critical":
                _news_alert_cooldown[code] = now
                signal_id = save_signal(
                    fake_signal,
                    opinion,
                    in_portfolio=True,
                    source="news_monitor",
                    decision_status="normal",
                    **get_judgment_runtime_meta(),
                )
                _rag_index_signal(fake_signal, signal_id, opinion)
                send_message(
                    f"🚨 *AI 뉴스 리스크 경보* ({name})\n"
                    f"{head_title}\n_{head_pub}_\n\n"
                    f"판정: *즉시 점검/대응 필요*\n"
                    f"신뢰도: *{confidence}%*\n"
                    f"사유: {reason or '-'}\n"
                    f"조치: {action or '-'}"
                )
                logger.warning(f"[news_monitor] CRITICAL {name}: {head_title}")
            elif label == "warning":
                _news_alert_cooldown[code] = now
                send_message(
                    f"⚠️ *AI 뉴스 주의 알림* ({name})\n"
                    f"{head_title}\n_{head_pub}_\n\n"
                    f"판정: *관찰/모니터링*\n"
                    f"신뢰도: *{confidence}%*\n"
                    f"사유: {reason or '-'}\n"
                    f"조치: {action or '-'}"
                )
                logger.info(f"[news_monitor] WARNING {name}: {head_title}")
            else:
                logger.debug(f"[news_monitor] IGNORE {name}: {head_title}")

        except Exception as e:
            logger.error(f"[news_monitor] ai classification failed for {name}: {e}")

    logger.debug(f"[news_monitor] done: holdings={len(holdings)}, ai_calls={news_ai_calls}")
def update_signal_results():
    """신호 발생 후 1일/3일/5일/10일 결과 수익률 업데이트 (영업일 기준)."""
    from datetime import datetime
    from data.db import get_conn

    periods = [
        ("1d", 1, "result_1d"),
        ("3d", 3, "result_pct"),
        ("5d", 5, "result_5d"),
        ("10d", 10, "result_10d"),
    ]
    today_kst = _now_kst().date()

    for period_name, days_after, col_name in periods:
        with get_conn() as conn:
            rows = conn.execute(
                f"SELECT id, stock_code, stock_name, current_price, created_at FROM signals "
                f"WHERE {col_name} IS NULL",
            ).fetchall()

        for row in rows:
            try:
                created_dt = datetime.strptime(str(row["created_at"])[:10], "%Y-%m-%d").date()
                if _business_days_elapsed(created_dt, today_kst) < days_after:
                    continue
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
    """Deprecated: paper_trades removed."""
    return


def update_trade_results():
    """실거래 1일/3일/5일 성과 업데이트 (매수/매도 방향 반영).
    기준가:
    - 평가일이 오늘이고 장중(main)이면 현재가
    - 그 외에는 평가일 종가(없으면 직전 영업일 종가)
    """
    from datetime import timedelta
    from data.db import get_conn, update_trade_result

    periods = [("1d", 1, "result_1d"), ("3d", 3, "result_3d"), ("5d", 5, "result_5d")]
    now_kst = _now_kst()
    today_kst = now_kst.date()

    for period_name, days_after, col_name in periods:
        eligible_to = (today_kst - timedelta(days=days_after)).strftime("%Y-%m-%d")
        with get_conn() as conn:
            rows = conn.execute(
                f"SELECT trade_id, stock_code, stock_name, side, price, executed_at FROM trades "
                f"WHERE {col_name} IS NULL AND price > 0 AND executed_at <= ?",
                (eligible_to,),
            ).fetchall()

        for row in rows:
            try:
                base = int(row["price"] or 0)
                if base <= 0:
                    continue
                executed_dt = datetime.strptime(str(row["executed_at"])[:10], "%Y-%m-%d").date()
                if _business_days_elapsed(executed_dt, today_kst) < days_after:
                    continue
                target_dt = executed_dt + _td(days=days_after)

                eval_price, price_src = _resolve_eval_price(row["stock_code"], target_dt, today_kst)
                if eval_price <= 0:
                    continue

                raw_pct = (eval_price - base) / base * 100
                side = str(row["side"] or "")
                signed_pct = -raw_pct if side in ("매도", "SELL", "sell") else raw_pct
                update_trade_result(row["trade_id"], round(signed_pct, 2), period=period_name)
                logger.debug(
                    f"[실거래 결과 {period_name}] {row['stock_name']} {side}: "
                    f"원시 {raw_pct:+.2f}% / 반영 {signed_pct:+.2f}% ({price_src})"
                )
                time.sleep(0.5)
            except Exception as e:
                logger.warning(f"[실거래 결과 실패] {row['stock_name']}: {e}")


def sync_realized_pnl():
    """실현손익(당일/30일) 조회 후 스냅샷 저장."""
    try:
        today = kiwoom.get_realized_pnl_today()
        if today.get("ok"):
            sid = save_realized_pnl_snapshot(
                scope="today",
                source_api=today.get("api_id", ""),
                realized_pnl=today.get("realized_pnl"),
                fee=today.get("fee"),
                tax=today.get("tax"),
                raw=today.get("raw"),
            )
            logger.info(
                f"[실현손익] today 저장 #{sid} api={today.get('api_id')} "
                f"손익={today.get('realized_pnl')} fee={today.get('fee')} tax={today.get('tax')}"
            )
        else:
            logger.warning(f"[실현손익] today 조회 실패: {today.get('error')} | attempts={today.get('attempt_errors')}")
    except Exception as e:
        logger.warning(f"[실현손익] today 저장 실패: {e}")

    try:
        period = kiwoom.get_realized_pnl_period(days=30)
        if period.get("ok"):
            sid = save_realized_pnl_snapshot(
                scope="period",
                source_api=period.get("api_id", ""),
                start_dt=period.get("start_dt"),
                end_dt=period.get("end_dt"),
                realized_pnl=period.get("realized_pnl"),
                fee=period.get("fee"),
                tax=period.get("tax"),
                raw=period.get("raw"),
            )
            logger.info(
                f"[실현손익] 30d 저장 #{sid} api={period.get('api_id')} "
                f"손익={period.get('realized_pnl')} fee={period.get('fee')} tax={period.get('tax')}"
            )
        else:
            logger.warning(f"[실현손익] 30d 조회 실패: {period.get('error')} | attempts={period.get('attempt_errors')}")
    except Exception as e:
        logger.warning(f"[실현손익] 30d 저장 실패: {e}")


def update_screening_results():
    """스크리닝 종목의 7일/30일 후 수익률 자동 업데이트.
    누락 방지:
    - 기존 좁은 시간창(7~8일, 30~31일) 대신
    - 기준일이 지난 NULL 레코드를 모두 보정
    가격 기준:
    - 평가일이 오늘이고 장중(main)이면 현재가
    - 그 외에는 평가일 종가(없으면 직전 영업일 종가)
    """
    from datetime import timedelta
    import json
    from data.db import get_conn, update_screening_result

    now_kst = _now_kst()
    today_kst = now_kst.date()
    periods = [("7d", 7, "result_7d"), ("30d", 30, "result_30d")]

    for period_name, days_after, col in periods:
        with get_conn() as conn:
            rows = conn.execute(
                f"SELECT id, stock_code, stock_name, current_price, created_at, ai_response, indicator_snapshot "
                f"FROM screening_log "
                f"WHERE {col} IS NULL"
            ).fetchall()

        for row in rows:
            try:
                base = int(row["current_price"] or 0)
                # 하위호환: 과거 로그(current_price NULL)도 ai_response/indicator_snapshot에서 복구 시도
                if base <= 0:
                    for src_key in ("ai_response", "indicator_snapshot"):
                        raw = row[src_key]
                        if not raw:
                            continue
                        try:
                            obj = json.loads(raw) if isinstance(raw, str) else raw
                        except Exception:
                            obj = None
                        if isinstance(obj, dict):
                            cand = (
                                obj.get("current_price")
                                or obj.get("cur_prc")
                                or obj.get("stk_prpr")
                                or obj.get("price")
                            )
                            try:
                                cand_i = int(str(cand or "0").replace(",", "").strip())
                            except Exception:
                                cand_i = 0
                            if cand_i > 0:
                                base = cand_i
                                break
                if base <= 0:
                    continue
                created_dt = datetime.strptime(str(row["created_at"])[:10], "%Y-%m-%d").date()
                if _business_days_elapsed(created_dt, today_kst) < days_after:
                    continue
                target_dt = created_dt + _td(days=days_after)

                eval_price, price_src = _resolve_eval_price(row["stock_code"], target_dt, today_kst)
                if eval_price <= 0:
                    continue

                pct = (eval_price - base) / base * 100
                update_screening_result(row["id"], round(pct, 2), period=period_name)
                # 복구한 기준가는 current_price에도 저장해 이후 계산/분석 일관성 확보
                if int(row["current_price"] or 0) <= 0:
                    try:
                        with get_conn() as conn:
                            conn.execute(
                                "UPDATE screening_log SET current_price = COALESCE(current_price, ?) WHERE id = ?",
                                (base, row["id"]),
                            )
                            conn.commit()
                    except Exception:
                        pass
                logger.debug(
                    f"[스크리닝 결과 {period_name}] {row['stock_name']} #{row['id']}: {pct:+.2f}% ({price_src})"
                )
                time.sleep(0.3)
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
            # 신호 이력이 없으면 watchlist 등록일(created_at) 기준으로 계산
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
    """미보유 종목 중 장기 미신호(기본 30일) → 관심종목 자동 삭제."""
    _wm = _WORKER_CONFIG.get("watchlist_management", {})
    INACTIVE_DAYS = int(_wm.get("inactive_days_removal", 30))
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
    """AUTO_TRADE=false path: no mock table write; only signal action update."""
    first_line = claude_opinion.strip().splitlines()[0] if claude_opinion.strip() else ""
    side = None
    if "[매수]" in first_line or "[추가매수" in first_line or "[물타기" in first_line:
        side = "매수"
    elif "[매도]" in first_line:
        side = "매도"
    if side and signal_id is not None:
        from data.db import update_signal_action
        update_signal_action(signal_id, side)
    logger.info(f"[수동모드] {signal.stock_name} 판단 기록만 반영 (실주문/모의체결 없음)")


def _parse_watchlist_decision(opinion: str) -> str | None:
    """매도 후 watchlist 유지/제외 판단 파싱.
    반환: keep | drop | reassess | None
    """
    text = str(opinion or "")
    for line in text.splitlines():
        s = line.strip().lower()
        if not s:
            continue
        if s.startswith("[watchlist") or s.startswith("[워치리스트결정]") or s.startswith("[관심종목결정]"):
            if any(k in s for k in ("keep", "유지")):
                return "keep"
            if any(k in s for k in ("drop", "remove", "삭제", "제거")):
                return "drop"
            if any(k in s for k in ("reassess", "재평가", "보류")):
                return "reassess"
    return None


def _apply_post_sell_watchlist_decision(signal, claude_opinion: str) -> None:
    """전량 매도 후 watchlist 처리.
    유효한 결정이 없으면 기본 REASSESS(비활성+재평가 대기).
    """
    try:
        from data.db import delete_stock, update_stock_field, get_watchlist, save_strategy_note
        default_decision = str(_WORKER_CONFIG.get("post_sell_default_watchlist_decision", "reassess")).strip().lower()
        if default_decision not in {"keep", "drop", "reassess"}:
            default_decision = "reassess"
        decision = _parse_watchlist_decision(claude_opinion) or default_decision

        if decision == "drop":
            removed = delete_stock(signal.stock_code)
            logger.info(f"[{signal.stock_name}] post-sell watchlist 결정: drop (removed={removed})")
            save_strategy_note("watchlist", f"{signal.stock_name} 전량매도 후 watchlist 제거", f"decision=drop(default={default_decision})")
            return

        if decision == "reassess":
            row = next((s for s in get_watchlist() if s.get("code") == signal.stock_code), None)
            old_note = str((row or {}).get("strategy_note") or "")
            marker = f"[POST_EXIT_REVIEW_REQUIRED] {_now_kst().strftime('%Y-%m-%d %H:%M:%S')}"
            new_note = (old_note + "\n" + marker).strip() if old_note else marker
            update_stock_field(signal.stock_code, "enabled", 0)
            update_stock_field(signal.stock_code, "strategy_note", new_note)
            logger.info(f"[{signal.stock_name}] post-sell watchlist 결정: reassess (enabled=0)")
            save_strategy_note("watchlist", f"{signal.stock_name} 전량매도 후 재평가 대기", "decision=reassess")
            return

        # keep
        update_stock_field(signal.stock_code, "enabled", 1)
        logger.info(f"[{signal.stock_name}] post-sell watchlist 결정: keep (enabled=1)")
        save_strategy_note("watchlist", f"{signal.stock_name} 전량매도 후 watchlist 유지", "decision=keep")
    except Exception as e:
        logger.warning(f"[{signal.stock_name}] post-sell watchlist 처리 실패: {e}")


def _auto_execute(
    signal,
    claude_opinion: str,
    signal_id: int | None,
    deposit: int = 0,
    buy_budget: int = 0,
    kospi_rate: float = 0.0,
    kosdaq_rate: float = 0.0,
) -> None:
    """AI 판단이 매수/매도이고 추천수량이 있으면 자동 주문 실행. 추천수량 없으면 홀드."""
    import re

    def _detect_order_side(opinion_text: str) -> tuple[str | None, str | None]:
        """Bracket 형식이 없어도 매수/매도 의도를 최대한 안정적으로 판별."""
        if not isinstance(opinion_text, str):
            return None, None

        lines = [ln.strip() for ln in opinion_text.splitlines() if ln.strip()]
        first = (lines[0] if lines else "").lower()
        head = "\n".join(lines[:3]).lower()

        # 홀드/관망 표현이 있으면 우선 실행 금지
        if any(tok in first for tok in ("[hold]", "hold", "홀드", "관망")):
            return None, None

        buy_tokens = ("[매수]", "[추가매수", "[물타기", " 매수", "매수 ", "buy", "entry")
        sell_tokens = ("[매도]", " 매도", "매도 ", "sell", "exit")

        # 1) 첫 줄에 명시된 단일 verdict를 최우선 사용
        first_compact = first.replace(" ", "")
        if first_compact.startswith("[매도]") or first_compact.startswith("매도"):
            return "2", "매도"
        if first_compact.startswith("[매수]") or first_compact.startswith("[추가매수") or first_compact.startswith("[물타기"):
            return "1", "매수"
        if first.startswith("[sell]") or first.startswith("sell") or first.startswith("exit"):
            return "2", "매도"
        if first.startswith("[buy]") or first.startswith("buy") or first.startswith("entry"):
            return "1", "매수"

        # 2) fallback: 상단 문맥에서 충돌 없이 한쪽만 검출될 때만 실행
        buy_hit = any(tok in head for tok in buy_tokens)
        sell_hit = any(tok in head for tok in sell_tokens)
        if buy_hit and not sell_hit:
            return "1", "매수"
        if sell_hit and not buy_hit:
            return "2", "매도"

        # 충돌/미검출은 오주문 방지를 위해 스킵
        logger.warning("[order-detect] ambiguous opinion head; skip auto order")
        return None, None

    order_type, side = _detect_order_side(claude_opinion)
    if not order_type:
        logger.info(f"[{signal.stock_name}] 자동 모드: AI 홀드 — 스킵")
        return

    market_event_ctx = getattr(signal, "market_event_context", {}) or {}
    tomorrow_closed = bool(market_event_ctx.get("tomorrow_closed"))
    if order_type == "1" and tomorrow_closed:
        _log_daily_review_event(
            signal.stock_code, signal.stock_name, "applied",
            "holiday_block_new_entry",
            {"tomorrow": market_event_ctx.get("tomorrow")},
        )
        logger.info(
            f"[{signal.stock_name}] 내일 휴장({market_event_ctx.get('tomorrow')}) "
            f"이벤트로 신규 매수 차단"
        )
        return

    dr_guardrails = getattr(signal, "daily_review_guardrails", {}) or {}
    if order_type == "1" and dr_guardrails:
        if not bool(dr_guardrails.get("allow_new_entry", True)):
            _log_daily_review_event(
                signal.stock_code, signal.stock_name, "applied",
                "daily_review_block_new_entry",
                {"source_date": dr_guardrails.get("source_date")},
            )
            logger.info(
                f"[{signal.stock_name}] daily_review 가드레일로 신규 매수 차단 "
                f"(source={dr_guardrails.get('source_date')})"
            )
            return

    # 매수 직전마다 최신 예수금/실질 매수여력을 다시 산출해 stale budget 사용을 방지한다.
    if order_type == "1":
        try:
            latest_deposit = int((kiwoom.get_deposit() or {}).get("order_available") or 0)
            latest_buy_budget = latest_deposit
            try:
                from data.db import get_positions as _get_positions
                holdings_now = get_portfolio()
                positions_map = {p["stock_code"]: p for p in _get_positions()}
                add_reserve = 0
                for h in holdings_now:
                    code = str(h.get("stock_code", ""))
                    pos = positions_map.get(code)
                    qty_h = int(h.get("quantity") or 0)
                    if pos and pos.get("add_buy_price"):
                        add_reserve += int(pos["add_buy_price"]) * int(qty_h * 0.5)
                    else:
                        add_reserve += int((h.get("eval_amount") or 0) * 0.15)
                latest_buy_budget = max(0, latest_deposit - int(add_reserve))
            except Exception as _reserve_e:
                logger.warning(f"[{signal.stock_name}] 매수 직전 reserve 계산 실패, order_available 기준 사용: {_reserve_e}")

            if latest_deposit != int(deposit or 0) or latest_buy_budget != int(buy_budget or 0):
                logger.info(
                    f"[{signal.stock_name}] 매수 직전 여력 재조회 반영: "
                    f"deposit {int(deposit or 0):,}원 -> {latest_deposit:,}원, "
                    f"buy_budget {int(buy_budget or 0):,}원 -> {latest_buy_budget:,}원"
                )
            deposit = latest_deposit
            buy_budget = latest_buy_budget
        except Exception as _deposit_e:
            logger.warning(f"[{signal.stock_name}] 매수 직전 예수금 재조회 실패(기존값 사용): {_deposit_e}")

    strong_exit = _is_strong_exit_signal(signal) if order_type == "2" else False
    if order_type == "2" and strong_exit:
        _pending_weak_exit_confirm.pop(signal.stock_code, None)
    if order_type == "2" and not strong_exit:
        if not _confirm_weak_exit_ready(signal.stock_code):
            logger.info(
                f"[{signal.stock_name}] 약한 매도 신호 1차 감지 — "
                f"{int(_WORKER_CONFIG.get('weak_exit_confirm_minutes', 20))}분 내 재확인 시 실행"
            )
            return

    qty = None
    order_market = None
    order_price = 0  # 기본 시장가
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
    for line in claude_opinion.splitlines():
        if line.strip().startswith("[주문방식]") and "지정가" in line:
            # 지정가 → 현재가로 설정 (슬리피지 방지)
            if signal.current_price > 0:
                order_price = signal.current_price

    # 매도인데 추천수량 없으면 보유 전량으로 처리
    missing_qty_policy = str(_WORKER_CONFIG.get("missing_qty_policy", "fallback")).strip().lower()

    if not qty and missing_qty_policy == "fallback":
        if order_type == "2":
            holdings = get_portfolio()
            qty = next(
                (int(h.get("quantity") or 0) for h in holdings
                 if str(h.get("stock_code", "")) == signal.stock_code),
                0,
            )
            if qty > 0:
                logger.info(f"[{signal.stock_name}] 추천수량 누락(보완): 보유 전량 {qty}주 적용")

    if not qty:
        logger.info(f"[{signal.stock_name}] 수량 없음(추천수량 미기재): 주문 실행을 건너뜁니다")
        return

    if order_type == "1":
        buy_reentry_block_minutes = max(0, int(_WORKER_CONFIG.get("buy_reentry_block_minutes", 60)))
        if _has_recent_buy_trade(signal.stock_code, buy_reentry_block_minutes):
            logger.info(
                f"[{signal.stock_name}] 최근 매수 이력({buy_reentry_block_minutes}분 이내)로 재매수 차단"
            )
            return

        signal_type = str(getattr(signal, "signal_type", "") or "")
        if signal.in_portfolio and signal_type != "add":
            logger.info(
                f"[{signal.stock_name}] 보유 종목 비-add 신호에서 매수 차단 (signal_type={signal_type or 'unknown'})"
            )
            return


    # Adaptive policy gate from historical similar outcomes.
    adaptive = None
    if order_type == "1":
        try:
            from worker.adaptive_policy import get_judgment_adaptive_policy

            adaptive = get_judgment_adaptive_policy(
                signal_type=str(getattr(signal, "signal_type", "") or ""),
                kospi_rate=float(kospi_rate or 0.0),
                kosdaq_rate=float(kosdaq_rate or 0.0),
                trigger_count=len(getattr(signal, "triggered_conditions", []) or []),
            )
            if (getattr(signal, "signal_type", "") or "") == "entry" and not adaptive.allow_new_entry:
                logger.info(
                    f"[{signal.stock_name}] adaptive gate: 신규진입 차단 "
                    f"(stance={adaptive.stance}, reason={adaptive.reason})"
                )
                return
            old_qty = qty
            qty = max(1, int(round(qty * float(adaptive.qty_multiplier or 1.0))))
            if qty != old_qty:
                logger.info(
                    f"[{signal.stock_name}] adaptive qty 조정: {old_qty}주 -> {qty}주 "
                    f"(stance={adaptive.stance}, multiplier={adaptive.qty_multiplier})"
                )
        except Exception as _adaptive_e:
            logger.debug(f"[{signal.stock_name}] adaptive policy 적용 실패: {_adaptive_e}")

        try:
            is_avoid, avoid_text = _today_soft_avoid_hit(signal.stock_code, signal.stock_name)
            if is_avoid and qty > 0:
                soft_factor = float(_WORKER_CONFIG.get("avoid_targets_soft_factor", 0.5))
                soft_factor = min(1.0, max(0.1, soft_factor))
                old_qty = qty
                qty = max(1, int(round(qty * soft_factor)))
                if qty != old_qty:
                    logger.info(
                        f"[{signal.stock_name}] avoid_targets soft constraint: "
                        f"{old_qty}주 -> {qty}주 (factor={soft_factor:.2f}, avoid={avoid_text})"
                    )
        except Exception as _avoid_e:
            logger.debug(f"[{signal.stock_name}] avoid_targets soft constraint apply failed: {_avoid_e}")

        try:
            dr_mult = float(dr_guardrails.get("qty_multiplier", 1.0))
            dr_mult = min(1.0, max(0.1, dr_mult))
            old_qty = qty
            qty = max(1, int(round(qty * dr_mult)))
            if qty != old_qty:
                _log_daily_review_event(
                    signal.stock_code, signal.stock_name, "applied",
                    "daily_review_qty_multiplier",
                    {"from": old_qty, "to": qty, "multiplier": dr_mult},
                )
                logger.info(
                    f"[{signal.stock_name}] daily_review 수량계수 적용: "
                    f"{old_qty}주 -> {qty}주 (x{dr_mult:.2f})"
                )
            else:
                _log_daily_review_event(
                    signal.stock_code, signal.stock_name, "ignored",
                    "daily_review_qty_multiplier_no_change",
                    {"qty": qty, "multiplier": dr_mult},
                )
        except Exception as _dr_mult_e:
            logger.debug(f"[{signal.stock_name}] daily_review qty multiplier apply failed: {_dr_mult_e}")

    # 하드캡: 매수 — 실질 매수 여력 초과 방지
    if order_type == "1" and signal.current_price > 0:
        budget = buy_budget if buy_budget > 0 else deposit
        max_qty_cash = budget // signal.current_price
        max_qty_margin = None
        try:
            margin_check_price = order_price if order_price > 0 else signal.current_price
            margin_info = kiwoom.get_orderable_qty_by_margin(signal.stock_code, margin_check_price)
            margin_qty = int(margin_info.get("max_qty") or 0)
            max_qty_margin = margin_qty
            if margin_qty <= 0:
                logger.info(
                    f"[{signal.stock_name}] 증거금 주문가능수량이 0주로 조회되어 매수 스킵 "
                    f"(kt00011, price={margin_check_price:,})"
                )
                return
        except Exception as _margin_e:
            logger.warning(f"[{signal.stock_name}] 증거금 가능수량 조회 실패(현금 하드캡만 적용): {_margin_e}")

        max_qty = max_qty_cash if max_qty_margin is None else min(max_qty_cash, max_qty_margin)
        if qty > max_qty:
            logger.warning(
                f"[{signal.stock_name}] 추천수량 {qty}주 → {max_qty}주로 조정 "
                f"(현금기준 {max_qty_cash}주"
                f"{f', 증거금기준 {max_qty_margin}주' if max_qty_margin is not None else ''} / "
                f"매수여력 {budget:,}원 / 현재가 {signal.current_price:,}원)"
            )
            qty = max_qty
        if qty <= 0:
            logger.info(f"[{signal.stock_name}] 매수 여력 부족으로 스킵")
            return

    # 하드캡: 매도 — 보유 수량 초과 방지
    held_qty_before = 0
    if order_type == "2":
        # 전량 매도 처리 시 위에서 이미 조회했을 수 있으나 재조회해도 무방 (캐시됨)
        held_qty = next(
            (int(h.get("quantity") or 0) for h in get_portfolio()
             if str(h.get("stock_code", "")) == signal.stock_code),
            0,
        )
        held_qty_before = held_qty
        if held_qty <= 0:
            logger.info(f"[{signal.stock_name}] 미보유 종목 매도 스킵")
            return
        if qty > held_qty:
            logger.warning(
                f"[{signal.stock_name}] 매도 추천수량 {qty}주 → 보유수량 {held_qty}주로 조정"
            )
            qty = held_qty

        # Weak exit guard: avoid full liquidation on soft sell signals.
        if not strong_exit and held_qty > 0:
            partial_ratio = float(_WORKER_CONFIG.get("weak_exit_partial_ratio", 0.5))
            partial_ratio = min(1.0, max(0.1, partial_ratio))
            weak_cap = max(1, int(held_qty * partial_ratio))
            if qty > weak_cap:
                logger.info(
                    f"[{signal.stock_name}] 약한 매도 신호 — 부분매도로 제한 "
                    f"({qty}주 → {weak_cap}주, 보유 {held_qty}주)"
                )
                qty = weak_cap

    try:
        result = kiwoom.place_order(signal.stock_code, order_type, qty, price=order_price, order_market=order_market)
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
        if order_type != "1":
            reset_cooldowns_for_stock(signal.stock_code)
        if order_type == "1":
            set_add_cooldown_after_trade(signal.stock_code)
            # 매수 직후 1시간 동안 entry 신호 재발동 방지
            _post_buy_lock[signal.stock_code] = _now_kst() + _td(hours=1)
            # 매수 후 포지션 자동 생성 (portfolio_sync에서 정확한 평단가로 갱신됨)
            create_position_from_trade(signal.stock_code, signal.stock_name, signal.current_price, qty)
        if signal_id is not None:
            update_signal_action(signal_id, side)
        save_strategy_note(
            "trade",
            f"{signal.stock_name} {qty}주 {side} (자동 매매)",
            meta={
                "type": "auto_trade_execution",
                "signal_id": signal_id,
                "stock_code": signal.stock_code,
                "stock_name": signal.stock_name,
                "side": side,
                "order_type": order_type,
                "quantity": qty,
                "price_hint": signal.current_price or order_price,
                "source": getattr(signal, "source", None),
            },
        )
        from worker.portfolio_sync import sync_all as _sync
        _sync(kiwoom)

        # 매수 후 AI 포지션 판단 (목표가/손절가/추가매수가 설정)
        if order_type == "1":
            _set_position_by_ai(signal.stock_code, signal.stock_name, signal.current_price, qty)
        # 부분 익절 후 손절가 자동 상향 (잔량 보호)
        if order_type == "2" and held_qty_before > 0 and qty < held_qty_before:
            _sell_price = signal.current_price or order_price or 0
            if _sell_price > 0:
                try:
                    from data.db import get_positions as _get_pos, update_position_field as _upd_pos
                    _pos = next((p for p in _get_pos() if p.get("stock_code") == signal.stock_code), None)
                    if _pos:
                        _cur_stop = _pos.get("stop_loss_price") or 0
                        _new_stop = int(_sell_price * 0.97)
                        if _new_stop > _cur_stop:
                            _upd_pos(signal.stock_code, "stop_loss_price", _new_stop)
                            logger.info(
                                f"[{signal.stock_name}] 부분 익절 후 손절가 자동 상향: "
                                f"{_cur_stop:,}원 → {_new_stop:,}원 (매도가 {_sell_price:,}원 × 0.97)"
                            )
                            send_message(
                                f"🔒 *손절가 자동 상향*\n"
                                f"종목: *{signal.stock_name}*\n"
                                f"부분 익절 {qty}주 후 잔량 보호\n"
                                f"손절가: {_cur_stop:,}원 → {_new_stop:,}원"
                            )
                except Exception as _sl_e:
                    logger.warning(f"[{signal.stock_name}] 부분 익절 후 손절가 자동 상향 실패: {_sl_e}")
        # 전량 매도 후 watchlist 처리 (유효 결정 없으면 기본 drop)
        if order_type == "2" and held_qty_before > 0 and qty >= held_qty_before:
            _apply_post_sell_watchlist_decision(signal, claude_opinion)
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
    # Use a stable key so configured cooldown_hours is honored across hour boundaries.
    dip_key = "dip_buy:global"
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
            if isinstance(opinion, str) and opinion.strip().startswith("INCOMPLETE_CONTEXT"):
                logger.info(f"[급락 스캔] INCOMPLETE_CONTEXT skip 저장/실행: {name}")
                results.append(f"⏭ {name} 컨텍스트 부족")
                continue

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
                signal_id = save_signal(
                    fake_signal,
                    opinion,
                    in_portfolio=False,
                    source="dip_buy",
                    decision_status="normal",
                    **get_judgment_runtime_meta(),
                )
                _rag_index_signal(fake_signal, signal_id, opinion)
                _auto_execute(
                    fake_signal,
                    opinion,
                    signal_id,
                    deposit=deposit,
                    buy_budget=buy_budget,
                    kospi_rate=kospi_rate,
                    kosdaq_rate=kosdaq_rate,
                )
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

        # Guardrail: prevent ultra-tight target prices that trigger instant tiny-profit exits.
        try:
            min_target_profit_pct = float(_WORKER_CONFIG.get("min_target_profit_pct", 2.0))
        except Exception:
            min_target_profit_pct = 2.0
        try:
            min_target_profit_krw = int(_WORKER_CONFIG.get("min_target_profit_krw", 100))
        except Exception:
            min_target_profit_krw = 100

        if tp and avg_price > 0:
            min_tp_by_pct = int(round(avg_price * (1.0 + (min_target_profit_pct / 100.0))))
            min_tp = max(min_tp_by_pct, avg_price + max(0, min_target_profit_krw))
            if tp < min_tp:
                logger.info(
                    f"[{stock_name}] AI 목표가 보정: {tp:,} -> {min_tp:,} "
                    f"(min_profit={min_target_profit_pct:.2f}%, min_krw={min_target_profit_krw})"
                )
                tp = min_tp

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


def run_check(check_mode: str = "all"):
    session = "main" if TEST_MODE else get_current_session()
    if session is None:
        logger.debug("장 운영 시간 외 - 스킵")
        return

    # Run check only in regular session (both real/mock).
    if session != "main":
        logger.info(f"정규장 외 시간({session}) - run_check 스킵")
        return

    global _daily_review_guardrail_context, _daily_review_event_date
    today = _now_kst().strftime("%Y-%m-%d")
    if _daily_review_event_date != today:
        _daily_review_execution_events.clear()
        _daily_review_event_date = today
    try:
        latest_review = (get_recent_daily_reviews(limit=1, exclude_today=True) or [None])[0]
    except Exception:
        latest_review = None
    _daily_review_guardrail_context = _resolve_guardrails_from_daily_review(latest_review)
    logger.info(
        "[daily_review] intraday guardrails loaded "
        f"(source={_daily_review_guardrail_context.get('source_date') or 'N/A'}, "
        f"entry={_daily_review_guardrail_context.get('allow_new_entry')}, "
        f"qty={_daily_review_guardrail_context.get('qty_multiplier')}, "
        f"sl={_daily_review_guardrail_context.get('stop_loss_sensitivity')})"
    )

    # DB에서 최신 종목/조건 로드 (MCP로 변경 시 즉시 반영)
    stocks = [s for s in get_watchlist() if s.get("enabled", False)]
    conditions = load_conditions()

    # R/R 기반 처리 우선순위 정렬: entry 신호 경합 시 R/R 높은 종목부터 처리해 현금 배분 최적화
    # positions에 저장된 avg_price·target_price·stop_loss_price로 R/R 추정
    # 미보유(positions 없음) 종목은 watchlist의 target/stop 사용, 없으면 0으로 뒤로 밀림
    try:
        from data.db import get_positions as _get_positions_sort
        _pos_map = {p["stock_code"]: p for p in _get_positions_sort()}

        def _rr_score(stock: dict) -> float:
            code = stock.get("code", "")
            pos = _pos_map.get(code, {})
            target = pos.get("target_price") or stock.get("target_price") or 0
            stop   = pos.get("stop_loss_price") or stock.get("stop_loss_price") or 0
            avg    = pos.get("avg_price") or stock.get("avg_price") or 0
            if target and stop and avg and avg > stop:
                return (target - avg) / (avg - stop)
            return 0.0

        stocks.sort(key=_rr_score, reverse=True)
        logger.debug(f"[우선순위] R/R 기준 정렬 완료: {[s.get('name','') for s in stocks]}")
    except Exception as _sort_e:
        logger.warning(f"[우선순위] R/R 정렬 실패, 기본 순서 유지: {_sort_e}")

    if check_mode not in {"all", "entry_only", "exit_only"}:
        check_mode = "all"

    logger.info(
        f"=== 조건 체크 시작 [{session}|mode={check_mode}] "
        f"({len(stocks)}개 종목, {len(conditions)}개 조건) ==="
    )

    use_claude = _WORKER_CONFIG.get("use_ai_judgment", _WORKER_CONFIG.get("use_claude_api", True))
    ai_cache_minutes = int(_WORKER_CONFIG.get("ai_cache_minutes", 20))
    ai_calls = 0
    holdings = get_portfolio()
    holding_codes = {
        str(h.get("stock_code", ""))
        for h in holdings
        if int(h.get("quantity") or 0) > 0
    }

    # exit-only path should scan only held positions to avoid unnecessary
    # watchlist-wide checks every minute.
    if check_mode == "exit_only":
        stocks = [s for s in stocks if str(s.get("code", "")) in holding_codes]
        if not stocks:
            logger.info("=== 조건 체크 완료 [mode=exit_only] === (보유 종목 없음, 스킵)")
            return

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
    market_event_ctx = _get_market_event_context()
    if market_event_ctx.get("tomorrow_closed"):
        logger.info(
            f"[market-event] tomorrow={market_event_ctx.get('tomorrow')} closed "
            f"(reason={market_event_ctx.get('tomorrow_closed_reason')})"
        )

    for stock in stocks:
        time.sleep(1)
        signal = check_stock(kiwoom, stock, conditions, holdings)
        if signal:
            signal_type = str(getattr(signal, "signal_type", "") or "").strip().lower()
            in_portfolio = bool(getattr(signal, "in_portfolio", False))

            if check_mode == "exit_only":
                if (not in_portfolio) or (signal_type not in {"exit", "both"}):
                    continue
            elif check_mode == "entry_only":
                if signal_type not in {"entry", "both", "add"}:
                    continue

            setattr(signal, "market_event_context", market_event_ctx)
            setattr(signal, "daily_review_core3", _daily_review_guardrail_context.get("daily_review_core3", []))
            setattr(signal, "daily_review_guardrails", _daily_review_guardrail_context)
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
            setattr(
                signal,
                "stop_loss_sensitivity",
                str((_daily_review_guardrail_context or {}).get("stop_loss_sensitivity", "유지")),
            )
            if bool(getattr(signal, "in_portfolio", False)):
                _sl_mode = str(getattr(signal, "stop_loss_sensitivity", "유지"))
                if _sl_mode != "유지":
                    _log_daily_review_event(
                        signal.stock_code, signal.stock_name, "applied",
                        "daily_review_stop_loss_sensitivity",
                        {"mode": _sl_mode, "signal_type": getattr(signal, "signal_type", "")},
                    )
                else:
                    _log_daily_review_event(
                        signal.stock_code, signal.stock_name, "ignored",
                        "daily_review_stop_loss_sensitivity_default",
                        {"mode": _sl_mode, "signal_type": getattr(signal, "signal_type", "")},
                    )
            logger.info(f"[{signal.stock_name}] 신호 감지: {new_conditions}")

            claude_opinion = None
            if use_claude:
                try:
                    # ── 하네스: 처리 경로 결정 ──
                    from worker.claude_judge import (
                        harness_check,
                        HARNESS_SKIP, HARNESS_DIRECT_SELL, HARNESS_AMBIGUOUS,
                    )
                    harness_result = harness_check(signal, holdings)

                    if harness_result == HARNESS_SKIP:
                        logger.info(f"[{signal.stock_name}] 하네스 SKIP — 신호 무시")
                        continue

                    if harness_result == HARNESS_DIRECT_SELL:
                        claude_opinion = (
                            "[매도]\n"
                            "• 근거1: 손절가 이탈 — 하네스 직행 처리"
                        )
                        logger.info(f"[{signal.stock_name}] 하네스 DIRECT_SELL → 풀 모델 생략")

                    else:  # AMBIGUOUS → 기존 풀 모델 경로
                        cache_allowed = (
                            ai_cache_minutes > 0
                            and not bool(getattr(signal, "in_portfolio", False))
                            and str(getattr(signal, "signal_type", "") or "") in ("entry", "both")
                        )
                        cached_opinion = _get_cached_ai_opinion(signal, ai_cache_minutes) if cache_allowed else None
                        if cached_opinion:
                            claude_opinion = cached_opinion
                            logger.info(f"[{signal.stock_name}] AI 판단 캐시 재사용 ({ai_cache_minutes}분 TTL)")
                        else:
                            sector = kiwoom.get_sector_index(signal.sector_code) if signal.sector_code else {}
                            claude_opinion = get_trade_opinion(
                                signal, holdings, kospi, kosdaq, sector, signal.recent_trades, deposit=deposit
                            )
                            ai_calls += 1
                            if cache_allowed:
                                _set_cached_ai_opinion(signal, claude_opinion)
                            logger.info(f"[{signal.stock_name}] AI 판단: {claude_opinion[:80]}...")

                    # 준강제: 홀드 + 전환조건 충족 시 "강제 주문" 대신 재판단(근거/수량 재계산)
                    semi_force_hint = _check_semiforce_transition(signal, claude_opinion)
                    if semi_force_hint:
                        try:
                            setattr(signal, "transition_hint", semi_force_hint)
                            sector = kiwoom.get_sector_index(signal.sector_code) if signal.sector_code else {}
                            claude_opinion = get_trade_opinion(
                                signal, holdings, kospi, kosdaq, sector, signal.recent_trades, deposit=deposit
                            )
                            ai_calls += 1
                            logger.info(
                                f"[{signal.stock_name}] 전환조건 기반 재판단 완료 "
                                f"(target={semi_force_hint.get('target')}, parsed={semi_force_hint.get('parsed_count')})"
                            )
                        finally:
                            try:
                                delattr(signal, "transition_hint")
                            except Exception:
                                pass
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

            incomplete_context = bool(
                isinstance(claude_opinion, str) and claude_opinion.strip().startswith("INCOMPLETE_CONTEXT")
            )
            trace = get_last_agent_trace() if claude_opinion else {}
            tool_sequence = list((trace or {}).get("tool_sequence") or [])
            reasoning_chain = list((trace or {}).get("reasoning_chain") or [])

            decision_status = str((trace or {}).get("decision_status") or "normal").strip().lower()
            if incomplete_context:
                decision_status = "incomplete_context"
            elif decision_status not in {"normal", "fallback"}:
                decision_status = "normal"

            runtime_meta = get_judgment_runtime_meta()
            model_id = str((trace or {}).get("model_id") or runtime_meta.get("model_id") or "")
            prompt_version = str((trace or {}).get("prompt_version") or runtime_meta.get("prompt_version") or "")
            policy_version = str((trace or {}).get("policy_version") or "")
            decision_confidence = _extract_decision_confidence(claude_opinion, trace)

            stock_feature_snapshot = None
            try:
                price_payload = kiwoom.get_current_price(signal.stock_code) or {}
                stock_feature_snapshot = _build_stock_feature_snapshot(signal, stock, price_payload)
            except Exception as _snap_e:
                logger.debug(f"[{signal.stock_name}] stock snapshot 생성 실패: {_snap_e}")

            mark_sent(signal.stock_code, new_ids)
            if incomplete_context:
                logger.info(
                    f"[{signal.stock_name}] INCOMPLETE_CONTEXT로 저장/실행 차단 "
                    f"(opinion={claude_opinion[:120]})"
                )
                continue

            signal_id = save_signal(
                signal,
                claude_opinion,
                in_portfolio=signal.in_portfolio,
                dart_summary=dart_summary,
                news_summary=news_summary,
                market_snapshot=market_snapshot,
                portfolio_snapshot=portfolio_snapshot,
                source="monitor",
                model_id=model_id,
                prompt_version=prompt_version,
                policy_version=(policy_version or None),
                decision_status=decision_status,
                decision_confidence=decision_confidence,
                stock_feature_snapshot=stock_feature_snapshot,
            )
            # Agent 모드 실행 시 tool_sequence + reasoning_chain 저장 + 텔레그램 흐름 전송
            agent_tools_summary = None
            if claude_opinion:
                try:
                    update_signal_agent_trace(
                        signal_id,
                        tool_sequence,
                        reasoning_chain,
                    )
                    save_agent_action_log(
                        signal_id=signal_id,
                        stock_code=signal.stock_code,
                        stock_name=signal.stock_name,
                        signal_type=getattr(signal, "signal_type", None),
                        tool_sequence=tool_sequence,
                        reasoning_chain=reasoning_chain,
                        final_opinion=claude_opinion,
                    )
                    if tool_sequence:
                        agent_tools_summary = " → ".join(tool_sequence)
                except Exception as _e:
                    logger.debug(f"[AgentTrace] 저장 실패: {_e}")
            _rag_index_signal(signal, signal_id, claude_opinion,
                              dart_summary=dart_summary, news_summary=news_summary)
            send_signal_alert(signal, claude_opinion, holdings=holdings, signal_id=signal_id, auto_mode=AUTO_TRADE)
            if agent_tools_summary:
                send_message(f"🔍 *분석 경로* ({signal.stock_name})\n{agent_tools_summary}")

            if claude_opinion:
                _maybe_save_hold_conditions(signal, claude_opinion)

            if claude_opinion:
                if AUTO_TRADE:
                    def _idx_rate(d):
                        try:
                            for k in ("flu_rt", "prdy_ctrt", "change_rate"):
                                v = (d or {}).get(k)
                                if v is not None and str(v).strip() != "":
                                    return float(str(v).replace(",", "").strip())
                        except Exception:
                            pass
                        return 0.0
                    _auto_execute(
                        signal,
                        claude_opinion,
                        signal_id,
                        deposit=deposit,
                        buy_budget=buy_budget,
                        kospi_rate=_idx_rate(kospi),
                        kosdaq_rate=_idx_rate(kosdaq),
                    )
                else:
                    _paper_execute(signal, claude_opinion, signal_id)

    logger.info(f"=== 조건 체크 완료 [mode={check_mode}] === (AI calls: {ai_calls})")


def main():
    interval = _WORKER_CONFIG.get("interval_seconds", 60)
    entry_interval = int(_WORKER_CONFIG.get("entry_interval_seconds", interval))
    exit_interval = int(_WORKER_CONFIG.get("exit_interval_seconds", 60))
    init_db()

    logger.info("포트폴리오 초기 동기화 중...")
    sync_all(kiwoom)

    logger.info("텔레그램 봇 시작...")
    start_bot_thread(kiwoom_client=kiwoom)

    interval_min = max(1, entry_interval // 60)
    exit_interval_min = max(1, exit_interval // 60)
    sync_realtime_minutes = max(1, int(_WORKER_CONFIG.get("sync_realtime_minutes", 2)))
    _trade_env = "모의투자" if kiwoom._is_mock else "실전투자"
    _trade_mode = "자동매매" if AUTO_TRADE else "수동(알림)"
    logger.info(
        "워커 시작 - "
        f"entry {entry_interval}초 / exit {exit_interval}초 간격 실행 (평일 08:00~18:00)"
    )
    send_message(f"✅ 워커 시작 [{_trade_env} | {_trade_mode}]")

    def auto_sync():
        logger.info("포트폴리오 자동 동기화")
        sync_all(kiwoom)

    scheduler = BackgroundScheduler(timezone="Asia/Seoul")
    # 평일 08:00~18:59 사이에만 실행
    scheduler.add_job(lambda: _run_on_open_day("run_check_entry", run_check, "entry_only"), "cron",
                      day_of_week="mon-fri", hour="8-18", minute=f"*/{interval_min}",
                      id="monitor_entry")
    scheduler.add_job(lambda: _run_on_open_day("run_check_exit", run_check, "exit_only"), "cron",
                      day_of_week="mon-fri", hour="8-18", minute=f"*/{exit_interval_min}",
                      id="monitor_exit")
    scheduler.add_job(lambda: _run_on_open_day("reset_all_cooldowns", reset_all_cooldowns), "cron", hour=9, minute=0, id="reset_cooldowns")
    scheduler.add_job(lambda: _run_on_open_day("sync_premarket", auto_sync), "cron", hour=8, minute=30, id="sync_premarket")
    scheduler.add_job(lambda: _run_on_open_day("sync_open", auto_sync), "cron", hour=9, minute=1, id="sync_open")
    scheduler.add_job(lambda: _run_on_open_day("sync_close", auto_sync), "cron", hour=18, minute=5, id="sync_close")
    pre_hh, pre_mm = _parse_hhmm(_WORKER_CONFIG.get("premarket_report_time", "08:50"), 8, 50)
    open_hh, open_mm = _parse_hhmm(_WORKER_CONFIG.get("opening_report_time", "09:05"), 9, 5)
    scheduler.add_job(lambda: _run_on_open_day("run_premarket_report", run_premarket_report), "cron",
                      day_of_week="mon-fri", hour=pre_hh, minute=pre_mm,
                      id="premarket_report")
    scheduler.add_job(lambda: _run_on_open_day("run_opening_report", run_opening_report), "cron",
                      day_of_week="mon-fri", hour=open_hh, minute=open_mm,
                      id="opening_report")
    scheduler.add_job(lambda: _run_on_open_day("sync_realtime", auto_sync), "cron",
                      day_of_week="mon-fri", hour="8-18", minute=f"*/{sync_realtime_minutes}",
                      id="sync_realtime")
    scheduler.add_job(lambda: _run_on_open_day("update_signal_results", update_signal_results), "cron",
                      day_of_week="mon-fri", hour="9-18", minute="*/30",
                      id="result_update")
    scheduler.add_job(lambda: _run_on_open_day("update_screening_results", update_screening_results), "cron",
                      day_of_week="mon-fri", hour="9-18", minute="*/30",
                      id="screening_result_update")
    scheduler.add_job(lambda: _run_on_open_day("check_trailing_stops", check_trailing_stops), "cron",
                      day_of_week="mon-fri", hour="9-15", minute="*/30",
                      id="trailing_stops")
    scheduler.add_job(lambda: _run_on_open_day("reassess_watchlist", reassess_watchlist, kiwoom), "cron",
                      day_of_week="mon-fri", hour=9, minute=15,
                      id="reassess_watchlist")
    scheduler.add_job(lambda: _run_on_open_day("check_inactive_stocks", check_inactive_stocks), "cron",
                      day_of_week="mon-fri", hour=8, minute=30,
                      id="inactive_alert")
    scheduler.add_job(lambda: _run_on_open_day("check_removal_candidates", check_removal_candidates), "cron",
                      day_of_week="mon-fri", hour="9-15", minute="*/30",
                      id="removal_check")
    scheduler.add_job(lambda: _run_on_open_day("check_market_dip", check_market_dip), "cron",
                      day_of_week="mon-fri", hour="9-14", minute="*/30",
                      id="dip_buy")
    scheduler.add_job(lambda: _run_on_open_day("run_intraday_scan", run_intraday_scan), "cron",
                      day_of_week="mon-fri", hour=11, minute=0,
                      id="intraday_scan")
    scheduler.add_job(lambda: _run_on_open_day("run_daily_screening", run_daily_screening), "cron",
                      day_of_week="mon-fri", hour=15, minute=40,
                      id="daily_screening")
    scheduler.add_job(lambda: _run_on_open_day("run_daily_review_with_checklist", run_daily_review_with_checklist), "cron",
                      day_of_week="mon-fri", hour=16, minute=10,
                      id="daily_review")
    scheduler.add_job(lambda: _run_on_open_day("run_weekly_performance_report", run_weekly_performance_report), "cron",
                      day_of_week="mon", hour=9, minute=0,
                      id="weekly_performance_report")
    scheduler.add_job(lambda: _run_on_open_day("run_weekly_self_correction", run_weekly_self_correction), "cron",
                      day_of_week="mon", hour=9, minute=5,
                      id="weekly_self_correction")
    scheduler.add_job(lambda: _run_on_open_day("run_reflection_policy_cycle", run_reflection_policy_cycle), "cron",
                      day_of_week="mon-fri", hour=16, minute=20,
                      id="daily_reflection_policy_loop")
    scheduler.add_job(lambda: _run_on_open_day("run_agent_action_log_refresh", run_agent_action_log_refresh), "cron",
                      day_of_week="mon", hour=9, minute=10,
                      id="weekly_agent_action_log_refresh")
    scheduler.add_job(lambda: _run_on_open_day("update_trade_results", update_trade_results), "cron",
                      day_of_week="mon-fri", hour="9-18", minute="*/30",
                      id="trade_result_update")
    news_monitor_times = str(_WORKER_CONFIG.get("news_monitor_times", "8:55,12:00") or "").strip()
    for idx, token in enumerate(news_monitor_times.split(","), start=1):
        t = token.strip()
        if not t or ":" not in t:
            continue
        hh, mm = t.split(":", 1)
        try:
            hour_i = max(0, min(23, int(hh)))
            min_i = max(0, min(59, int(mm)))
        except ValueError:
            continue
        scheduler.add_job(
            lambda: _run_on_open_day("run_news_monitor", run_news_monitor),
            "cron",
            day_of_week="mon-fri",
            hour=hour_i,
            minute=min_i,
            id=f"news_monitor_fixed_{idx}",
        )
    if not bool(_WORKER_CONFIG.get("rag_realtime_index", False)):
        rag_batch_times = str(_WORKER_CONFIG.get("rag_batch_times", "") or "").strip()
        rag_batch_hours = str(_WORKER_CONFIG.get("rag_batch_hours", "") or "").strip()
        rag_batch_minute = max(0, min(59, int(_WORKER_CONFIG.get("rag_batch_minute", 5))))
        if rag_batch_times:
            # Example: "5:50,13:05,16:05"
            for idx, token in enumerate(rag_batch_times.split(","), start=1):
                t = token.strip()
                if not t or ":" not in t:
                    continue
                hh, mm = t.split(":", 1)
                try:
                    hour_i = max(0, min(23, int(hh)))
                    min_i = max(0, min(59, int(mm)))
                except ValueError:
                    continue
                scheduler.add_job(
                    lambda: _run_on_open_day("run_rag_batch_index", run_rag_batch_index),
                    "cron",
                    day_of_week="mon-fri",
                    hour=hour_i,
                    minute=min_i,
                    id=f"rag_batch_index_fixed_{idx}",
                    max_instances=1,
                    coalesce=True,
                )
        elif rag_batch_hours:
            scheduler.add_job(
                lambda: _run_on_open_day("run_rag_batch_index", run_rag_batch_index),
                "cron",
                day_of_week="mon-fri",
                hour=rag_batch_hours,
                minute=rag_batch_minute,
                id="rag_batch_index_fixed",
                max_instances=1,
                coalesce=True,
            )
        else:
            logger.info("[RAG] 배치 시간(rag_batch_times/rag_batch_hours)이 없어 스케줄 등록을 생략합니다.")
        if bool(_WORKER_CONFIG.get("rag_batch_run_close", False)):
            # Optional close-time catch-up run
            scheduler.add_job(
                lambda: _run_on_open_day("run_rag_batch_index_close", run_rag_batch_index),
                "cron",
                day_of_week="mon-fri",
                hour=18,
                minute=20,
                id="rag_batch_index_close",
                max_instances=1,
                coalesce=True,
            )
    _run_on_open_day("startup_run_check", run_check)

    for sig_name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handle_shutdown_signal)
        except Exception:
            pass

    scheduler.start()
    logger.info("[shutdown] worker loop started")
    try:
        while not _shutdown_event.is_set():
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        _shutdown_event.set()
    finally:
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            pass
        logger.info("worker terminated")
        send_message("🛑 Quant Trading 워커가 종료되었습니다.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="장 시간 체크 무시하고 즉시 실행")
    parser.add_argument("--env", type=str, default=None, help=".env 파일 경로 (예: .env.real)")
    args = parser.parse_args()

    if args.test:
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
