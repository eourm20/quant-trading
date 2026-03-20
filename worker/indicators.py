"""
기술적 지표 계산
— RSI, MA, MACD, 볼린저, 스토캐스틱, CCI, 일목균형표, OBV,
   캔들 패턴, RSI 다이버전스, 피보나치, 지지/저항
"""

import math
from dataclasses import dataclass, field


@dataclass
class ChartSummary:
    # ── 이동평균선 ──
    ma5: float | None
    ma20: float | None
    trend: str                       # "상승" / "하락" / "횡보"
    above_ma5: bool | None
    above_ma20: bool | None
    price_change_5d: float | None    # 5일 전 대비 등락률(%)
    recent_10d_prices: list[int]     # 최근 10일 종가 (오래된 순)
    golden_cross: bool = False       # MA5가 MA20 상향 돌파
    death_cross: bool = False        # MA5가 MA20 하향 돌파
    new_high_20d: bool = False       # 20일 신고가 돌파
    broke_below_ma20: bool = False   # MA20 하향 이탈
    broke_below_ma5: bool = False    # MA5 하향 이탈
    broke_above_ma5: bool = False    # MA5 상향 돌파 (회복)
    # ── MACD ──
    macd_line: float | None = None
    macd_signal: float | None = None
    macd_golden_cross: bool = False
    macd_death_cross: bool = False
    # ── 볼린저 밴드 ──
    bollinger_upper: float | None = None
    bollinger_lower: float | None = None
    bollinger_above_upper: bool = False
    bollinger_below_lower: bool = False
    bollinger_critical_below: bool = False
    # ── 스토캐스틱 ──
    stochastic_k: float | None = None
    stochastic_d: float | None = None
    stochastic_golden_cross: bool = False  # %K가 %D 상향 돌파
    stochastic_death_cross: bool = False   # %K가 %D 하향 돌파
    # ── CCI ──
    cci: float | None = None
    # ── 일목균형표 ──
    ichimoku_tenkan: float | None = None   # 전환선 (9일)
    ichimoku_kijun: float | None = None    # 기준선 (26일)
    ichimoku_senkou_a: float | None = None # 선행스팬A
    ichimoku_senkou_b: float | None = None # 선행스팬B (52일)
    ichimoku_above_cloud: bool = False   # 구름대 위
    ichimoku_below_cloud: bool = False   # 구름대 아래
    ichimoku_tenkan_cross: str | None = None  # "golden" / "dead" / None (내부용)
    ichimoku_tenkan_golden: bool = False  # 전환선 골든크로스
    ichimoku_tenkan_dead: bool = False    # 전환선 데드크로스
    ichimoku_cloud_thickness: float | None = None  # 구름대 두께 (% 기준)
    # ── 볼륨 스프레드 심화 ──
    volume_spread: str | None = None  # 몸통×거래량 조합 해석
    # ── OBV ──
    obv_trend: str | None = None    # "상승 (매수세 우위)" / "하락 (매도세 우위)" / "횡보"
    # ── 캔들 패턴 ──
    candle_patterns: list[str] = field(default_factory=list)
    # ── 다이버전스 ──
    rsi_divergence: str | None = None
    macd_divergence: str | None = None
    # ── 거래량 추세 (다일간) ──
    volume_price_trend: str | None = None  # "추세 강화" / "추세 약화" / None
    # ── 피보나치 되돌림/확장 ──
    fibonacci: dict | None = None   # swing_high, swing_low, fib_236..786, ext_1272, ext_1618
    # ── 지지/저항 ──
    support_level: int | None = None
    resistance_level: int | None = None
    # ── 차트 패턴 (삼각형, 이중바닥 등) ──
    chart_patterns: list[str] = field(default_factory=list)


# ─────────────────────────── 기본 함수 ───────────────────────────

def calculate_rsi(prices: list[float], period: int = 14) -> float | None:
    """Wilder's Smoothed RSI. prices는 최신순."""
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
    """EMA 계산. prices는 오래된 순. 반환: EMA 값 리스트(오래된 순)."""
    if len(prices) < period:
        return []
    k = 2 / (period + 1)
    emas = [sum(prices[:period]) / period]
    for p in prices[period:]:
        emas.append(p * k + emas[-1] * (1 - k))
    return emas


# ─────────────────────────── MACD ───────────────────────────

def calculate_macd(
    prices: list[float],
    fast: int = 12, slow: int = 26, signal_period: int = 9,
) -> tuple[float | None, float | None]:
    if len(prices) < slow + signal_period:
        return None, None
    chron = list(reversed(prices))
    ema_fast = _ema(chron, fast)
    ema_slow = _ema(chron, slow)
    offset = slow - fast
    macd_series = [ema_fast[offset + i] - ema_slow[i] for i in range(len(ema_slow))]
    signal_series = _ema(macd_series, signal_period)
    if not signal_series:
        return None, None
    return round(macd_series[-1], 2), round(signal_series[-1], 2)


# ─────────────────────────── 볼린저 밴드 ───────────────────────────

def calculate_bollinger(
    prices: list[float], period: int = 20, std_dev: float = 2.0,
) -> tuple[float | None, float | None]:
    if len(prices) < period:
        return None, None
    recent = prices[:period]
    mean = sum(recent) / period
    variance = sum((p - mean) ** 2 for p in recent) / period
    std = math.sqrt(variance)
    return round(mean + std_dev * std, 0), round(mean - std_dev * std, 0)


# ─────────────────────────── 스토캐스틱 ───────────────────────────

def calculate_stochastic(
    high_prices: list[int],
    low_prices: list[int],
    close_prices: list[int],
    k_period: int = 14,
    d_period: int = 3,
) -> tuple[float | None, float | None]:
    """%K, %D 반환. 모두 최신순 리스트."""
    if len(high_prices) < k_period or len(low_prices) < k_period or len(close_prices) < k_period:
        return None, None

    k_values = []
    needed = d_period + 1
    for i in range(min(needed, len(close_prices) - k_period + 1)):
        highs = high_prices[i:i + k_period]
        lows = low_prices[i:i + k_period]
        highest = max(highs)
        lowest = min(lows)
        if highest == lowest:
            k_values.append(50.0)
        else:
            k_values.append((close_prices[i] - lowest) / (highest - lowest) * 100)

    if not k_values:
        return None, None

    k = round(k_values[0], 2)
    d = round(sum(k_values[:d_period]) / min(d_period, len(k_values)), 2) if len(k_values) >= d_period else None
    return k, d


# ─────────────────────────── CCI ───────────────────────────

def calculate_cci(
    high_prices: list[int],
    low_prices: list[int],
    close_prices: list[int],
    period: int = 20,
) -> float | None:
    """CCI = (TP - SMA_TP) / (0.015 × MD). 모두 최신순."""
    if len(high_prices) < period or len(low_prices) < period or len(close_prices) < period:
        return None
    tp = [(high_prices[i] + low_prices[i] + close_prices[i]) / 3 for i in range(period)]
    tp_mean = sum(tp) / period
    md = sum(abs(t - tp_mean) for t in tp) / period
    if md == 0:
        return 0.0
    return round((tp[0] - tp_mean) / (0.015 * md), 2)


# ─────────────────────────── 일목균형표 ───────────────────────────

def calculate_ichimoku(
    high_prices: list[int],
    low_prices: list[int],
    current_price: int,
) -> dict:
    """전환선(9), 기준선(26), 선행스팬A/B, 구름대 위치. 모두 최신순."""
    result = {
        "tenkan": None, "kijun": None,
        "senkou_a": None, "senkou_b": None,
        "above_cloud": None, "tenkan_cross": None,
    }

    if len(high_prices) >= 9 and len(low_prices) >= 9:
        result["tenkan"] = round((max(high_prices[:9]) + min(low_prices[:9])) / 2)
    if len(high_prices) >= 26 and len(low_prices) >= 26:
        result["kijun"] = round((max(high_prices[:26]) + min(low_prices[:26])) / 2)

    if result["tenkan"] is not None and result["kijun"] is not None:
        result["senkou_a"] = round((result["tenkan"] + result["kijun"]) / 2)

        # 전환선/기준선 교차
        if len(high_prices) >= 27 and len(low_prices) >= 27:
            prev_tenkan = round((max(high_prices[1:10]) + min(low_prices[1:10])) / 2)
            prev_kijun = round((max(high_prices[1:27]) + min(low_prices[1:27])) / 2)
            if prev_tenkan <= prev_kijun and result["tenkan"] > result["kijun"]:
                result["tenkan_cross"] = "golden"
            elif prev_tenkan >= prev_kijun and result["tenkan"] < result["kijun"]:
                result["tenkan_cross"] = "dead"

    if len(high_prices) >= 52 and len(low_prices) >= 52:
        result["senkou_b"] = round((max(high_prices[:52]) + min(low_prices[:52])) / 2)

    # 구름대 위/아래 판단 + 두께
    if result["senkou_a"] is not None and result["senkou_b"] is not None:
        cloud_top = max(result["senkou_a"], result["senkou_b"])
        cloud_bottom = min(result["senkou_a"], result["senkou_b"])
        if current_price > cloud_top:
            result["above_cloud"] = True
        elif current_price < cloud_bottom:
            result["above_cloud"] = False
        # 구름대 두께 (현재가 대비 %)
        if current_price > 0:
            result["cloud_thickness"] = round((cloud_top - cloud_bottom) / current_price * 100, 2)

    return result


# ─────────────────────────── OBV ───────────────────────────

def calculate_obv_trend(
    close_prices: list[int],
    volumes: list[int],
    period: int = 10,
) -> str | None:
    """OBV 추세. 최신순 리스트."""
    n = min(period, len(close_prices) - 1, len(volumes) - 1)
    if n < 3:
        return None

    # 오래된 순으로 OBV 누적
    obv = [0]
    for i in range(n - 1, -1, -1):
        if close_prices[i] < close_prices[i + 1]:
            obv.append(obv[-1] + volumes[i])
        elif close_prices[i] > close_prices[i + 1]:
            obv.append(obv[-1] - volumes[i])
        else:
            obv.append(obv[-1])

    if len(obv) >= 4:
        mid = len(obv) // 2
        first_avg = sum(obv[:mid]) / mid
        second_avg = sum(obv[mid:]) / (len(obv) - mid)
        if second_avg > first_avg * 1.05:
            return "상승 (매수세 우위)"
        elif second_avg < first_avg * 0.95:
            return "하락 (매도세 우위)"
    return "횡보"


# ─────────────────────────── 볼륨 스프레드 심화 ───────────────────────────

def analyze_volume_spread(
    open_prices: list[int],
    high_prices: list[int],
    low_prices: list[int],
    close_prices: list[int],
    volumes: list[int],
    vol_period: int = 20,
) -> str | None:
    """몸통 크기 × 거래량 조합 분석 (최신 캔들 기준). 모두 최신순."""
    if (len(open_prices) < 1 or len(high_prices) < 1 or len(low_prices) < 1
            or len(close_prices) < 1 or len(volumes) < vol_period + 1):
        return None

    o, h, l, c = open_prices[0], high_prices[0], low_prices[0], close_prices[0]
    body = abs(c - o)
    total_range = h - l if h > l else 1

    # 몸통 비율: 전체 범위 대비
    body_ratio = body / total_range if total_range > 0 else 0
    is_big_body = body_ratio > 0.6
    is_small_body = body_ratio < 0.3

    # 거래량 비율: 20일 평균 대비
    avg_vol = sum(volumes[1:vol_period + 1]) / vol_period if vol_period > 0 else 0
    if avg_vol == 0:
        return None
    vol_ratio = volumes[0] / avg_vol
    is_high_vol = vol_ratio >= 1.5
    is_low_vol = vol_ratio < 0.7

    is_bullish = c > o  # 양봉

    # 4가지 조합
    if is_big_body and is_high_vol:
        direction = "양봉 → 강한 매수세, 추가 상승 가능" if is_bullish else "음봉 → 강한 매도세, 추가 하락 가능"
        return f"큰 몸통+높은 거래량({vol_ratio:.1f}배) {direction}"

    if is_small_body and is_high_vol:
        return f"작은 몸통+높은 거래량({vol_ratio:.1f}배) → 매수/매도 갈등, 추세 전환 가능"

    if is_big_body and is_low_vol:
        return f"큰 몸통+낮은 거래량({vol_ratio:.1f}배) → 속임수 가능, 추세 신뢰도 낮음"

    if is_small_body and is_low_vol:
        return f"작은 몸통+낮은 거래량({vol_ratio:.1f}배) → 관망세, 방향 미정"

    return None


# ─────────────────────────── 캔들 패턴 ───────────────────────────

def detect_candle_patterns(
    open_prices: list[int],
    high_prices: list[int],
    low_prices: list[int],
    close_prices: list[int],
) -> list[str]:
    """최근 캔들에서 주요 패턴 감지. 모두 최신순."""
    patterns = []
    if len(open_prices) < 3 or len(high_prices) < 3:
        return patterns

    o, h, l, c = open_prices[0], high_prices[0], low_prices[0], close_prices[0]
    body = abs(c - o)
    total_range = h - l if h > l else 1
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l

    # 도지 (Doji)
    if body < total_range * 0.1 and total_range > 0:
        patterns.append("도지 (매수/매도 균형, 전환 가능)")

    # 망치형 (Hammer) — 아랫꼬리 길고 윗꼬리 짧음, 양봉
    if body > 0 and lower_wick > body * 2 and upper_wick < body * 0.5 and c > o:
        if close_prices[1] < open_prices[1]:
            patterns.append("망치형 (하락 후 반등 신호)")

    # 역망치형 (Inverted Hammer) — 윗꼬리 길고 아랫꼬리 짧음, 음봉
    if body > 0 and upper_wick > body * 2 and lower_wick < body * 0.5 and c < o:
        if close_prices[1] > open_prices[1]:
            patterns.append("역망치형 (상승 후 조정 신호)")

    # 불리시 엔걸핑 (Bullish Engulfing)
    prev_o, prev_c = open_prices[1], close_prices[1]
    if prev_c < prev_o and c > o:
        if c > prev_o and o < prev_c:
            patterns.append("불리시 엔걸핑 (강한 상승 전환)")

    # 베어리시 엔걸핑 (Bearish Engulfing)
    if prev_c > prev_o and c < o:
        if o > prev_c and c < prev_o:
            patterns.append("베어리시 엔걸핑 (강한 하락 전환)")

    # 하락 삼법 (Falling Three Methods) — 대음봉 + 작은 양봉 3개 + 대음봉
    if len(open_prices) >= 5:
        first_bearish = close_prices[4] < open_prices[4] and abs(close_prices[4] - open_prices[4]) > total_range * 0.5
        last_bearish = c < o and body > total_range * 0.5
        middle_small = all(
            abs(close_prices[i] - open_prices[i]) < abs(close_prices[4] - open_prices[4]) * 0.5
            for i in range(1, 4)
        )
        if first_bearish and last_bearish and middle_small:
            patterns.append("하락삼법 (하락 추세 지속)")

    return patterns


# ─────────────────────────── RSI 다이버전스 ───────────────────────────

def detect_rsi_divergence(
    close_prices: list[int],
    period: int = 14,
    lookback: int = 10,
) -> str | None:
    """가격 vs RSI 방향 괴리 감지. close_prices 최신순."""
    if len(close_prices) < period + lookback:
        return None

    rsi_now = calculate_rsi(close_prices)
    rsi_prev = calculate_rsi(close_prices[lookback:])

    if rsi_now is None or rsi_prev is None:
        return None

    price_now = close_prices[0]
    price_prev = close_prices[lookback]

    # 강세 다이버전스: 가격 하락 + RSI 상승
    if price_now < price_prev and rsi_now > rsi_prev + 2:
        return f"강세 (가격↓{price_prev:,}→{price_now:,} / RSI↑{rsi_prev:.0f}→{rsi_now:.0f})"

    # 약세 다이버전스: 가격 상승 + RSI 하락
    if price_now > price_prev and rsi_now < rsi_prev - 2:
        return f"약세 (가격↑{price_prev:,}→{price_now:,} / RSI↓{rsi_prev:.0f}→{rsi_now:.0f})"

    return None


# ─────────────────────────── MACD 다이버전스 ───────────────────────────

def detect_macd_divergence(
    close_prices: list[int],
    lookback: int = 10,
) -> str | None:
    """MACD 라인 vs 가격 방향 괴리 감지. close_prices 최신순."""
    if len(close_prices) < 35 + lookback:
        return None

    macd_now, _ = calculate_macd(close_prices)
    macd_prev, _ = calculate_macd(close_prices[lookback:])

    if macd_now is None or macd_prev is None:
        return None

    price_now = close_prices[0]
    price_prev = close_prices[lookback]

    if price_now < price_prev and macd_now > macd_prev + 0.5:
        return f"강세 (가격↓ MACD↑)"

    if price_now > price_prev and macd_now < macd_prev - 0.5:
        return f"약세 (가격↑ MACD↓)"

    return None


# ─────────────────────────── 거래량 추세 (다일간) ───────────────────────────

def analyze_volume_price_trend(
    close_prices: list[int],
    volumes: list[int],
    lookback: int = 5,
) -> str | None:
    """최근 N일 가격 방향 + 거래량 방향 조합으로 추세 강도 평가. 최신순."""
    if len(close_prices) < lookback + 1 or len(volumes) < lookback + 1:
        return None

    # 가격 방향 (최근 vs lookback일 전)
    price_rising = close_prices[0] > close_prices[lookback]

    # 거래량 추세: 최근 절반 평균 vs 이전 절반 평균
    mid = lookback // 2
    recent_vol = sum(volumes[:mid]) / max(mid, 1)
    older_vol = sum(volumes[mid:lookback]) / max(lookback - mid, 1)
    vol_rising = recent_vol > older_vol * 1.1

    if price_rising and vol_rising:
        return "추세 강화 (가격↑ + 거래량↑ → 매수세 지속)"
    elif price_rising and not vol_rising:
        return "추세 약화 (가격↑ + 거래량↓ → 매수세 약화 주의)"
    elif not price_rising and vol_rising:
        return "추세 강화 (가격↓ + 거래량↑ → 매도세 강화)"
    elif not price_rising and not vol_rising:
        return "추세 약화 (가격↓ + 거래량↓ → 매도압력 약화, 반등 가능)"

    return None


# ─────────────────────────── 피보나치 되돌림/확장 ───────────────────────────

def calculate_fibonacci_levels(
    high_prices: list[int],
    low_prices: list[int],
    lookback: int = 20,
) -> dict | None:
    """최근 N일 스윙 고점/저점 기반 피보나치 레벨. 최신순."""
    n = min(lookback, len(high_prices), len(low_prices))
    if n < 5:
        return None

    swing_high = max(high_prices[:n])
    swing_low = min(low_prices[:n])
    diff = swing_high - swing_low

    if diff <= 0:
        return None

    return {
        "swing_high": swing_high,
        "swing_low": swing_low,
        # 되돌림 레벨 (고점에서 하락)
        "fib_236": round(swing_high - diff * 0.236),
        "fib_382": round(swing_high - diff * 0.382),
        "fib_500": round(swing_high - diff * 0.500),
        "fib_618": round(swing_high - diff * 0.618),
        "fib_786": round(swing_high - diff * 0.786),
        # 확장 레벨 (목표가 설정용)
        "ext_1272": round(swing_low + diff * 1.272),
        "ext_1618": round(swing_low + diff * 1.618),
        "ext_2000": round(swing_low + diff * 2.000),
    }


# ─────────────────────────── 지지/저항 ───────────────────────────

def find_support_resistance(
    high_prices: list[int],
    low_prices: list[int],
    lookback: int = 20,
) -> tuple[int | None, int | None]:
    """최근 N일 고가 최대값(저항), 저가 최소값(지지). 최신순."""
    n = min(lookback, len(high_prices), len(low_prices))
    if n < 5:
        return None, None
    resistance = max(high_prices[:n])
    support = min(low_prices[:n])
    return support, resistance


# ─────────────────────────── 차트 패턴 감지 ───────────────────────────

def _find_swing_points(
    prices: list[int],  # 오래된 순
    window: int = 3,
) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """스윙 고점/저점 찾기. 반환: (highs, lows) as (index, price) 리스트."""
    highs, lows = [], []
    for i in range(window, len(prices) - window):
        if all(prices[i] >= prices[i - j] for j in range(1, window + 1)) and \
           all(prices[i] >= prices[i + j] for j in range(1, window + 1)):
            highs.append((i, prices[i]))
        if all(prices[i] <= prices[i - j] for j in range(1, window + 1)) and \
           all(prices[i] <= prices[i + j] for j in range(1, window + 1)):
            lows.append((i, prices[i]))
    return highs, lows


def detect_chart_patterns(
    close_prices: list[int],  # 최신순
    high_prices: list[int],
    low_prices: list[int],
) -> list[str]:
    """차트 패턴 감지 (이중바닥/천장, 헤드앤숄더, 삼각형, 박스권 등). 최신순."""
    if len(close_prices) < 20 or len(high_prices) < 20 or len(low_prices) < 20:
        return []

    # 오래된 순 변환
    n = min(len(close_prices), len(high_prices), len(low_prices))
    closes = list(reversed(close_prices[:n]))
    h_prices = list(reversed(high_prices[:n]))
    l_prices = list(reversed(low_prices[:n]))

    patterns = []
    swing_highs, swing_lows = _find_swing_points(closes)

    # ── 이중 바닥 (Double Bottom) ──
    if len(swing_lows) >= 2:
        l1, l2 = swing_lows[-2], swing_lows[-1]
        if max(l1[1], l2[1]) > 0:
            diff_pct = abs(l1[1] - l2[1]) / max(l1[1], l2[1]) * 100
            if diff_pct < 3:
                mid_highs = [h for h in swing_highs if l1[0] < h[0] < l2[0]]
                if mid_highs:
                    neckline = max(h[1] for h in mid_highs)
                    if closes[-1] > neckline:
                        patterns.append(f"이중 바닥 돌파 (넥라인 {neckline:,} 돌파 → 상승 전환)")
                    else:
                        patterns.append(f"이중 바닥 형성 중 (넥라인 {neckline:,} 돌파 대기)")

    # ── 이중 천장 (Double Top) ──
    if len(swing_highs) >= 2:
        h1, h2 = swing_highs[-2], swing_highs[-1]
        if max(h1[1], h2[1]) > 0:
            diff_pct = abs(h1[1] - h2[1]) / max(h1[1], h2[1]) * 100
            if diff_pct < 3:
                mid_lows = [l for l in swing_lows if h1[0] < l[0] < h2[0]]
                if mid_lows:
                    neckline = min(l[1] for l in mid_lows)
                    if closes[-1] < neckline:
                        patterns.append(f"이중 천장 이탈 (넥라인 {neckline:,} 이탈 → 하락 신호)")
                    else:
                        patterns.append(f"이중 천장 형성 중 (넥라인 {neckline:,} 이탈 주의)")

    # ── 헤드 앤 숄더 ──
    if len(swing_highs) >= 3:
        h1, h2, h3 = swing_highs[-3], swing_highs[-2], swing_highs[-1]
        if h2[1] > h1[1] and h2[1] > h3[1] and max(h1[1], h3[1]) > 0:
            shoulder_diff = abs(h1[1] - h3[1]) / max(h1[1], h3[1]) * 100
            if shoulder_diff < 5:
                # 넥라인: 좌우 숄더 사이 저점
                neck_lows = [l for l in swing_lows if h1[0] < l[0] < h3[0]]
                neck = min(l[1] for l in neck_lows) if neck_lows else None
                if neck:
                    if closes[-1] < neck:
                        patterns.append(f"헤드 앤 숄더 이탈 (넥라인 {neck:,} → 하락 전환)")
                    else:
                        patterns.append(f"헤드 앤 숄더 형성 중 (넥라인 {neck:,} 이탈 주의)")

    # ── 역 헤드 앤 숄더 ──
    if len(swing_lows) >= 3:
        l1, l2, l3 = swing_lows[-3], swing_lows[-2], swing_lows[-1]
        if l2[1] < l1[1] and l2[1] < l3[1] and max(l1[1], l3[1]) > 0:
            shoulder_diff = abs(l1[1] - l3[1]) / max(l1[1], l3[1]) * 100
            if shoulder_diff < 5:
                neck_highs = [h for h in swing_highs if l1[0] < h[0] < l3[0]]
                neck = max(h[1] for h in neck_highs) if neck_highs else None
                if neck:
                    if closes[-1] > neck:
                        patterns.append(f"역 헤드 앤 숄더 돌파 (넥라인 {neck:,} → 상승 전환)")
                    else:
                        patterns.append(f"역 헤드 앤 숄더 형성 중 (넥라인 {neck:,} 돌파 대기)")

    # ── 삼각형 패턴 ──
    if len(swing_highs) >= 2 and len(swing_lows) >= 2:
        rh = swing_highs[-2:]
        rl = swing_lows[-2:]
        high_slope = rh[-1][1] - rh[-2][1]
        low_slope = rl[-1][1] - rl[-2][1]

        if high_slope < 0 and low_slope > 0:
            patterns.append("대칭 삼각형 (수렴 중, 돌파 방향 주시)")
        elif rh[-1][1] > 0 and abs(high_slope) < rh[-1][1] * 0.02 and low_slope > 0:
            patterns.append("상승 삼각형 (저항 돌파 시 강한 상승)")
        elif rl[-1][1] > 0 and abs(low_slope) < rl[-1][1] * 0.02 and high_slope < 0:
            patterns.append("하락 삼각형 (지지 이탈 시 강한 하락)")

    # ── 깃발형 (Flag) — 강한 추세 후 작은 역방향 조정 ──
    if len(closes) >= 15:
        # 직전 5일 강한 상승/하락 + 이후 5일 작은 조정
        impulse = closes[-10] - closes[-15] if len(closes) >= 15 else 0
        consolidation = closes[-1] - closes[-5] if len(closes) >= 5 else 0
        if closes[-15] > 0 and abs(impulse) / closes[-15] > 0.05:  # 5% 이상 impulse
            if abs(consolidation) < abs(impulse) * 0.3:  # 조정이 impulse의 30% 미만
                direction = "상승" if impulse > 0 else "하락"
                patterns.append(f"깃발형 ({direction} 후 조정 → 추세 연장 가능)")

    # ── 쐐기형 (Wedge) — 고점·저점 모두 같은 방향으로 수렴 ──
    if len(swing_highs) >= 2 and len(swing_lows) >= 2:
        rh = swing_highs[-2:]
        rl = swing_lows[-2:]
        high_slope = rh[-1][1] - rh[-2][1]
        low_slope = rl[-1][1] - rl[-2][1]
        # 상승 쐐기: 고점·저점 모두 상승하지만 수렴
        if high_slope > 0 and low_slope > 0 and high_slope < low_slope:
            patterns.append("상승 쐐기 (상승 속 수렴 → 하락 돌파 가능)")
        # 하락 쐐기: 고점·저점 모두 하락하지만 수렴
        elif high_slope < 0 and low_slope < 0 and abs(high_slope) > abs(low_slope):
            patterns.append("하락 쐐기 (하락 속 수렴 → 상승 돌파 가능)")

    # ── 컵 위드 핸들 — U자형 회복 + 작은 풀백 ──
    if len(closes) >= 30:
        # 30일 중 중간 저점 찾기
        mid_idx = len(closes) // 2
        left_half = closes[:mid_idx]
        right_half = closes[mid_idx:]
        if left_half and right_half:
            cup_low_idx = left_half.index(min(left_half))
            cup_low = min(left_half)
            cup_left_high = closes[0]  # 최근(오른쪽 끝)
            cup_right_high = closes[-1] if len(closes) > mid_idx else 0  # 왼쪽 끝(오래된 순이므로)
            # 실제로 closes는 오래된 순이므로: closes[0]=가장 오래됨, closes[-1]=최근
            rim_left = closes[0]
            rim_right = closes[-1]
            if cup_low > 0 and rim_left > 0:
                dip_pct = (rim_left - cup_low) / rim_left * 100
                recovery = rim_right >= rim_left * 0.95  # 왼쪽 림의 95% 이상 회복
                if 10 < dip_pct < 35 and recovery:
                    # 핸들: 최근 5일 소폭 하락
                    if len(closes) >= 5 and closes[-1] < closes[-5]:
                        handle_dip = (closes[-5] - closes[-1]) / closes[-5] * 100
                        if handle_dip < 10:
                            patterns.append("컵 위드 핸들 (U자형 회복 + 핸들 조정 → 돌파 시 강한 상승)")

    # ── 박스권 (횡보) ──
    if len(closes) >= 20:
        recent = closes[-20:]
        highest, lowest = max(recent), min(recent)
        if lowest > 0:
            range_pct = (highest - lowest) / lowest * 100
            if range_pct < 5:
                patterns.append(f"박스권 ({lowest:,}~{highest:,}, 돌파 방향 주시)")

    return patterns


# ─────────────────────────── 차트 요약 계산 ───────────────────────────

def calculate_chart_summary(
    prices: list[int],          # 종가 (최신순)
    high_prices: list[int],     # 고가 (최신순)
    current_price: int,
    low_prices: list[int] | None = None,   # 저가 (최신순)
    open_prices: list[int] | None = None,  # 시가 (최신순)
    volumes: list[int] | None = None,      # 거래량 (최신순)
) -> ChartSummary:
    """일봉 데이터로 차트 요약 계산."""
    low_prices = low_prices or []
    open_prices = open_prices or []
    volumes = volumes or []

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

    # 최근 10일 종가 오래된 순
    recent_10d = list(reversed(prices[:10])) if len(prices) >= 10 else list(reversed(prices))

    # ── MA 골든/데드크로스 ──
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

    # 20일 신고가
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

    # ── MACD ──
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

    # ── 볼린저 밴드 ──
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
        if current_price < bb_lower * 0.97:
            bollinger_critical_below = True

    # ── 스토캐스틱 ──
    stoch_k, stoch_d = None, None
    stoch_golden = False
    stoch_dead = False
    if low_prices and len(low_prices) >= 14:
        stoch_k, stoch_d = calculate_stochastic(high_prices, low_prices, prices)
        if len(low_prices) >= 15:
            prev_k, prev_d = calculate_stochastic(high_prices[1:], low_prices[1:], prices[1:])
            if stoch_k is not None and stoch_d is not None and prev_k is not None and prev_d is not None:
                if prev_k <= prev_d and stoch_k > stoch_d:
                    stoch_golden = True
                elif prev_k >= prev_d and stoch_k < stoch_d:
                    stoch_dead = True

    # ── CCI ──
    cci_val = None
    if low_prices and len(low_prices) >= 20:
        cci_val = calculate_cci(high_prices, low_prices, prices)

    # ── 일목균형표 ──
    ichimoku = {"tenkan": None, "kijun": None, "senkou_a": None, "senkou_b": None,
                "above_cloud": None, "tenkan_cross": None}
    if low_prices and len(low_prices) >= 9:
        ichimoku = calculate_ichimoku(high_prices, low_prices, current_price)

    # ── 볼륨 스프레드 심화 ──
    vol_spread = None
    if open_prices and low_prices and volumes and len(volumes) >= 21:
        vol_spread = analyze_volume_spread(open_prices, high_prices, low_prices, prices, volumes)

    # ── OBV ──
    obv = None
    if volumes and len(volumes) >= 5:
        obv = calculate_obv_trend(prices, volumes)

    # ── 캔들 패턴 ──
    candle_pat = []
    if open_prices and low_prices and len(open_prices) >= 3:
        candle_pat = detect_candle_patterns(open_prices, high_prices, low_prices, prices)

    # ── RSI 다이버전스 ──
    rsi_div = detect_rsi_divergence(prices) if len(prices) >= 25 else None

    # ── MACD 다이버전스 ──
    macd_div = detect_macd_divergence(prices) if len(prices) >= 45 else None

    # ── 거래량 추세 (다일간) ──
    vol_price_trend = None
    if volumes and len(volumes) >= 6:
        vol_price_trend = analyze_volume_price_trend(prices, volumes)

    # ── 피보나치 ──
    fib = None
    if low_prices and len(low_prices) >= 5:
        fib = calculate_fibonacci_levels(high_prices, low_prices)

    # ── 지지/저항 ──
    support, resistance = None, None
    if low_prices and len(low_prices) >= 5:
        support, resistance = find_support_resistance(high_prices, low_prices)

    # ── 차트 패턴 (이중바닥/천장, 헤드앤숄더, 삼각형, 박스권) ──
    chart_pat = []
    if low_prices and len(low_prices) >= 20:
        chart_pat = detect_chart_patterns(prices, high_prices, low_prices)

    return ChartSummary(
        ma5=ma5, ma20=ma20, trend=trend,
        above_ma5=current_price > ma5 if ma5 else None,
        above_ma20=current_price > ma20 if ma20 else None,
        price_change_5d=price_change_5d,
        recent_10d_prices=recent_10d,
        golden_cross=golden_cross, death_cross=death_cross,
        new_high_20d=new_high_20d,
        broke_below_ma20=broke_below_ma20,
        broke_below_ma5=broke_below_ma5,
        broke_above_ma5=broke_above_ma5,
        macd_line=macd_line, macd_signal=macd_sig,
        macd_golden_cross=macd_golden_cross, macd_death_cross=macd_death_cross,
        bollinger_upper=bb_upper, bollinger_lower=bb_lower,
        bollinger_above_upper=bollinger_above_upper,
        bollinger_below_lower=bollinger_below_lower,
        bollinger_critical_below=bollinger_critical_below,
        stochastic_k=stoch_k, stochastic_d=stoch_d,
        stochastic_golden_cross=stoch_golden,
        stochastic_death_cross=stoch_dead,
        cci=cci_val,
        ichimoku_tenkan=ichimoku["tenkan"],
        ichimoku_kijun=ichimoku["kijun"],
        ichimoku_senkou_a=ichimoku["senkou_a"],
        ichimoku_senkou_b=ichimoku["senkou_b"],
        ichimoku_above_cloud=ichimoku.get("above_cloud") is True,
        ichimoku_below_cloud=ichimoku.get("above_cloud") is False,
        ichimoku_tenkan_cross=ichimoku.get("tenkan_cross"),
        ichimoku_tenkan_golden=ichimoku.get("tenkan_cross") == "golden",
        ichimoku_tenkan_dead=ichimoku.get("tenkan_cross") == "dead",
        ichimoku_cloud_thickness=ichimoku.get("cloud_thickness"),
        volume_spread=vol_spread,
        obv_trend=obv,
        candle_patterns=candle_pat,
        rsi_divergence=rsi_div,
        macd_divergence=macd_div,
        volume_price_trend=vol_price_trend,
        fibonacci=fib,
        support_level=support,
        resistance_level=resistance,
        chart_patterns=chart_pat,
    )
