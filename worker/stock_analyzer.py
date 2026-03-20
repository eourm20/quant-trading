"""
자동 종목 스크리닝 (워커 전용)
- 장 마감 후(15:40) 유망 종목 자동 발굴 → AI 분석 → watchlist 자동 추가
- 수동 종목 분석은 Claude Desktop에서 MCP 도구로 직접 수행
"""

import json
import logging
import os
import re
import time

from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

logger = logging.getLogger(__name__)

_ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
_OPENAI_KEY = os.getenv("OPENAI_API_KEY", "").strip()

if _ANTHROPIC_KEY:
    from anthropic import Anthropic
    _ai_client = Anthropic(api_key=_ANTHROPIC_KEY)
    _AI_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
    _AI_BACKEND = "anthropic"
elif _OPENAI_KEY:
    from openai import OpenAI
    _ai_client = OpenAI(api_key=_OPENAI_KEY)
    _AI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    _AI_BACKEND = "openai"
else:
    _ai_client = None
    _AI_MODEL = ""
    _AI_BACKEND = ""


def _fmt_int(value, default: int = 0) -> int:
    """None/문자열/숫자를 안전하게 정수로 변환."""
    try:
        if value is None or value == "":
            return default
        return int(float(str(value).replace(",", "").strip()))
    except Exception:
        return default


def _build_market_text(kiwoom) -> str:
    try:
        kospi = kiwoom.get_market_index("kospi")
        kosdaq = kiwoom.get_market_index("kosdaq")
        kospi_rate = kospi.get("flu_rt") or kospi.get("prdy_ctrt") or "N/A"
        kosdaq_rate = kosdaq.get("flu_rt") or kosdaq.get("prdy_ctrt") or "N/A"
        return f"코스피 {kospi_rate}% / 코스닥 {kosdaq_rate}%"
    except Exception:
        return "시장 지수 조회 실패"


def _extract_json_block(text: str) -> str | None:
    """응답 텍스트에서 recommendation 키가 포함된 첫 JSON 객체 블록 추출."""
    m = re.search(r'\{.*"recommendation".*\}', text, re.DOTALL)
    if not m:
        return None
    chunk = text[m.start():]
    depth = 0
    for i, ch in enumerate(chunk):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        if depth == 0:
            return chunk[: i + 1]
    return m.group()


def _repair_screening_json(raw_text: str) -> str:
    """파싱 실패 시 LLM에게 JSON 정규화 재요청."""
    repair_prompt = (
        "아래 텍스트를 JSON 객체 1개로만 정규화해서 반환해.\n"
        "설명/코드블록/주석 없이 순수 JSON만 출력.\n"
        "키 이름은 유지하고, 잘못된 따옴표/쉼표/중괄호를 고쳐.\n\n"
        f"{raw_text}"
    )
    if _AI_BACKEND == "anthropic":
        response = _ai_client.messages.create(
            model=_AI_MODEL,
            max_tokens=1200,
            messages=[{"role": "user", "content": repair_prompt}],
        )
        return response.content[0].text
    response = _ai_client.chat.completions.create(
        model=_AI_MODEL,
        max_tokens=1200,
        messages=[{"role": "user", "content": repair_prompt}],
    )
    return response.choices[0].message.content or ""


# ═══════════════════════════ 후보 수집 ═══════════════════════════

def _screen_candidates() -> list[dict]:
    """키움 API로 유망 종목 후보 수집. 기존 watchlist 종목은 제외.

    Returns:
        [{stock_code, stock_name, source}, ...]  최대 30개
    """
    from worker.clients.kiwoom_client import KiwoomClient
    from data.db import get_watchlist

    kiwoom = KiwoomClient()
    existing_codes = {s["code"] for s in get_watchlist()}

    candidates = []

    def _extract(items, source, limit=20):
        for item in items[:limit]:
            code = str(item.get("stk_cd") or item.get("shtn_iscd") or "").strip()
            name = str(item.get("hts_kor_isnm") or item.get("stk_nm") or "").strip()
            if code and code not in existing_codes and len(code) == 6:
                if not any(c["stock_code"] == code for c in candidates):
                    candidates.append({"stock_code": code, "stock_name": name, "source": source})

    # 1. 거래량 급증 종목 (ka10023)
    try:
        _extract(kiwoom.get_volume_surge(), "거래량 급증", 20)
    except Exception as e:
        logger.warning(f"거래량 급증 조회 실패: {e}")

    time.sleep(1)

    # 2. 전일대비 등락률 하위 — 눌림목 후보 (ka10027)
    try:
        _extract(kiwoom.get_decline_rank(), "눌림목 후보", 20)
    except Exception as e:
        logger.warning(f"등락률 하위 조회 실패: {e}")

    time.sleep(1)

    # 3. 외인 연속 순매수 상위 (ka10035)
    try:
        _extract(kiwoom.get_foreign_net_buy(), "외인 순매수", 10)
    except Exception as e:
        logger.warning(f"외인 순매수 조회 실패: {e}")

    logger.info(f"[스크리닝] 후보 {len(candidates)}개 발견")
    return candidates[:30]


# ═══════════════════════════ 장중 경량 스캔 ═══════════════════════════

def run_intraday_scan():
    """장중 거래량 급증 종목 경량 스캔 → 텔레그램 알림만 (watchlist 추가 안 함).

    - AI 분석 없이 거래량만 체크 (API 1회)
    - 기존 watchlist 종목은 제외
    - 관심 가면 사용자가 Claude Desktop에서 직접 분석 요청
    """
    from worker.clients.kiwoom_client import KiwoomClient
    from data.db import get_watchlist, get_cooldown, set_cooldown
    from notifications.telegram import send_message
    from datetime import datetime

    kiwoom = KiwoomClient()
    existing_codes = {s["code"] for s in get_watchlist()}

    # 쿨다운: 같은 종목은 하루에 1번만 알림
    movers = []
    try:
        for item in kiwoom.get_volume_surge()[:15]:
            code = str(item.get("stk_cd") or item.get("shtn_iscd") or "").strip()
            name = str(item.get("hts_kor_isnm") or item.get("stk_nm") or "").strip()
            if not code or code in existing_codes or len(code) != 6:
                continue

            # 하루 1번 쿨다운
            key = f"intraday_scan:{code}"
            last = get_cooldown(key)
            if last and (datetime.now() - last).total_seconds() < 43200:  # 12시간
                continue

            prc = str(item.get("cur_prc") or item.get("stk_prpr") or "").replace(",", "")
            chg = str(item.get("prdy_ctrt") or item.get("flu_rt") or "").replace(",", "")
            vol = str(item.get("trde_qty") or item.get("acml_vol") or "").replace(",", "")

            movers.append({"code": code, "name": name, "price": prc, "change": chg, "volume": vol})
            set_cooldown(key)
    except Exception as e:
        logger.warning(f"장중 스캔 실패: {e}")
        return

    if not movers:
        logger.info("[장중 스캔] 특이 종목 없음")
        return

    lines = [f"📡 *[장중 스캔] 거래량 급증 {len(movers)}종목*\n"]
    for m in movers[:10]:
        lines.append(f"• *{m['name']}* (`{m['code']}`) {m['price']}원 ({m['change']}%) vol:{m['volume']}")
    lines.append("\n_관심 종목은 Claude Desktop에서 \"XX 분석해줘\"로 상세 분석_")

    send_message("\n".join(lines))
    logger.info(f"[장중 스캔] {len(movers)}개 종목 알림 발송")


# ═══════════════════════════ 스크리닝 AI 시스템 프롬프트 (캐싱) ═══════════════════════════

_SCREENING_KNOWLEDGE = """## 트레이딩 분석 지식 — 종목 스크리닝용

### 1. 캔들 분석
[캔들 구조] 양봉=매수세 우위, 음봉=매도세 우위. 긴 아랫꼬리=매도 후 매수 반등. 긴 윗꼬리=매수 후 매도 압력.
[주요 패턴]
- 망치형: 하락 후 긴 아랫꼬리 양봉 → 반등 신호
- 역망치형: 상승 후 긴 윗꼬리 음봉 → 조정 신호
- 도지: 시가≈종가 → 추세 전환 초기 신호
- 불리시 엔걸핑: 음봉 후 감싸는 양봉 → 강한 상승 전환
- 베어리시 엔걸핑: 양봉 후 감싸는 음봉 → 강한 하락 전환

### 2. 거래량 분석
가격↑ + 거래량↑ = 강한 매수세 (추세 강화)
가격↑ + 거래량↓ = 매수세 약화 (신뢰도 낮음)
가격↓ + 거래량↑ = 강한 매도세 (추가 하락 가능)
가격↓ + 거래량↓ = 매도압력 약화 (반등 가능)
[볼륨 스프레드] 큰 몸통+높은 거래량=강한 추세 / 작은 몸통+높은 거래량=전환 가능 / 큰 몸통+낮은 거래량=속임수 가능
[OBV] 상승=매수세 우위, 하락=매도세 우위. 가격과 불일치=다이버전스

### 3. 추세·지지·저항
상승 추세: 고점·저점 점진 상승. 하락 추세: 고점·저점 점진 하락.
과거 고점=저항, 과거 저점=지지. 거래량 집중 가격대=강한 지지/저항.
저항 돌파 후 되돌림 성공 → 지지 전환. 지지 이탈 후 반등 실패 → 저항 전환.
돌파 시 거래량 급증 동반 = 신뢰도 높음.

### 4. 이동평균선(MA)
현재가 > MA5 > MA20: 강한 상승 (매수 유리)
MA5 > MA20 + 현재가 < MA5: 단기 눌림목 (지지 확인 후 entry 유리)
현재가 < MA5 < MA20: 강한 하락 (매수 신중)
골든크로스: 단기MA > 장기MA 전환 → 상승 추세 시작
데드크로스: 단기MA < 장기MA 전환 → 하락 추세 시작

### 5. RSI
RSI ≤ 30: 극과매도, 강한 반등 가능
RSI 31~43: 과매도, entry 신뢰도 높음
RSI 44~55: 중립
RSI 56~65: 과매수 접근
RSI ≥ 66: 과매수
[다이버전스] 강세: 가격 신저점 + RSI 저점 상승 → 반등 임박. 약세: 가격 신고점 + RSI 고점 하락 → 조정 임박.

### 6. 스토캐스틱·CCI·일목균형표
스토캐스틱: %K/%D 교차. 과매수≥80, 과매도≤20. 과매도에서 골든크로스=강한 매수 신호.
CCI: <-100 반등=매수 신호. >+100 하락=매도 신호.
일목: 구름대 위=상승, 아래=하락, 내부=중립. 전환선>기준선=매수 신호. 두꺼운 구름대=강한 지지/저항.

### 7. 차트 패턴
[반전] 이중 바닥→상승 전환 / 이중 천장→하락 전환 / 헤드앤숄더→하락 / 역 헤드앤숄더→상승
[지속] 상승 삼각형→돌파 시 강한 상승 / 깃발형→추세 연장 / 컵 위드 핸들→돌파 시 상승
[패턴+거래량] 돌파 시 거래량 급증 동반 = 신뢰도 높음

### 8. 피보나치
되돌림: 38.2~61.8% = 정상 되돌림, 매수 구간. 78.6% 초과 = 추세 전환 가능.
확장: 127.2%, 161.8%, 200% — 목표가 설정에 활용.
피보나치 지지 + MA 지지 + 거래량 증가 동시 = 높은 신뢰도 매수 포인트.

### 9. 공시 분석 (DART)
수주/계약=매출 성장 기대 / 실적 발표=서프라이즈/쇼크 판단 / 유증/CB/BW=주식 희석 단기 악재
호재 공시 + RSI 과매도 + 거래량 급증 = 강한 entry 신호.
악재 공시 + MA 하향 이탈 = 부적합 종목."""


_SCREENING_SYSTEM_PROMPT = f"""당신은 개인 투자자의 퀀트 트레이딩 시스템에서 신규 종목 편입 적합성을 판단하는 스크리닝 AI입니다.
후보 종목의 기술적 지표, 재무, 공시, 뉴스를 분석하여 편입 여부와 초기 전략을 제안합니다.

{_SCREENING_KNOWLEDGE}

## 편입 조건 4가지 (2가지 이상 충족 필요)
1. **눌림목**: 상승 추세 중 조정 구간에 진입한 종목
   - MA20 지지선 근처 (현재가가 MA20 ± 3% 이내)
   - RSI 38~50 구간 (과매도 진입 또는 진입 직전)
   - 피보나치 38.2~61.8% 되돌림 구간
   - 거래량 감소 중 (매도세 약화 = 반등 가능)
   → 3개 이상 동시 충족 시 강한 눌림목

2. **저평가**: 펀더멘털 대비 주가가 낮은 종목
   - PER이 동종업계 평균 대비 낮음 (또는 절대 PER < 10)
   - PBR < 1 (자산가치 대비 저평가)
   - 영업이익률 양호한데 주가 하락 중
   → 재무제표 데이터로 판단. 데이터 없으면 이 조건 평가 불가로 처리

3. **테마 미반영**: 호재가 있으나 주가에 반영되지 않은 종목
   - 최근 공시에 수주/계약/신사업/정책 수혜 내용이 있으나 주가 횡보/하락
   - 뉴스에 긍정적 이슈가 있으나 거래량 미동반
   → 공시/뉴스 데이터 없으면 이 조건 평가 불가로 처리

4. **실적 개선**: 매출/영업이익이 증가 추세인 종목
   - 최근 2~3분기 매출 또는 영업이익 연속 증가
   - 적자→흑자 전환 또는 흑자 폭 확대
   → 재무제표 데이터로 판단. 데이터 없으면 이 조건 평가 불가로 처리

## 부적합 필터 (1개라도 해당 시 즉시 부적합)
- 데드크로스 발생 중 (MA5 < MA20 + 하향 진행)
- RSI > 70 (이미 과매수)
- 거래량 없이 급등 (속임수 가능)
- 악재 공시 발견 (유증, CB, 관리종목 등)
- 시가총액 500억 미만 (유동성 리스크)

## 목표가·손절가 설정 기준
- 목표가: 피보나치 확장 127.2~161.8% 또는 직전 고점 저항선 기준
- 손절가: 평단 대비 -5~-10%. 최소한 직전 지지선 아래로 설정
- R/R 2:1 이상 확보 필수 (목표 수익폭 ≥ 손절 손실폭 × 2)
- R/R 2:1 미만이면 편입 보류 권고

## RSI 임계값 설정 기준
- rsi_oversold: 일봉 RSI14 기준. 종목 변동성에 따라 38~43 범위.
  변동성 높은 종목=38~40 / 안정적 종목=41~43
- rsi_overbought: 60~75 범위.
  스윙 매매(중기)=65~70 / 단기 매매=60~65

## horizon 설정 기준
- 단기: 급등 후 조정 종목, 거래량 급증 동반, 뉴스/이벤트 기반
- 중기: 추세 전환 초기, 실적 개선 초기, 눌림목 진입
- 장기: 저평가 가치주, 성장주 초기 진입

## 활성화 가능한 모니터링 조건 (29개)
아래에서 종목 특성에 맞는 조건만 활성화하고, 각 조건에 대해 왜 활성화/비활성화했는지 근거를 제시하세요.

[가격 조건]
- target_price: 목표가 도달 알림 (exit) — 목표가 설정 시 필수 활성화
- stop_loss_price: 손절가 도달 알림 (exit) — 손절가 설정 시 필수 활성화

[RSI 조건]
- rsi_oversold: RSI 과매도 (entry) — 값: 38~43
- rsi_overbought: RSI 과매수 (exit) — 값: 60~75
- rsi_oversold_intraday: RSI 5분봉 과매도 (entry, 단기만) — 값: 30~35
- rsi_critical: RSI 극단적 과매도 경고 (both) — 값: 25~30
- rsi_oversold_add: RSI 과매도 물타기 (add)

[이동평균 조건]
- golden_cross: MA 골든크로스 (entry)
- death_cross: MA 데드크로스 (exit)
- ma20_support_break: MA20 하향 이탈 (exit)
- ma5_support_break: MA5 하향 이탈 (both)
- ma5_recovery: MA5 상향 돌파 회복 (entry)
- ma5_recovery_add: MA5 회복 추가매수 (add)
- new_high_20d: 20일 신고가 돌파 (both)

[MACD 조건]
- macd_golden_cross: MACD 골든크로스 (entry)
- macd_death_cross: MACD 데드크로스 (exit)

[볼린저 조건]
- bollinger_upper_break: 볼린저 상단 돌파 (both)
- bollinger_lower_break: 볼린저 하단 이탈 (entry)
- bollinger_lower_break_add: 볼린저 하단 물타기 (add)
- bollinger_critical_below: 볼린저 하단 3% 이탈 경고 (both)

[스토캐스틱 조건]
- stochastic_golden_cross: 스토캐스틱 골든크로스 (entry)
- stochastic_death_cross: 스토캐스틱 데드크로스 (exit)

[CCI 조건]
- cci_oversold: CCI 과매도 (entry) — 값: -100
- cci_overbought: CCI 과매수 (exit) — 값: 100

[일목균형표 조건]
- ichimoku_golden_cross: 일목 전환선 골든크로스 (entry)
- ichimoku_death_cross: 일목 전환선 데드크로스 (exit)
- ichimoku_cloud_breakout: 일목 구름대 돌파 (entry)
- ichimoku_cloud_breakdown: 일목 구름대 이탈 (exit)

[거래량 조건]
- volume_surge_ratio: 거래량 급증 (both) — 값: 1.5~3.0배

## 출력 형식 (반드시 JSON만 출력, 다른 텍스트 금지)
```json
{{
    "recommendation": "관심종목 등록" 또는 "보류" 또는 "부적합",
    "reason": "판단 근거 2~3문장",
    "met_conditions": ["충족된 편입조건명"],
    "disqualifiers": ["부적합 사유 (있을 때만)"],
    "target_price": 목표가(정수),
    "target_price_reason": "목표가 설정 근거 한 줄",
    "stop_loss_price": 손절가(정수),
    "stop_loss_price_reason": "손절가 설정 근거 한 줄",
    "horizon": "단기" 또는 "중기" 또는 "장기",
    "horizon_reason": "매매 기간 설정 근거 한 줄",
    "rsi_oversold": RSI 과매도 기준값(정수, 38~43),
    "rsi_oversold_reason": "과매도 기준 설정 근거 한 줄",
    "rsi_overbought": RSI 과매수 기준값(정수, 60~75),
    "rsi_overbought_reason": "과매수 기준 설정 근거 한 줄",
    "rr_ratio": 손익비(소수점 1자리),
    "enabled_conditions": {{
        "조건id": {{"enabled": true/false, "reason": "활성화/비활성화 근거 한 줄", "value": 값(해당시)}},
        ...전체 29개 조건에 대해 명시
    }}
}}
```

판단 원칙:
- 수치 기반 판단만 허용. "느낌", "분위기"로 판단 금지.
- 데이터 부족 시 해당 조건은 "평가 불가"로 처리하고, 나머지 조건으로 판단.
- 보수적으로 판단. 확실하지 않으면 "보류".
- 관심종목 등록 시 반드시 목표가·손절가·R/R을 수치로 제시.
- 각 임계값과 조건 활성화의 설정 근거를 반드시 한 줄로 명시.
- target_price, stop_loss_price는 관심종목 등록 시 반드시 활성화.
- 단기 종목이 아니면 rsi_oversold_intraday 비활성화.
- 불필요한 add 조건은 초기 등록 시 비활성화 (보유 전이므로)."""


# ═══════════════════════════ AI 편입 분석 ═══════════════════════════

def _analyze_candidate(stock_code: str, stock_name: str, kiwoom=None, market_text: str | None = None) -> dict:
    """후보 종목 1개를 차트+공시+뉴스로 분석하여 편입 적합성 판단.

    Returns:
        {recommendation, reason, target_price, stop_loss_price, horizon, rsi_oversold, rsi_overbought}
    """
    from worker.clients.kiwoom_client import KiwoomClient
    from worker.indicators import calculate_rsi, calculate_volume_ratio, calculate_chart_summary

    if kiwoom is None:
        kiwoom = KiwoomClient()

    # 1. 현재가 + 차트 데이터
    price_data = kiwoom.get_current_price(stock_code)
    current_price = abs(int(str(
        price_data.get("cur_prc") or price_data.get("stk_prpr") or price_data.get("prpr") or "0"
    ).replace(",", "")))

    if not stock_name:
        stock_name = price_data.get("hts_kor_isnm") or stock_code

    daily_data = kiwoom.get_daily_ohlcv(stock_code, period=90)
    close_prices, high_prices, low_prices, open_prices, volumes = [], [], [], [], []
    for d in daily_data:
        cp = abs(int(str(d.get("cur_prc", "0")).replace(",", "") or "0"))
        hp = abs(int(str(d.get("high_pric", "0")).replace(",", "") or "0"))
        lp = abs(int(str(d.get("lwst_pric", "0") or d.get("low_pric", "0")).replace(",", "") or "0"))
        op = abs(int(str(d.get("strt_pric", "0") or d.get("opn_pric", "0")).replace(",", "") or "0"))
        vol = abs(int(str(d.get("trde_qty", "0")).replace(",", "") or "0"))
        if cp: close_prices.append(cp)
        if hp: high_prices.append(hp)
        if lp: low_prices.append(lp)
        if op: open_prices.append(op)
        if vol: volumes.append(vol)

    rsi = calculate_rsi(close_prices) if len(close_prices) >= 15 else None
    volume_ratio = calculate_volume_ratio(volumes) if len(volumes) >= 21 else None
    chart = calculate_chart_summary(
        close_prices, high_prices, current_price,
        low_prices=low_prices, open_prices=open_prices, volumes=volumes,
    ) if len(close_prices) >= 5 else None

    if not chart or not _ai_client:
        return {"recommendation": "분석 불가"}

    # 2. DART 공시 + 재무
    dart_text = ""
    try:
        from worker.clients.dart_client import format_full_context_for_ai, DART_API_KEY
        if DART_API_KEY:
            dart_text = format_full_context_for_ai(stock_code)
    except Exception:
        pass

    # 3. 뉴스
    news_text = ""
    try:
        from worker.clients.news_client import format_news_for_ai, NAVER_CLIENT_ID
        if NAVER_CLIENT_ID:
            news_text = format_news_for_ai(stock_name, max_items=5)
    except Exception:
        pass

    # 4. 포트폴리오 맥락 (현재 보유 종목 수, 현금 비중 등)
    portfolio_context = ""
    try:
        from data.db import get_conn
        with get_conn() as conn:
            watchlist_count = conn.execute("SELECT COUNT(*) FROM watchlist WHERE enabled = 1").fetchone()[0]
            portfolio_rows = conn.execute("SELECT stock_name, profit_rate FROM portfolio").fetchall()
        holding_count = len(portfolio_rows)
        if portfolio_rows:
            holdings_summary = ", ".join(
                f"{r['stock_name']}({r['profit_rate']:+.1f}%)" for r in portfolio_rows[:10]
            )
            portfolio_context = (
                f"현재 보유: {holding_count}종목 ({holdings_summary})\n"
                f"관심종목: {watchlist_count}개"
            )
        else:
            portfolio_context = f"현재 보유 없음 / 관심종목 {watchlist_count}개"
    except Exception:
        portfolio_context = "포트폴리오 조회 실패"

    # 5. 시장 환경
    if not market_text:
        market_text = _build_market_text(kiwoom)

    # 6. 차트 분석 텍스트 (claude_judge.py 수준)
    stoch_text = ""
    if chart.stochastic_k is not None:
        stoch_level = ""
        if chart.stochastic_k >= 80: stoch_level = " (과매수)"
        elif chart.stochastic_k <= 20: stoch_level = " (과매도)"
        cross = ""
        if getattr(chart, 'stochastic_golden_cross', False): cross = " ★골든크로스"
        elif getattr(chart, 'stochastic_death_cross', False): cross = " ★데드크로스"
        d_str = f"/ %D {chart.stochastic_d:.0f}" if chart.stochastic_d is not None else ""
        stoch_text = f"%K {chart.stochastic_k:.0f} {d_str}{stoch_level}{cross}"

    cci_text = ""
    if chart.cci is not None:
        cci_level = ""
        if chart.cci > 100: cci_level = " (과매수)"
        elif chart.cci < -100: cci_level = " (과매도)"
        cci_text = f"{chart.cci:.0f}{cci_level}"

    ichimoku_parts = []
    if chart.ichimoku_above_cloud is True:
        ichimoku_parts.append("구름대 위 (상승)")
    elif chart.ichimoku_above_cloud is False:
        ichimoku_parts.append("구름대 아래 (하락)")
    else:
        ichimoku_parts.append("구름대 내부 (중립)")
    if getattr(chart, 'ichimoku_cloud_thickness', None) is not None:
        label = "두꺼움" if chart.ichimoku_cloud_thickness > 3 else "얇음" if chart.ichimoku_cloud_thickness < 1 else "보통"
        ichimoku_parts.append(f"두께 {chart.ichimoku_cloud_thickness:.1f}% [{label}]")

    fib_text = ""
    if chart.fibonacci:
        f = chart.fibonacci
        levels = [
            ("23.6%", _fmt_int(f.get("fib_236"), 0)),
            ("38.2%", _fmt_int(f.get("fib_382"), 0)),
            ("50%", _fmt_int(f.get("fib_500"), 0)),
            ("61.8%", _fmt_int(f.get("fib_618"), 0)),
        ]
        nearest = min(levels, key=lambda x: abs(x[1] - current_price))
        swing_high = _fmt_int(f.get("swing_high"), 0)
        swing_low = _fmt_int(f.get("swing_low"), 0)
        ext_1272 = _fmt_int(f.get("ext_1272"), 0)
        ext_1618 = _fmt_int(f.get("ext_1618"), 0)
        fib_text = (f"고점 {swing_high:,} / 저점 {swing_low:,}"
                    f" — 근접 레벨: {nearest[0]}({nearest[1]:,})"
                    f" | 확장: 127.2%={ext_1272:,} / 161.8%={ext_1618:,}")

    # MA20 대비 거리 (눌림목 판단용)
    ma20_dist = ""
    if chart.ma20:
        dist_pct = (current_price - chart.ma20) / chart.ma20 * 100
        ma20_dist = f" (MA20 대비 {dist_pct:+.1f}%)"

    # 연속 하락일
    consecutive_down = 0
    prices_with_cur = (list(getattr(chart, 'recent_10d_prices', []) or []) + [current_price])
    if len(prices_with_cur) >= 2:
        for i in range(len(prices_with_cur) - 1, 0, -1):
            if prices_with_cur[i] < prices_with_cur[i - 1]:
                consecutive_down += 1
            else:
                break

    # 7. 유저 프롬프트 조립
    ma5_val = _fmt_int(getattr(chart, "ma5", None), 0)
    ma20_val = _fmt_int(getattr(chart, "ma20", None), 0)
    support_val = _fmt_int(getattr(chart, "support_level", None), 0)
    resist_val = _fmt_int(getattr(chart, "resistance_level", None), 0)
    macd_line = getattr(chart, "macd_line", None)
    macd_signal = getattr(chart, "macd_signal", None)
    macd_line_text = f"{macd_line:.0f}" if isinstance(macd_line, (int, float)) else "N/A"
    macd_signal_text = f"{macd_signal:.0f}" if isinstance(macd_signal, (int, float)) else "N/A"

    user_prompt = f"""## 종목 정보
- 종목: {stock_name} ({stock_code})
- 현재가: {current_price:,}원

## 기술적 지표
- MA5: {ma5_val:,}원 / MA20: {ma20_val:,}원{ma20_dist}
- 추세: {chart.trend} (현재가 MA5 {'위' if chart.above_ma5 else '아래'} / MA20 {'위' if chart.above_ma20 else '아래'})
- RSI(14): {rsi if rsi else 'N/A'}
- 거래량 배율: {f'{volume_ratio:.1f}배' if volume_ratio else 'N/A'}
- 스토캐스틱: {stoch_text or 'N/A'}
- CCI: {cci_text or 'N/A'}
- 일목균형표: {' / '.join(ichimoku_parts)}
- MACD: {macd_line_text} / Signal: {macd_signal_text}
- 볼린저: 상단 {int(chart.bollinger_upper or 0):,} / 하단 {int(chart.bollinger_lower or 0):,}
- OBV: {chart.obv_trend or 'N/A'}
- 지지: {support_val:,}원 / 저항: {resist_val:,}원
- 피보나치: {fib_text or 'N/A'}
- RSI 다이버전스: {chart.rsi_divergence or '없음'}
- MACD 다이버전스: {chart.macd_divergence or '없음'}
- 캔들 패턴: {', '.join(chart.candle_patterns) if chart.candle_patterns else '없음'}
- 차트 패턴: {', '.join(chart.chart_patterns) if chart.chart_patterns else '없음'}
- 볼륨 스프레드: {getattr(chart, 'volume_spread', '없음') or '없음'}
- 거래량 추세: {getattr(chart, 'volume_price_trend', '없음') or '없음'}
- 연속 하락: {consecutive_down}일
- 5일 전 대비: {f'{chart.price_change_5d:+.2f}%' if chart.price_change_5d is not None else 'N/A'}

## DART 공시/재무
{dart_text or '데이터 없음'}

## 최근 뉴스
{news_text or '데이터 없음'}

## 포트폴리오 현황
{portfolio_context}

## 시장 환경
{market_text}"""

    try:
        if _AI_BACKEND == "anthropic":
            response = _ai_client.messages.create(
                model=_AI_MODEL, max_tokens=1000,
                system=[
                    {
                        "type": "text",
                        "text": _SCREENING_SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_prompt}],
            )
            ai_text = response.content[0].text
        else:
            response = _ai_client.chat.completions.create(
                model=_AI_MODEL, max_tokens=1000,
                messages=[
                    {"role": "system", "content": _SCREENING_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
            )
            ai_text = response.choices[0].message.content

        # 중첩 JSON(enabled_conditions) 포함 응답 파싱
        json_text = _extract_json_block(ai_text)
        if json_text:
            try:
                result = json.loads(json_text)
            except Exception:
                # 1차 파싱 실패 시 JSON 정규화 재요청 후 재시도
                repaired = _repair_screening_json(ai_text)
                repaired_block = _extract_json_block(repaired) or repaired.strip()
                result = json.loads(repaired_block)

            # R/R 2:1 미만이면 관심종목 등록 → 보류로 강제 변환
            rr = result.get("rr_ratio", 0)
            if result.get("recommendation") == "관심종목 등록" and rr and float(rr) < 2.0:
                logger.info(f"[스크리닝] {stock_name}: R/R {rr} < 2.0 → 보류로 변환")
                result["recommendation"] = "보류"
                result["reason"] = f"R/R {rr}:1 미달 (2:1 이상 필요). " + result.get("reason", "")
            return result

        return {"recommendation": "분석 실패"}

    except Exception as e:
        logger.error(f"스크리닝 AI 분석 오류 ({stock_name}): {e}")
        return {"recommendation": "분석 실패"}


# ═══════════════════════════ watchlist 추가 ═══════════════════════════

def add_to_watchlist(stock_code: str, stock_name: str, analysis: dict) -> bool:
    """분석 결과로 watchlist에 추가. 자동/수동 모드 공용."""
    from data.db import get_conn

    # AI가 제안한 조건별 활성화 설정 사용
    ai_conditions = analysis.get("enabled_conditions", {})
    conditions = {}

    # 값이 있는 조건 (임계값 설정)
    value_fields = {
        "target_price", "stop_loss_price", "rsi_oversold", "rsi_overbought",
        "rsi_oversold_intraday", "rsi_critical", "volume_surge_ratio",
        "cci_oversold", "cci_overbought",
    }
    # 불리언 조건 (활성화/비활성화만)
    flag_fields = {
        "golden_cross", "death_cross", "ma20_support_break", "ma5_support_break",
        "ma5_recovery", "ma5_recovery_add", "new_high_20d",
        "macd_golden_cross", "macd_death_cross",
        "bollinger_upper_break", "bollinger_lower_break",
        "bollinger_lower_break_add", "bollinger_critical_below",
        "stochastic_golden_cross", "stochastic_death_cross",
        "ichimoku_golden_cross", "ichimoku_death_cross",
        "ichimoku_cloud_breakout", "ichimoku_cloud_breakdown",
        "rsi_oversold_add",
    }

    for cond_id, cond_info in ai_conditions.items():
        if not isinstance(cond_info, dict) or not cond_info.get("enabled"):
            continue
        if cond_id in value_fields:
            val = cond_info.get("value")
            if val is None:
                val = analysis.get(cond_id, 0)
            conditions[cond_id] = val
        elif cond_id in flag_fields:
            conditions[cond_id] = True

    # AI가 enabled_conditions를 안 줬을 때 fallback
    if not conditions:
        conditions = {
            "target_price": analysis.get("target_price", 0),
            "stop_loss_price": analysis.get("stop_loss_price", 0),
            "rsi_oversold": analysis.get("rsi_oversold", 40),
            "rsi_overbought": analysis.get("rsi_overbought", 65),
            "golden_cross": True,
            "death_cross": True,
            "volume_surge_ratio": 2.0,
            "bollinger_lower_break": True,
            "ma20_support_break": True,
        }

    horizon = analysis.get("horizon", "중기")

    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO watchlist (code, name, enabled, conditions, horizon) VALUES (?, ?, 1, ?, ?)",
            (stock_code, stock_name, json.dumps(conditions, ensure_ascii=False), horizon),
        )
        conn.commit()

    logger.info(f"[관심종목 등록] {stock_name}({stock_code}) horizon={horizon} "
                f"목표={analysis.get('target_price')} 손절={analysis.get('stop_loss_price')}")
    return True


def _format_screening_alert(stock_name: str, stock_code: str, source: str, analysis: dict) -> str:
    """스크리닝 결과를 텔레그램 알림 텍스트로 포맷."""
    reason = analysis.get("reason", "")
    met_conditions = analysis.get("met_conditions", [])
    if not isinstance(met_conditions, list):
        met_conditions = []
    met = ", ".join(met_conditions)
    rr = analysis.get("rr_ratio", "N/A")
    target_price = _fmt_int(analysis.get("target_price"), 0)
    stop_loss_price = _fmt_int(analysis.get("stop_loss_price"), 0)
    rsi_oversold = _fmt_int(analysis.get("rsi_oversold"), 40)
    rsi_overbought = _fmt_int(analysis.get("rsi_overbought"), 65)

    lines = [
        f"🔍 *{stock_name}* ({stock_code}) — {source}",
        f"",
        f"*[AI 분석]* {met} 충족. R/R {rr}:1",
        f"{reason}",
        f"",
        f"📌 *제안 전략*",
        f"• 목표가 {target_price:,}원 — {analysis.get('target_price_reason', '')}",
        f"• 손절가 {stop_loss_price:,}원 — {analysis.get('stop_loss_price_reason', '')}",
        f"• RSI 과매도 {rsi_oversold} — {analysis.get('rsi_oversold_reason', '')}",
        f"• RSI 과매수 {rsi_overbought} — {analysis.get('rsi_overbought_reason', '')}",
        f"• {analysis.get('horizon', '중기')} — {analysis.get('horizon_reason', '')}",
    ]

    # 활성화된 조건 근거 표시
    enabled_conditions = analysis.get("enabled_conditions", {})
    if enabled_conditions:
        enabled = [(k, v) for k, v in enabled_conditions.items()
                   if isinstance(v, dict) and v.get("enabled")
                   and k not in ("target_price", "stop_loss_price")]
        disabled_important = [(k, v) for k, v in enabled_conditions.items()
                              if isinstance(v, dict) and not v.get("enabled")
                              and v.get("reason")]

        if enabled:
            lines.append("")
            lines.append(f"📋 *활성 조건* ({len(enabled)}개)")
            for cond_id, info in enabled:
                val_str = f" ({info['value']})" if info.get("value") else ""
                lines.append(f"  ✅ {cond_id}{val_str} — {info.get('reason', '')}")

        # 주요 비활성 조건 (근거가 있는 것만, 최대 5개)
        if disabled_important:
            notable = [d for d in disabled_important
                       if d[0] in ("rsi_oversold_intraday", "rsi_critical",
                                   "ichimoku_golden_cross", "ichimoku_death_cross",
                                   "stochastic_golden_cross", "stochastic_death_cross")][:5]
            if notable:
                lines.append("")
                lines.append("🚫 *주요 비활성 조건*")
                for cond_id, info in notable:
                    lines.append(f"  ❌ {cond_id} — {info.get('reason', '')}")

    return "\n".join(lines)


def _is_auto_mode() -> bool:
    """AUTO_TRADE 환경변수로 자동/수동 모드 판별."""
    return os.getenv("AUTO_TRADE", "false").strip().lower() == "true"


# ═══════════════════════════ 메인: 일일 자동 스크리닝 ═══════════════════════════

def run_daily_screening():
    """일일 자동 스크리닝: 후보 발굴 → AI 분석 → 모드에 따라 자동 편입 or 사용자 승인 요청."""
    from notifications.telegram import send_message, send_message_with_inline_buttons
    from data.db import save_strategy_note
    from worker.clients.kiwoom_client import KiwoomClient

    auto_mode = _is_auto_mode()
    kiwoom = KiwoomClient()
    market_text = _build_market_text(kiwoom)

    candidates = _screen_candidates()
    if not candidates:
        logger.info("[스크리닝] 후보 없음")
        return

    added = []
    pending = []
    for cand in candidates:
        try:
            logger.info(f"[스크리닝] 분석 중: {cand['stock_name']} ({cand['stock_code']})")
            analysis = _analyze_candidate(
                cand["stock_code"],
                cand["stock_name"],
                kiwoom=kiwoom,
                market_text=market_text,
            )
            rec = analysis.get("recommendation", "분석 실패")
            rr = analysis.get("rr_ratio", "N/A")
            reason = str(analysis.get("reason", "") or "").replace("\n", " ").strip()
            logger.info(
                f"[스크리닝] 분석 결과: {cand['stock_name']} ({cand['stock_code']}) "
                f"→ {rec} (R/R={rr}) 사유: {reason[:140]}"
            )

            if analysis.get("recommendation") != "관심종목 등록":
                time.sleep(2)
                continue

            alert_text = _format_screening_alert(
                cand["stock_name"], cand["stock_code"], cand["source"], analysis,
            )

            if auto_mode:
                # 자동 모드: 즉시 등록 + 근거 포함 알림
                add_to_watchlist(cand["stock_code"], cand["stock_name"], analysis)
                send_message(f"{alert_text}\n\n✅ *자동 관심종목 등록 완료*")
                added.append(cand["stock_name"])
            else:
                # 수동 모드: 근거 포함 알림 + 승인 버튼
                buttons = [
                    [
                        {"text": "✅ 관심종목 등록", "callback_data": f"screen_add:{cand['stock_code']}"},
                        {"text": "❌ 패스", "callback_data": f"screen_pass:{cand['stock_code']}"},
                    ]
                ]
                # analysis를 임시 저장 (텔레그램 봇에서 콜백 시 사용)
                _pending_screenings[cand["stock_code"]] = {
                    "stock_name": cand["stock_name"],
                    "analysis": analysis,
                }
                send_message_with_inline_buttons(alert_text, buttons)
                pending.append(cand["stock_name"])

            time.sleep(2)  # API rate limit
        except Exception as e:
            logger.error(f"[스크리닝] {cand['stock_name']} 분석 실패: {e}")

    # 결과 로그
    if added:
        summary = f"자동 스크리닝: {', '.join(added)} 관심종목 등록"
        save_strategy_note("watchlist", summary, summary)
        logger.info(f"[스크리닝] {len(added)}개 종목 자동 등록 완료")
    if pending:
        logger.info(f"[스크리닝] {len(pending)}개 종목 사용자 승인 대기 중")
    if not added and not pending:
        logger.info("[스크리닝] 적합 종목 없음")


# 수동 모드에서 사용자 승인 대기 중인 스크리닝 결과
_pending_screenings: dict[str, dict] = {}


def handle_screening_callback(stock_code: str, action: str) -> str:
    """텔레그램 봇에서 스크리닝 콜백 처리.

    Returns:
        응답 메시지 텍스트
    """
    pending = _pending_screenings.pop(stock_code, None)
    if not pending:
        return "⚠️ 만료된 요청입니다."

    if action == "add":
        add_to_watchlist(stock_code, pending["stock_name"], pending["analysis"])
        from data.db import save_strategy_note
        save_strategy_note(
            "watchlist",
            f"수동 스크리닝: {pending['stock_name']} 관심종목 등록",
            f"사용자 승인으로 등록. {pending['analysis'].get('reason', '')}",
        )
        return f"✅ {pending['stock_name']} 관심종목 등록 완료"
    else:
        return f"❌ {pending['stock_name']} 패스"
