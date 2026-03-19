"""
AI 매매 판단 — Anthropic Claude 또는 OpenAI GPT 사용
ANTHROPIC_API_KEY가 있으면 Claude, 없으면 OpenAI로 자동 전환
"""

import os
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

_ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
_OPENAI_KEY = os.getenv("OPENAI_API_KEY", "").strip()

if _ANTHROPIC_KEY:
    from anthropic import Anthropic
    _client = Anthropic(api_key=_ANTHROPIC_KEY)
    MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
    _BACKEND = "anthropic"
elif _OPENAI_KEY:
    from openai import OpenAI
    _client = OpenAI(api_key=_OPENAI_KEY)
    MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    _BACKEND = "openai"
else:
    raise RuntimeError("ANTHROPIC_API_KEY 또는 OPENAI_API_KEY 중 하나를 .env에 설정하세요.")


def _p(value) -> int:
    if not value:
        return 0
    try:
        return abs(int(float(str(value).replace(",", "").strip() or "0")))
    except (ValueError, TypeError):
        return 0


def _f(value) -> float:
    if not value:
        return 0.0
    try:
        return float(str(value).replace(",", "").strip() or "0")
    except (ValueError, TypeError):
        return 0.0


def _fmt_index(d: dict, name: str) -> str:
    price = _p(d.get("cur_prc") or d.get("prpr"))
    rate = d.get("flu_rt") or d.get("prdy_ctrt") or "N/A"
    sign = "▲" if str(rate).startswith("-") is False and rate != "N/A" else "▼"
    if price:
        return f"{name} {price:,}pt ({sign}{rate}%)"
    return f"{name} 조회 실패"


def _fmt_chart(signal) -> str:
    c = signal.chart
    if not c:
        return "차트 데이터 없음"

    # 5일 일봉 방향 화살표 (오래된 순 → 최신)
    trend_arrows = ""
    if c.recent_10d_prices and len(c.recent_10d_prices) >= 2:
        arrows = []
        for i in range(1, len(c.recent_10d_prices)):
            arrows.append("▲" if c.recent_10d_prices[i] > c.recent_10d_prices[i - 1] else "▼")
        # 마지막은 현재가와 비교
        arrows.append("▲" if signal.current_price > c.recent_10d_prices[-1] else "▼")
        prices_str = " → ".join(f"{p:,}" for p in c.recent_10d_prices)
        trend_arrows = f"{' '.join(arrows)}  ({prices_str} → {signal.current_price:,})"

    lines = [
        f"  - MA5: {int(c.ma5):,}원  /  MA20: {int(c.ma20):,}원" if c.ma5 and c.ma20 else "  - MA: 데이터 부족",
        f"  - 추세: {c.trend} (현재가 MA5 {'위' if c.above_ma5 else '아래'} / MA20 {'위' if c.above_ma20 else '아래'})" if c.above_ma5 is not None else "",
        f"  - 10일 흐름: {trend_arrows}" if trend_arrows else "",
        f"  - 5일 전 대비: {c.price_change_5d:+.2f}%" if c.price_change_5d is not None else "",
    ]
    return "\n".join(l for l in lines if l)


def _fmt_sector(sector: dict, sector_code: str | None) -> str:
    if not sector:
        return "섹터 데이터 없음"
    price = _p(sector.get("cur_prc") or sector.get("prpr"))
    rate = sector.get("flu_rt") or sector.get("prdy_ctrt") or "N/A"
    name = sector.get("upjong_nm") or sector_code or "해당 섹터"
    if price:
        return f"{name}: {price:,}pt (등락률 {rate}%)"
    return "섹터 조회 실패"


def _fmt_portfolio(holdings: list[dict], stock_code: str) -> tuple[str, str]:
    """DB 캐시 기반 (보유상세, 포트폴리오전체) 반환"""
    if not holdings:
        return "미보유 (DB 캐시 없음)", "동기화 필요"

    holding_detail = "미보유"
    lines = []
    total_eval = 0
    total_profit = 0

    for h in holdings:
        code = str(h.get("stock_code") or "")
        name = h.get("stock_name") or code
        qty = _p(h.get("quantity"))
        avg = _p(h.get("avg_price"))
        cur = _p(h.get("current_price"))
        rate = _f(h.get("profit_rate"))
        profit = _p(h.get("profit_loss"))
        eval_amt = _p(h.get("eval_amount"))

        if code == stock_code:
            holding_detail = f"{qty}주 보유 | 평균단가 {avg:,}원 | 수익률 {rate:+.2f}%"

        total_eval += eval_amt
        total_profit += h.get("profit_loss", 0) if isinstance(h.get("profit_loss"), int) else _p(h.get("profit_loss"))
        lines.append(f"  - {name}: {qty}주 | 평단 {avg:,}원 | 현재 {cur:,}원 | {rate:+.2f}%")

    if total_eval:
        total_cost = total_eval - total_profit
        rate_total = total_profit / total_cost * 100 if total_cost else 0
        lines.append(f"  ▶ 합계: 평가 {total_eval:,}원 | 손익 {total_profit:+,}원 ({rate_total:+.1f}%)")

    return holding_detail, "\n".join(lines) if lines else "보유 종목 없음"


def get_trade_opinion(
    signal,
    holdings: list[dict],
    kospi: dict,
    kosdaq: dict,
    sector: dict,
) -> str:
    holding_detail, portfolio_text = _fmt_portfolio(holdings, signal.stock_code)
    conditions_text = "\n".join(f"    - {c}" for c in signal.triggered_conditions)

    prompt = f"""당신은 개인 투자자의 주식 매매를 보조하는 퀀트 AI입니다.
아래 데이터를 종합 분석하여 매매 판단을 내려주세요.

## 1. 시장 현황
{_fmt_index(kospi, '코스피')}
{_fmt_index(kosdaq, '코스닥')}

## 2. 섹터 흐름
{_fmt_sector(sector, signal.sector_code)}

## 3. 신호 발생 종목: {signal.stock_name} ({signal.stock_code})
- 현재가: {signal.current_price:,}원
- RSI(14): {signal.rsi if signal.rsi else 'N/A'}
- 거래량 배율: {f'{signal.volume_ratio}배' if signal.volume_ratio else 'N/A'}
- 보유 현황: {holding_detail}

### 차트 분석
{_fmt_chart(signal)}

### 트리거된 조건
{conditions_text}

## 4. 포트폴리오 전체
{portfolio_text}

## 판단 요청
위 데이터를 바탕으로 [매수] / [매도] / [홀드] 중 하나를 판단하세요.
매수 또는 매도 판단 시, 시장가/지정가 중 더 유리한 주문 방식도 함께 추천하세요.

출력 형식 (반드시 준수):
[매수 or 매도 or 홀드]
• 근거1: (1~2문장)
• 근거2: (1~2문장)
• 근거3: (1~2문장)
[주문방식] 시장가 or 지정가 — 이유 한 문장 (홀드이면 이 줄 생략)

마크다운 헤더(#, ##) 사용 금지. 총 180단어 이내로 작성."""

    if _BACKEND == "anthropic":
        response = _client.messages.create(
            model=MODEL,
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text
    else:
        response = _client.chat.completions.create(
            model=MODEL,
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.choices[0].message.content
