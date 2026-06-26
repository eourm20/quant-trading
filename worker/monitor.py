"""
모니터링 엔진 - 조건 감지 (conditions.yaml 기반 동적 평가)
"""

import logging
from dataclasses import dataclass, field as dc_field
from worker.clients.kiwoom_client import KiwoomClient
from worker.indicators import calculate_rsi, calculate_volume_ratio, calculate_chart_summary, ChartSummary

logger = logging.getLogger(__name__)

TREND_FOLLOW_RSI_MIN = 55.0
TREND_FOLLOW_RSI_MAX = 75.0
TREND_FOLLOW_VOLUME_MIN = 1.2
TREND_FOLLOW_BREAKOUT_MIN_5D_PCT = 20.0
TREND_FOLLOW_BREAKOUT_VOLUME_MIN = 2.0


def load_conditions() -> list[dict]:
    from data.db import get_conditions
    return get_conditions()


@dataclass
class Signal:
    stock_code: str
    stock_name: str
    current_price: int
    triggered_conditions: list[str]  # 텔레그램/DB용 메시지
    triggered_ids: list[str]         # 쿨다운 관리용 조건 ID
    rsi: float | None
    volume_ratio: float | None
    chart: ChartSummary | None = None
    sector_code: str | None = None
    in_portfolio: bool = False
    signal_type: str = ""            # entry / exit / add / both
    target_price: int = 0
    stop_loss_price: int = 0
    strategy_note: str = ""          # 관심목록 등록 근거 메모
    horizon: str = ""                # 단기 / 중기 / 장기
    recent_trades: list[dict] = dc_field(default_factory=list)  # 최근 3일 매매 이력
    avg_price: int = 0
    add_signal_mode: str = ""


def _parse_price(value: str | int | None) -> int:
    if value is None:
        return 0
    return abs(int(str(value).replace(",", "").strip() or "0"))


def _evaluate_condition(
    cond_def: dict,
    stock_cond: dict,
    current_price: int,
    rsi: float | None,
    volume_ratio: float | None,
    chart: ChartSummary | None,
    rsi_intraday: float | None = None,
) -> str | None:
    """조건 평가. 트리거되면 메시지 반환, 아니면 None."""
    evaluator = cond_def.get("evaluator")
    param = cond_def.get("param")
    msg_template = cond_def.get("message", "")

    fmt = {
        "price": current_price,
        "rsi": rsi or 0,
        "rsi_intraday": rsi_intraday or 0,
        "ratio": volume_ratio or 0,
        "threshold": 0,
        "ma5": int(chart.ma5) if chart and chart.ma5 else 0,
        "ma20": int(chart.ma20) if chart and chart.ma20 else 0,
        "macd": chart.macd_line or 0 if chart else 0,
        "signal": chart.macd_signal or 0 if chart else 0,
        "upper": int(chart.bollinger_upper) if chart and chart.bollinger_upper else 0,
        "lower": int(chart.bollinger_lower) if chart and chart.bollinger_lower else 0,
        "stoch_k": chart.stochastic_k or 0 if chart else 0,
        "stoch_d": chart.stochastic_d or 0 if chart else 0,
        "cci": chart.cci or 0 if chart else 0,
    }

    try:
        if evaluator == "price_gte":
            threshold = stock_cond.get(param)
            if threshold and current_price >= threshold:
                fmt["threshold"] = threshold
                return msg_template.format(**fmt)

        elif evaluator == "price_lte":
            threshold = stock_cond.get(param)
            if threshold and current_price <= threshold:
                fmt["threshold"] = threshold
                return msg_template.format(**fmt)

        elif evaluator == "rsi_gte":
            threshold = stock_cond.get(param)
            if rsi is not None and threshold and rsi >= threshold:
                fmt["threshold"] = threshold
                return msg_template.format(**fmt)

        elif evaluator == "rsi_lte":
            threshold = stock_cond.get(param)
            if rsi is not None and threshold and rsi <= threshold:
                fmt["threshold"] = threshold
                return msg_template.format(**fmt)

        elif evaluator == "rsi_lte_intraday":
            threshold = stock_cond.get(param)
            if rsi_intraday is not None and threshold and rsi_intraday <= threshold:
                fmt["threshold"] = threshold
                return msg_template.format(**fmt)

        elif evaluator == "volume_gte":
            threshold = stock_cond.get(param)
            if volume_ratio is not None and threshold and volume_ratio >= threshold:
                return msg_template.format(**fmt)

        elif evaluator == "cci_gte":
            threshold = stock_cond.get(param)
            if chart and chart.cci is not None and threshold and chart.cci >= threshold:
                fmt["threshold"] = threshold
                return msg_template.format(**fmt)

        elif evaluator == "cci_lte":
            threshold = stock_cond.get(param)
            if chart and chart.cci is not None and threshold and chart.cci <= threshold:
                fmt["threshold"] = threshold
                return msg_template.format(**fmt)

        elif evaluator == "flag":
            enabled = stock_cond.get(param)
            chart_field = cond_def.get("chart_field")
            if enabled and chart and getattr(chart, chart_field, False):
                return msg_template.format(**fmt)

        elif evaluator == "trend_follow_entry":
            enabled = stock_cond.get(param)
            # 신규 조건 롤아웃: 값이 비어있는 기존 watchlist 행도 기본적으로 활성 취급
            if enabled is None:
                enabled = True
            if not enabled:
                return None
            if chart is None or rsi is None or volume_ratio is None:
                return None
            if not (chart.above_ma5 and chart.above_ma20):
                return None
            if chart.trend != "상승":
                return None
            normal_rsi_ok = TREND_FOLLOW_RSI_MIN <= rsi <= TREND_FOLLOW_RSI_MAX
            normal_vol_ok = volume_ratio >= TREND_FOLLOW_VOLUME_MIN
            if normal_rsi_ok and normal_vol_ok:
                return msg_template.format(**fmt)

            # 과열 구간(RSI 상단 초과)이라도 20일 신고가 돌파 + 거래량 급증일 때는 추격 진입 예외 허용
            breakout_enabled = bool(stock_cond.get("new_high_20d"))
            breakout_vol_threshold = stock_cond.get("volume_surge_ratio") or TREND_FOLLOW_BREAKOUT_VOLUME_MIN
            breakout_vol_threshold = max(float(breakout_vol_threshold), TREND_FOLLOW_BREAKOUT_VOLUME_MIN)
            breakout_price_ok = (
                chart.price_change_5d is not None
                and chart.price_change_5d >= TREND_FOLLOW_BREAKOUT_MIN_5D_PCT
            )
            breakout_ok = (
                breakout_enabled
                and bool(chart.new_high_20d)
                and bool(chart.above_ma20)
                and volume_ratio >= breakout_vol_threshold
                and breakout_price_ok
                and rsi >= TREND_FOLLOW_RSI_MAX
            )
            if breakout_ok:
                return (
                    f"신고가 돌파 추격 진입 (RSI {rsi:.1f}, 거래량 {volume_ratio:.1f}배, "
                    f"5일상승 {chart.price_change_5d:.1f}%)"
                )
            return None

    except (KeyError, ValueError) as e:
        logger.warning(f"조건 메시지 포맷 오류 [{cond_def.get('id')}]: {e}")

    return None


def _classify_add_signal(triggered_ids: list[str], current_price: int, avg_price: int) -> str:
    if not triggered_ids or not avg_price:
        return ""

    if "ma5_recovery_add" in triggered_ids:
        return "momentum_add" if current_price > avg_price else ""

    dip_add_ids = {"rsi_oversold_add", "bollinger_lower_break_add"}
    if any(cond_id in dip_add_ids for cond_id in triggered_ids):
        return "averaging_down" if current_price < avg_price else ""

    return ""


def check_stock(
    client: KiwoomClient,
    stock: dict,
    conditions: list[dict] | None = None,
    holdings: list[dict] | None = None,
) -> Signal | None:
    if conditions is None:
        conditions = load_conditions()

    code = stock["code"]
    name = stock["name"]
    cond = stock  # 정규화: 조건 필드가 stock dict에 직접 포함

    holding_codes = {str(h.get("stock_code", "")) for h in (holdings or [])}
    in_portfolio = code in holding_codes

    # 보유 종목이면 positions 테이블에서 포지션 관리 데이터 로드
    position_data = None
    if in_portfolio:
        from data.db import get_position
        position_data = get_position(code)

    # 보유 여부에 따라 적용할 signal_type 결정
    # in_portfolio=True  → exit, both 조건만 평가
    # in_portfolio=False → entry, both 조건만 평가
    allowed_types = {"exit", "add", "both"} if in_portfolio else {"entry", "both"}

    try:
        price_data = client.get_current_price(code)
        current_price = _parse_price(
            price_data.get("cur_prc")
            or price_data.get("stk_prpr")
            or price_data.get("prpr")
        )

        if current_price == 0:
            logger.warning(f"[{name}] 현재가 파싱 실패. 응답 키: {list(price_data.keys())}")
            return None

        sector_code = str(price_data.get("upjong_cd") or "").strip() or None
        if sector_code and not stock.get("sector_code"):
            try:
                from data.db import update_stock_field
                update_stock_field(code, "sector_code", sector_code)
            except Exception:
                pass

        # 일목균형표(52일) + 차트패턴(60일+) + 여유분으로 90일치 데이터 조회
        daily_data = client.get_daily_ohlcv(code, period=90)
        close_prices, high_prices, low_prices, open_prices, volumes = [], [], [], [], []
        for d in daily_data:
            cp = _parse_price(d.get("cur_prc"))
            hp = _parse_price(d.get("high_pric"))
            lp = _parse_price(d.get("lwst_pric") or d.get("low_pric"))
            op = _parse_price(d.get("strt_pric") or d.get("opn_pric"))
            vol = _parse_price(d.get("trde_qty"))
            if cp:
                close_prices.append(cp)
            if hp:
                high_prices.append(hp)
            if lp:
                low_prices.append(lp)
            if op:
                open_prices.append(op)
            if vol:
                volumes.append(vol)

        # horizon별 RSI 기간 차등 적용
        horizon = stock.get("horizon", "")
        rsi_period = {"단기": 7, "장기": 21}.get(horizon, 14)
        rsi = calculate_rsi(close_prices, period=rsi_period) if len(close_prices) >= rsi_period + 1 else None
        volume_ratio = calculate_volume_ratio(volumes) if len(volumes) >= 21 else None
        chart = calculate_chart_summary(
            close_prices, high_prices, current_price,
            low_prices=low_prices, open_prices=open_prices, volumes=volumes,
        ) if len(close_prices) >= 5 else None

        # 단기 종목: 5분봉 RSI 추가 계산
        rsi_intraday = None
        if horizon == "단기":
            try:
                intraday_data = client.get_intraday_ohlcv(code, tic_scope="5", period=30)
                intraday_prices = [
                    abs(int(str(d.get("cur_prc", 0)).replace(",", "")))
                    for d in intraday_data
                    if d.get("cur_prc")
                ]
                intraday_prices = [p for p in intraday_prices if p > 0]
                rsi_intraday = calculate_rsi(intraday_prices) if len(intraday_prices) >= 15 else None
                if rsi_intraday is not None:
                    logger.debug(f"[{name}] 5분봉 RSI: {rsi_intraday}")
            except Exception as e:
                logger.warning(f"[{name}] 5분봉 데이터 조회 실패: {e}")

        avg_price = 0
        if in_portfolio:
            avg_price = next(
                (_parse_price(h.get("avg_price")) for h in (holdings or []) if str(h.get("stock_code", "")) == code),
                0,
            )

        # 보유 종목: positions 데이터를 조건 평가용에 merge
        # target_price, stop_loss_price는 positions에서, 나머지는 watchlist에서
        eval_cond = dict(cond)
        if in_portfolio and position_data:
            for pf in ("target_price", "stop_loss_price"):
                pv = position_data.get(pf, 0)
                if pv:
                    eval_cond[pf] = pv

        triggered_msgs = []
        triggered_ids = []

        # 손절가 이탈 — 다른 조건 없어도 즉시 exit 신호 생성 (#124)
        # 쿨다운 필터보다 앞에서 잡아야 하므로 conditions 루프 전에 체크
        _sl_check = eval_cond.get("stop_loss_price") or 0
        if in_portfolio and _sl_check > 0 and current_price <= _sl_check:
            triggered_msgs.append(f"손절가 이탈 ({current_price:,}원 ≤ {_sl_check:,}원)")
            triggered_ids.append("stop_loss_breach")

        for cond_def in conditions:
            if cond_def.get("signal_type", "both") not in allowed_types:
                continue
            # rsi_lte_intraday 조건은 단기 종목에만 평가
            if cond_def.get("evaluator") == "rsi_lte_intraday" and horizon != "단기":
                continue
            msg = _evaluate_condition(cond_def, eval_cond, current_price, rsi, volume_ratio, chart, rsi_intraday)
            if msg:
                triggered_msgs.append(msg)
                triggered_ids.append(cond_def["id"])

        if triggered_msgs:
            # 트리거된 조건들의 signal_type 중 대표값 결정
            # entry > exit > add > both 우선순위 (가장 구체적인 것)
            type_priority = {"entry": 0, "exit": 1, "add": 2, "both": 3}
            triggered_types = [
                c.get("signal_type", "both")
                for c in conditions
                if c["id"] in triggered_ids
            ]
            # stop_loss_breach는 conditions 테이블에 없는 합성 ID — 항상 exit 처리
            if "stop_loss_breach" in triggered_ids:
                triggered_types.append("exit")
            rep_signal_type = min(triggered_types, key=lambda t: type_priority.get(t, 9), default="")
            add_signal_mode = _classify_add_signal(triggered_ids, current_price, avg_price) if rep_signal_type == "add" else ""

            if rep_signal_type == "add" and not add_signal_mode:
                logger.info(f"[{name}] add 신호 감지됐으나 평단 조건 불일치로 스킵")
                return None

            # MA20 하향이탈 상태에서 물타기 차단 (#129, #130)
            # exit 쿨다운 중 add만 단독 발화해도 MA20 아래면 억제
            if rep_signal_type == "add" and chart and chart.above_ma20 is False:
                logger.info(f"[{name}] add 신호 감지됐으나 MA20 하향이탈 중 — 물타기 금지")
                return None

            # 물타기(averaging_down) 최대 1회 원칙 하드 강제 (#120, #123)
            # 현재 포지션 내 매수 횟수가 2회 이상이면 추가 물타기 차단
            if rep_signal_type == "add" and add_signal_mode == "averaging_down":
                from data.db import get_add_buy_count
                _buy_cnt = get_add_buy_count(code)
                if _buy_cnt >= 2:
                    logger.info(
                        f"[{name}] averaging_down 신호 감지됐으나 물타기 1회 한도 초과"
                        f" (현재 포지션 내 매수 {_buy_cnt}회) — 스킵"
                    )
                    return None

            # RSI 과매도 entry: 확인 캔들 요구 (#118)
            # 전일 종가 대비 현재가가 회복 중일 때만 entry 허용 (낙하 중 매수 방지)
            rsi_entry_ids = {"rsi_oversold", "rsi_lte_intraday"}
            if (
                rep_signal_type == "entry"
                and any(cid in rsi_entry_ids for cid in triggered_ids)
                and len(close_prices) >= 2
                and current_price <= close_prices[0]
            ):
                logger.info(
                    f"[{name}] RSI entry 신호 감지됐으나 확인 캔들 없음"
                    f" (현재가 {current_price:,} ≤ 전일종가 {close_prices[0]:,}) — 스킵"
                )
                return None

            # entry 기준 강화: 연속형 조건만으로 발동 시 MA20 하락 과도 구간 차단 (#139)
            # 전환형(골든크로스·볼린저·MACD·일목 등) 없이 연속형(RSI/CCI/거래량)만 트리거된 경우
            # MA20 대비 -5% 이상 하락 중이면 추가 하락 초입 진입 방지
            _transition_ids = {
                "golden_cross", "macd_golden_cross", "bollinger_lower_break",
                "ichimoku_golden_cross", "ichimoku_cloud_breakout",
                "stochastic_golden_cross", "ma5_recovery", "new_high_20d",
            }
            if (
                rep_signal_type == "entry"
                and not in_portfolio
                and triggered_ids
                and not any(cid in _transition_ids for cid in triggered_ids)
                and chart is not None
                and getattr(chart, "ma20", None)
                and current_price < chart.ma20 * 0.95
            ):
                logger.info(
                    f"[{name}] entry 연속형 단독 + MA20 하락 과도 "
                    f"({current_price:,} < MA20 {int(chart.ma20):,} × 0.95) — 스킵"
                )
                return None

            from data.db import get_recent_trades_for_stock
            recent_trades = get_recent_trades_for_stock(code, days=3)

            # 보유종목은 positions에서 목표가/손절가, 미보유는 0 (매수 전이므로 미설정)
            if in_portfolio and position_data:
                sig_target = position_data.get("target_price", 0)
                sig_stop = position_data.get("stop_loss_price", 0)
            else:
                sig_target = 0
                sig_stop = 0

            return Signal(
                stock_code=code,
                stock_name=name,
                current_price=current_price,
                triggered_conditions=triggered_msgs,
                triggered_ids=triggered_ids,
                rsi=rsi,
                volume_ratio=volume_ratio,
                chart=chart,
                sector_code=sector_code,
                in_portfolio=in_portfolio,
                signal_type=rep_signal_type,
                target_price=sig_target,
                stop_loss_price=sig_stop,
                strategy_note=cond.get("strategy_note") or "",
                horizon=horizon,
                recent_trades=recent_trades,
                avg_price=avg_price,
                add_signal_mode=add_signal_mode,
            )

        logger.info(
            f"[{name}] {current_price:,}원 | RSI: {rsi}"
            + (f" | RSI5분: {rsi_intraday}" if rsi_intraday is not None else "")
            + (f" | MA5: {int(chart.ma5):,}" if chart and chart.ma5 else "")
            + " | 조건 없음"
        )
        return None

    except Exception as e:
        logger.error(f"[{name}] 조건 체크 중 오류: {e}", exc_info=True)
        return None
