"""
기술적 지표 계산
"""

import math
from dataclasses import dataclass, field


@dataclass
class ChartSummary:
    ma5: float | None
    ma20: float | None
    trend: str                       # "상승" / "하락" / "횡보"
    above_ma5: bool | None
    above_ma20: bool | None
    price_change_5d: float | None    # 5일 전 대비 등락률(%)
    recent_10d_prices: list[int]     # 최근 10일(2주) 종가 (오래된 순)
    golden_cross: bool = False       # MA5가 MA20 상향 돌파
    death_cross: bool = False        # MA5가 MA20 하향 돌파
    new_high_20d: bool = False       # 20일 신고가 돌파
    broke_below_ma20: bool = False   # MA20 하향 이탈
    broke_below_ma5: bool = False    # MA5 하향 이탈
    broke_above_ma5: bool = False    # MA5 상향 돌파 (회복)
    macd_line: float | None = None   # MACD 라인
    macd_signal: float | None = None # MACD 시그널 라인
    macd_golden_cross: bool = False  # MACD 라인이 시그널 상향 돌파
    macd_death_cross: bool = False   # MACD 라인이 시그널 하향 돌파
    bollinger_upper: float | None = None  # 볼린저 상단
    bollinger_lower: float | None = None  # 볼린저 하단
    bollinger_above_upper: bool = False   # 볼린저 상단 돌파
    bollinger_below_lower: bool = False   # 볼린저 하단 이탈
    bollinger_critical_below: bool = False  # 볼린저 하단 3% 이상 이탈 (심각)


def calculate_rsi(prices: list[float], period: int = 14) -> float | None:
    """RSI 계산. prices는 최신순 정렬된 종가 리스트."""
    if len(prices) < period + 1:
        return None

    prices = list(reversed(prices))
    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]

    gains = [d if d > 0 else 0 for d in deltas[:period]]
    losses = [-d if d < 0 else 0 for d in deltas[:period]]

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    for delta in deltas[period:]:
        gain = delta if delta > 0 else 0
        loss = -delta if delta < 0 else 0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def calculate_volume_ratio(volumes: list[int], period: int = 20) -> float | None:
    """현재 거래량 / 최근 N일 평균 거래량"""
    if len(volumes) < period + 1:
        return None
    avg = sum(volumes[1:period + 1]) / period
    if avg == 0:
        return None
    return round(volumes[0] / avg, 2)


def _ma(prices: list[int], n: int) -> float | None:
    if len(prices) < n:
        return None
    return round(sum(prices[:n]) / n, 0)


def _ema(prices: list[float], period: int) -> list[float]:
    """EMA 계산. prices는 오래된 순(index 0이 가장 오래됨).
    반환값: len(prices) - period + 1 개의 EMA 값 (오래된 순)."""
    if len(prices) < period:
        return []
    k = 2 / (period + 1)
    emas = [sum(prices[:period]) / period]
    for p in prices[period:]:
        emas.append(p * k + emas[-1] * (1 - k))
    return emas


def calculate_macd(
    prices: list[float],  # 최신순
    fast: int = 12,
    slow: int = 26,
    signal_period: int = 9,
) -> tuple[float | None, float | None]:
    """MACD 라인과 시그널 라인의 최신값 반환. 데이터 부족 시 (None, None)."""
    if len(prices) < slow + signal_period:
        return None, None

    chron = list(reversed(prices))  # 오래된 순으로 변환

    ema_fast = _ema(chron, fast)   # len = n - fast + 1
    ema_slow = _ema(chron, slow)   # len = n - slow + 1

    # ema_fast[slow-fast + i]와 ema_slow[i]가 같은 시점
    offset = slow - fast
    macd_series = [
        ema_fast[offset + i] - ema_slow[i]
        for i in range(len(ema_slow))
    ]

    signal_series = _ema(macd_series, signal_period)
    if not signal_series:
        return None, None

    return round(macd_series[-1], 2), round(signal_series[-1], 2)


def calculate_bollinger(
    prices: list[float],  # 최신순
    period: int = 20,
    std_dev: float = 2.0,
) -> tuple[float | None, float | None]:
    """볼린저 밴드 상단/하단 반환. 데이터 부족 시 (None, None)."""
    if len(prices) < period:
        return None, None
    recent = prices[:period]
    mean = sum(recent) / period
    variance = sum((p - mean) ** 2 for p in recent) / period
    std = math.sqrt(variance)
    return round(mean + std_dev * std, 0), round(mean - std_dev * std, 0)


def calculate_chart_summary(
    prices: list[int],      # 종가 (최신순)
    high_prices: list[int], # 고가 (최신순)
    current_price: int,
) -> ChartSummary:
    """일봉 데이터로 차트 요약 계산."""
    ma5 = _ma(prices, 5)
    ma20 = _ma(prices, 20)

    # 추세: MA5 vs MA20
    if ma5 and ma20:
        if ma5 > ma20 * 1.005:
            trend = "상승"
        elif ma5 < ma20 * 0.995:
            trend = "하락"
        else:
            trend = "횡보"
    else:
        trend = "횡보"

    # 5일 전 대비 등락률
    price_change_5d = None
    if len(prices) >= 6 and prices[5] > 0:
        price_change_5d = round((current_price - prices[5]) / prices[5] * 100, 2)

    # 최근 10일(2주) 종가 오래된 순
    recent_10d = list(reversed(prices[:10])) if len(prices) >= 10 else list(reversed(prices))

    # MA 골든/데드크로스
    golden_cross = False
    death_cross = False
    if len(prices) >= 21:
        prev_ma5 = _ma(prices[1:], 5)
        prev_ma20 = _ma(prices[1:], 20)
        if ma5 and ma20 and prev_ma5 and prev_ma20:
            if prev_ma5 <= prev_ma20 and ma5 > ma20:
                golden_cross = True
            elif prev_ma5 >= prev_ma20 and ma5 < ma20:
                death_cross = True

    # 20일 신고가 돌파
    new_high_20d = False
    if high_prices and len(high_prices) >= 20:
        if current_price > max(high_prices[:20]):
            new_high_20d = True

    # MA20 하향 이탈
    broke_below_ma20 = False
    if ma20 and len(prices) >= 2:
        if prices[1] >= ma20 and current_price < ma20:
            broke_below_ma20 = True

    # MA5 하향 이탈 / 상향 돌파
    broke_below_ma5 = False
    broke_above_ma5 = False
    if ma5 and len(prices) >= 2:
        if prices[1] >= ma5 and current_price < ma5:
            broke_below_ma5 = True
        elif prices[1] <= ma5 and current_price > ma5:
            broke_above_ma5 = True

    # MACD
    macd_line, macd_sig = calculate_macd(prices)
    macd_golden_cross = False
    macd_death_cross = False
    if len(prices) >= 36:
        prev_macd, prev_sig = calculate_macd(prices[1:])
        if (macd_line is not None and macd_sig is not None
                and prev_macd is not None and prev_sig is not None):
            if prev_macd <= prev_sig and macd_line > macd_sig:
                macd_golden_cross = True
            elif prev_macd >= prev_sig and macd_line < macd_sig:
                macd_death_cross = True

    # 볼린저 밴드
    bb_upper, bb_lower = calculate_bollinger(prices)
    bollinger_above_upper = False
    bollinger_below_lower = False
    bollinger_critical_below = False
    if bb_upper is not None and bb_lower is not None and len(prices) >= 2:
        prev_bb_upper, prev_bb_lower = calculate_bollinger(prices[1:])
        if prev_bb_upper is not None and prices[1] <= prev_bb_upper and current_price > bb_upper:
            bollinger_above_upper = True
        if prev_bb_lower is not None and prices[1] >= prev_bb_lower and current_price < bb_lower:
            bollinger_below_lower = True
        # 볼린저 하단보다 3% 이상 하락 (심각 과매도)
        if current_price < bb_lower * 0.97:
            bollinger_critical_below = True

    return ChartSummary(
        ma5=ma5,
        ma20=ma20,
        trend=trend,
        above_ma5=current_price > ma5 if ma5 else None,
        above_ma20=current_price > ma20 if ma20 else None,
        price_change_5d=price_change_5d,
        recent_10d_prices=recent_10d,
        golden_cross=golden_cross,
        death_cross=death_cross,
        new_high_20d=new_high_20d,
        broke_below_ma20=broke_below_ma20,
        broke_below_ma5=broke_below_ma5,
        broke_above_ma5=broke_above_ma5,
        macd_line=macd_line,
        macd_signal=macd_sig,
        macd_golden_cross=macd_golden_cross,
        macd_death_cross=macd_death_cross,
        bollinger_upper=bb_upper,
        bollinger_lower=bb_lower,
        bollinger_above_upper=bollinger_above_upper,
        bollinger_below_lower=bollinger_below_lower,
        bollinger_critical_below=bollinger_critical_below,
    )
