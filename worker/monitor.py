"""
모니터링 엔진 - 조건 감지 (conditions.yaml 기반 동적 평가)
"""

import logging
from dataclasses import dataclass, field as dc_field
from worker.kiwoom_client import KiwoomClient
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
) -> str | None:
    """조건 평가. 트리거되면 메시지 반환, 아니면 None."""
    evaluator = cond_def.get("evaluator")
    param = cond_def.get("param")
    msg_template = cond_def.get("message", "")

    fmt = {
        "price": current_price,
        "rsi": rsi or 0,
        "ratio": volume_ratio or 0,
        "threshold": 0,
        "ma5": int(chart.ma5) if chart and chart.ma5 else 0,
        "ma20": int(chart.ma20) if chart and chart.ma20 else 0,
        "macd": chart.macd_line or 0 if chart else 0,
        "signal": chart.macd_signal or 0 if chart else 0,
        "upper": int(chart.bollinger_upper) if chart and chart.bollinger_upper else 0,
        "lower": int(chart.bollinger_lower) if chart and chart.bollinger_lower else 0,
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

        elif evaluator == "volume_gte":
            threshold = stock_cond.get(param)
            if volume_ratio is not None and threshold and volume_ratio >= threshold:
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

        # MACD 계산을 위해 40일치 데이터 조회
        daily_data = client.get_daily_ohlcv(code, period=40)
        close_prices, high_prices, volumes = [], [], []
        for d in daily_data:
            cp = _parse_price(d.get("cur_prc"))
            hp = _parse_price(d.get("high_pric"))
            vol = _parse_price(d.get("trde_qty"))
            if cp:
                close_prices.append(cp)
            if hp:
                high_prices.append(hp)
            if vol:
                volumes.append(vol)

        rsi = calculate_rsi(close_prices) if len(close_prices) >= 15 else None
        volume_ratio = calculate_volume_ratio(volumes) if len(volumes) >= 21 else None
        chart = calculate_chart_summary(close_prices, high_prices, current_price) if len(close_prices) >= 5 else None

        triggered_msgs = []
        triggered_ids = []
        for cond_def in conditions:
            if cond_def.get("signal_type", "both") not in allowed_types:
                continue
            msg = _evaluate_condition(cond_def, cond, current_price, rsi, volume_ratio, chart)
            if msg:
                triggered_msgs.append(msg)
                triggered_ids.append(cond_def["id"])

        if triggered_msgs:
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
            )

        logger.info(
            f"[{name}] {current_price:,}원 | RSI: {rsi}"
            + (f" | MA5: {int(chart.ma5):,}" if chart and chart.ma5 else "")
            + " | 조건 없음"
        )
        return None

    except Exception as e:
        logger.error(f"[{name}] 조건 체크 중 오류: {e}", exc_info=True)
        return None
