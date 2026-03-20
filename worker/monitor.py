"""
모니터링 엔진 - 조건 감지 (conditions.yaml 기반 동적 평가)
"""

import logging
from dataclasses import dataclass, field as dc_field
from worker.clients.kiwoom_client import KiwoomClient
from worker.indicators import calculate_rsi, calculate_volume_ratio, calculate_chart_summary, ChartSummary

logger = logging.getLogger(__name__)

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

    except (KeyError, ValueError) as e:
        logger.warning(f"조건 메시지 포맷 오류 [{cond_def.get('id')}]: {e}")

    return None


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
    cond = stock.get("conditions", {})

    holding_codes = {str(h.get("stock_code", "")) for h in (holdings or [])}
    in_portfolio = code in holding_codes

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

        triggered_msgs = []
        triggered_ids = []
        for cond_def in conditions:
            if cond_def.get("signal_type", "both") not in allowed_types:
                continue
            # rsi_lte_intraday 조건은 단기 종목에만 평가
            if cond_def.get("evaluator") == "rsi_lte_intraday" and horizon != "단기":
                continue
            msg = _evaluate_condition(cond_def, cond, current_price, rsi, volume_ratio, chart, rsi_intraday)
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
            rep_signal_type = min(triggered_types, key=lambda t: type_priority.get(t, 9), default="")

            from data.db import get_recent_trades_for_stock
            recent_trades = get_recent_trades_for_stock(code, days=3)

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
                target_price=cond.get("target_price") or 0,
                stop_loss_price=cond.get("stop_loss_price") or 0,
                strategy_note=cond.get("strategy_note") or "",
                horizon=horizon,
                recent_trades=recent_trades,
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
