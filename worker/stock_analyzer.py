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

    # 1. 거래량 급증 종목 (ka10023)
    try:
        vol_surge = kiwoom._call_api("ka10023", {})
        for item in (vol_surge.get("output") or vol_surge.get("output1") or [])[:20]:
            code = str(item.get("stk_cd") or item.get("shtn_iscd") or "").strip()
            name = str(item.get("hts_kor_isnm") or item.get("stk_nm") or "").strip()
            if code and code not in existing_codes and len(code) == 6:
                candidates.append({"stock_code": code, "stock_name": name, "source": "거래량 급증"})
    except Exception as e:
        logger.warning(f"거래량 급증 조회 실패: {e}")

    time.sleep(1)

    # 2. 전일대비 등락률 하위 — 눌림목 후보 (ka10027)
    try:
        dip_stocks = kiwoom._call_api("ka10027", {"flu_tp": "2"})
        for item in (dip_stocks.get("output") or dip_stocks.get("output1") or [])[:20]:
            code = str(item.get("stk_cd") or item.get("shtn_iscd") or "").strip()
            name = str(item.get("hts_kor_isnm") or item.get("stk_nm") or "").strip()
            if code and code not in existing_codes and len(code) == 6:
                if not any(c["stock_code"] == code for c in candidates):
                    candidates.append({"stock_code": code, "stock_name": name, "source": "눌림목 후보"})
    except Exception as e:
        logger.warning(f"등락률 하위 조회 실패: {e}")

    time.sleep(1)

    # 3. 외인 연속 순매수 상위 (ka10035)
    try:
        foreign_buy = kiwoom._call_api("ka10035", {})
        for item in (foreign_buy.get("output") or foreign_buy.get("output1") or [])[:10]:
            code = str(item.get("stk_cd") or item.get("shtn_iscd") or "").strip()
            name = str(item.get("hts_kor_isnm") or item.get("stk_nm") or "").strip()
            if code and code not in existing_codes and len(code) == 6:
                if not any(c["stock_code"] == code for c in candidates):
                    candidates.append({"stock_code": code, "stock_name": name, "source": "외인 순매수"})
    except Exception as e:
        logger.warning(f"외인 순매수 조회 실패: {e}")

    logger.info(f"[스크리닝] 후보 {len(candidates)}개 발견")
    return candidates[:30]


# ═══════════════════════════ AI 편입 분석 ═══════════════════════════

def _analyze_candidate(stock_code: str, stock_name: str) -> dict:
    """후보 종목 1개를 차트+공시+뉴스로 분석하여 편입 적합성 판단.

    Returns:
        {recommendation, reason, target_price, stop_loss_price, horizon, rsi_oversold, rsi_overbought}
    """
    from worker.clients.kiwoom_client import KiwoomClient
    from worker.indicators import calculate_rsi, calculate_volume_ratio, calculate_chart_summary

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

    # 4. 피보나치 텍스트
    fib_text = ""
    if chart.fibonacci:
        f = chart.fibonacci
        fib_text = (f"피보나치: 고점 {f['swing_high']:,} / 저점 {f['swing_low']:,} | "
                    f"38.2%={f['fib_382']:,} | 61.8%={f['fib_618']:,} | "
                    f"확장 161.8%={f.get('ext_1618', 0):,}")

    # 5. AI 분석 요청
    prompt = f"""당신은 퀀트 트레이딩 시스템의 종목 스크리닝 AI입니다.
다음 종목의 편입 적합성을 분석하고, 편입 시 초기 전략을 제안하세요.

## 종목 정보
- 종목: {stock_name} ({stock_code})
- 현재가: {current_price:,}원

## 기술적 지표
- RSI(14): {rsi}
- 거래량 배율: {volume_ratio}배
- 추세: {chart.trend}
- MA5: {int(chart.ma5):,} / MA20: {int(chart.ma20):,} (현재가 MA5 {'위' if chart.above_ma5 else '아래'} / MA20 {'위' if chart.above_ma20 else '아래'})
- 스토캐스틱: %K={chart.stochastic_k} / %D={chart.stochastic_d}
- CCI: {chart.cci}
- 일목균형표: 구름대 {'위' if chart.ichimoku_above_cloud else '아래' if chart.ichimoku_above_cloud is False else '내부'}
- MACD: {chart.macd_line} / Signal: {chart.macd_signal}
- 볼린저: 상단 {int(chart.bollinger_upper or 0):,} / 하단 {int(chart.bollinger_lower or 0):,}
- OBV: {chart.obv_trend}
- 지지: {chart.support_level:,} / 저항: {chart.resistance_level:,}
- {fib_text}
- RSI 다이버전스: {chart.rsi_divergence or '없음'}
- MACD 다이버전스: {chart.macd_divergence or '없음'}
- 캔들 패턴: {', '.join(chart.candle_patterns) if chart.candle_patterns else '없음'}
- 차트 패턴: {', '.join(chart.chart_patterns) if chart.chart_patterns else '없음'}

## DART 공시/재무
{dart_text or '데이터 없음'}

## 최근 뉴스
{news_text or '데이터 없음'}

## 편입 조건 4가지 (2가지 이상 충족 필요)
1. 눌림목: MA20 지지 근처, RSI 과매도, 피보나치 되돌림 구간
2. 저평가: PER/PBR 동종업계 대비 낮음, 실적 대비 주가 저평가
3. 테마 미반영: 호재 공시/뉴스 있으나 주가 미반영
4. 실적 개선: 매출/영업이익 증가 추세

## 출력 형식 (JSON만 출력)
```json
{{
    "recommendation": "편입" 또는 "보류" 또는 "부적합",
    "reason": "판단 근거 2~3문장",
    "met_conditions": ["눌림목", "저평가"],
    "target_price": 목표가(정수),
    "stop_loss_price": 손절가(정수),
    "horizon": "단기" 또는 "중기" 또는 "장기",
    "rsi_oversold": RSI 과매도 기준값(정수, 38~43),
    "rsi_overbought": RSI 과매수 기준값(정수, 60~75)
}}
```
"""

    try:
        if _AI_BACKEND == "anthropic":
            response = _ai_client.messages.create(
                model=_AI_MODEL, max_tokens=800,
                messages=[{"role": "user", "content": prompt}],
            )
            ai_text = response.content[0].text
        else:
            response = _ai_client.chat.completions.create(
                model=_AI_MODEL, max_tokens=800,
                messages=[{"role": "user", "content": prompt}],
            )
            ai_text = response.choices[0].message.content

        json_match = re.search(r'\{[^{}]*"recommendation"[^{}]*\}', ai_text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())

        return {"recommendation": "분석 실패"}

    except Exception as e:
        logger.error(f"스크리닝 AI 분석 오류 ({stock_name}): {e}")
        return {"recommendation": "분석 실패"}


# ═══════════════════════════ watchlist 자동 추가 ═══════════════════════════

def _auto_add_to_watchlist(stock_code: str, stock_name: str, analysis: dict) -> bool:
    """분석 결과가 '편입'이면 watchlist에 자동 추가."""
    if analysis.get("recommendation") != "편입":
        return False

    from data.db import get_conn

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

    logger.info(f"[자동 편입] {stock_name}({stock_code}) horizon={horizon} "
                f"목표={analysis.get('target_price')} 손절={analysis.get('stop_loss_price')}")
    return True


# ═══════════════════════════ 메인: 일일 자동 스크리닝 ═══════════════════════════

def run_daily_screening():
    """일일 자동 스크리닝: 후보 발굴 → AI 분석 → 적합 종목 자동 편입 + 텔레그램 알림."""
    from notifications.telegram import send_message
    from data.db import save_strategy_note

    candidates = _screen_candidates()
    if not candidates:
        logger.info("[스크리닝] 후보 없음")
        return

    added = []
    for cand in candidates:
        try:
            logger.info(f"[스크리닝] 분석 중: {cand['stock_name']} ({cand['stock_code']})")
            analysis = _analyze_candidate(cand["stock_code"], cand["stock_name"])

            if analysis.get("recommendation") == "편입":
                _auto_add_to_watchlist(cand["stock_code"], cand["stock_name"], analysis)
                added.append({
                    "name": cand["stock_name"],
                    "code": cand["stock_code"],
                    "source": cand["source"],
                    "reason": analysis.get("reason", ""),
                    "target": analysis.get("target_price", 0),
                    "stop_loss": analysis.get("stop_loss_price", 0),
                    "horizon": analysis.get("horizon", ""),
                })

            time.sleep(2)  # API rate limit
        except Exception as e:
            logger.error(f"[스크리닝] {cand['stock_name']} 분석 실패: {e}")

    # 결과 알림
    if added:
        lines = [f"🔍 *[자동 스크리닝] {len(added)}개 종목 편입*\n"]
        for a in added:
            lines.append(
                f"• *{a['name']}* ({a['code']}) — {a['source']}\n"
                f"  {a['reason'][:60]}\n"
                f"  목표 {a['target']:,}원 / 손절 {a['stop_loss']:,}원 / {a['horizon']}"
            )
        msg = "\n".join(lines)
        send_message(msg)
        save_strategy_note(
            "watchlist",
            f"자동 스크리닝: {', '.join(a['name'] for a in added)} 편입",
            msg,
        )
        logger.info(f"[스크리닝] {len(added)}개 종목 자동 편입 완료")
    else:
        logger.info("[스크리닝] 편입 적합 종목 없음")
