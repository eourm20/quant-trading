"""
신호/매매/스크리닝 결과 수익률 업데이트 및 실현손익 동기화.
stock_analyzer.py에서도 임포트하므로 worker.main에 대한 의존성 없음.
"""

import logging
import time
from datetime import datetime, timedelta as _td

from data.db import (
    save_realized_pnl_snapshot,
    update_signal_result,
    update_signal_agent_trace,
)
from worker.worker_utils import (
    _now_kst,
    _safe_int_price,
    _parse_ymd,
    _business_days_elapsed,
    get_current_session,
)

logger = logging.getLogger(__name__)

# KiwoomClient 인스턴스 — main.py의 init()으로 주입
_kiwoom = None


def init(kiwoom_client) -> None:
    """main() 시작 시 한 번 호출해 kiwoom 클라이언트를 주입한다."""
    global _kiwoom
    _kiwoom = kiwoom_client


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _resolve_eval_price(stock_code: str, target_dt, today_dt):
    """성과 평가 가격 결정.
    - target이 오늘이고 정규장(main) 진행 중이면 현재가
    - 그 외에는 target일(없으면 직전 영업일) 종가
    """
    target_ymd = target_dt.strftime("%Y%m%d")
    today_ymd = today_dt.strftime("%Y%m%d")

    if target_ymd == today_ymd and get_current_session() == "main":
        pd = _kiwoom.get_current_price(stock_code)
        now_price = _safe_int_price(pd.get("cur_prc") or pd.get("stk_prpr") or pd.get("prpr"))
        return now_price, "current"

    daily = _kiwoom.get_daily_ohlcv(stock_code, period=40) or []
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


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------

def update_signal_results():
    """신호 발생 후 1일/3일/5일/10일 결과 수익률 업데이트 (영업일 기준)."""
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
                pd = _kiwoom.get_current_price(row["stock_code"])
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
    """실거래 1일/3일/5일 성과 업데이트 (매수/매도 방향 반영)."""
    from data.db import get_conn, update_trade_result

    periods = [("1d", 1, "result_1d"), ("3d", 3, "result_3d"), ("5d", 5, "result_5d")]
    now_kst = _now_kst()
    today_kst = now_kst.date()

    for period_name, days_after, col_name in periods:
        eligible_to = (today_kst - _td(days=days_after)).strftime("%Y-%m-%d")
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
        today = _kiwoom.get_realized_pnl_today()
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
        period = _kiwoom.get_realized_pnl_period(days=30)
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
    """스크리닝 종목의 7일/30일 후 수익률 자동 업데이트."""
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