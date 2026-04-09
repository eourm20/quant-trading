"""
Judgment Agent — 신호 수신 시 AI가 도구를 스스로 호출하여 매수/매도/홀드 판단.

기존 claude_judge.get_trade_opinion()의 Agent 버전.
"""

from __future__ import annotations
import logging

from worker.agents.base_agent import BaseAgent
from worker.agents.tools.registry import load_judgment_tools

logger = logging.getLogger(__name__)

# 기존 claude_judge에서 _TRADING_KNOWLEDGE를 재사용
def _get_trading_knowledge() -> str:
    try:
        from worker.claude_judge import _TRADING_KNOWLEDGE
        return _TRADING_KNOWLEDGE
    except Exception:
        return ""


_SYSTEM_PROMPT = f"""당신은 개인 투자자의 퀀트 트레이딩 시스템에서 최종 매매 판단을 내리는 AI입니다.
신호 정보를 받으면 도구를 직접 호출하여 필요한 데이터를 수집한 후 판단하세요.

{_get_trading_knowledge()}

## 도구 사용 가이드
- get_chart: 차트·지표 분석 (RSI, MA, 볼린저, 스토캐스틱 등) — 반드시 호출
- get_deposit + get_portfolio: 현금 비중, 보유 현황 — entry/add 신호 시 필수
- get_positions: 목표가/손절가/추가매수가 — 보유 종목(exit/add) 시 필수
- get_dart / get_news: 공시·뉴스 — 판단에 영향 가능성 있을 때 호출
- get_macro_news: 거시경제·지정학 뉴스 (관세·금리·전쟁 등) — 시장 전반 충격이 의심될 때 호출
- get_sector_news: 업종 업황 뉴스 — 섹터 리스크/호재 파악 시 호출 (sector_name: 업종명 입력)
- get_global_market: 나스닥·S&P500·달러원 — 해외 시장 영향 판단 시 호출
- get_signal_history: 과거 AI 판단 이력 — 일관성 확인에 활용
- get_market_index: 코스피·코스닥·섹터 지수 — 필요 시 호출
- update_watchlist: 임계값 변경 — 구조적 오류 확인 시에만
- execute_order: 주문 실행 — 사용자가 자동 실행 모드로 요청한 경우에만

## 운용 원칙 (변경 불가)
- 물타기 최대 1회 원칙 (averaging_down add 신호)
- momentum_add는 현재가 > 평단일 때만 유효
- 손절가 도달 시 즉시 매도 원칙
- 현금 비중 5% 미만이면 신규 매수 보류
- 포트 전체 수익률 -10% 이하: [홀드] 우선
- 추천수량은 실질 매수 여력(현금 - 물타기 예비금) 이내

## 출력 형식 (반드시 준수)
[매수 or 추가매수(매수) or 물타기(매수) or 매도 or 홀드]
• 근거1: (필수, 1문장)
• 근거2: (선택)
• 근거3: (선택)
[주문시장] KRX or NXT or SOR (홀드이면 생략)
[주문방식] 시장가 or 지정가 (홀드이면 생략)
[추천수량] N주 (약 XXX만원) (홀드이면 생략)
[전환조건] 홀드 시 매수/매도 전환 트리거 명시 (홀드가 아니면 생략)
[임계값] 변경 불필요하면 반드시 생략 (field=value 형식)

마크다운 헤더(#, ##) 사용 금지. 총 250단어 이내."""


class JudgmentAgent:
    """신호 1건에 대한 매매 판단 Agent."""

    def __init__(self, max_steps: int = 10):
        tools = load_judgment_tools()
        self._agent = BaseAgent(
            tools=tools,
            system_prompt=_SYSTEM_PROMPT,
            max_steps=max_steps,
            max_tokens=1024,
        )

    def run(self, signal) -> str:
        """
        signal: worker.monitor.Signal 인스턴스
        반환: claude_judge.get_trade_opinion()과 동일한 형식의 문자열
        """
        signal_type_label = {
            "entry": "entry — 신규 매수 타이밍 검토",
            "exit":  "exit  — 매도·익절·손절 타이밍 검토",
            "add":   "add   — 보유 중 추가매수/물타기 검토",
            "both":  "both  — 매수/매도 모두 해당",
        }.get(signal.signal_type, signal.signal_type)

        add_mode = getattr(signal, "add_signal_mode", "")
        if signal.signal_type == "add" and add_mode:
            if add_mode == "momentum_add":
                signal_type_label = "add — 보유 중 반등 확인 후 추가매수(momentum_add) 검토"
            else:
                signal_type_label = "add — 보유 중 평단 하회 물타기(averaging_down) 검토"

        conditions_text = "\n".join(f"  - {c}" for c in signal.triggered_conditions)
        horizon = getattr(signal, "horizon", "") or ""

        initial_message = f"""## 신호 정보
- 종목: {signal.stock_name} ({signal.stock_code})
- 신호 유형: {signal_type_label}
- 트리거 조건:
{conditions_text}
- 현재가: {signal.current_price:,}원
- RSI(현재 계산값): {signal.rsi if signal.rsi else 'N/A'}
- 거래량 배율: {f'{signal.volume_ratio}배' if signal.volume_ratio else 'N/A'}
- 매매 기간(horizon): {horizon or '미설정'}
- 보유 여부: {'보유 중' if signal.in_portfolio else '미보유'}

도구를 호출하여 필요한 데이터를 수집한 후 매매 판단을 내려주세요.
필수 조회: get_chart (차트·지표), get_portfolio + get_deposit (잔고·예수금)
권장 조회: get_global_market (글로벌 지수), get_macro_news (거시경제 이슈) — 시장 변동성이 큰 경우
섹터 이슈 의심 시: get_sector_news에 업종명을 입력하여 업황 확인"""

        opinion = self._agent.run(initial_message)

        # 사용 도구 목록을 로그 + 반환값 끝에 부록으로 첨부
        if self._agent._used_tools:
            tools_summary = " → ".join(self._agent._used_tools)
            logger.info(f"[JudgmentAgent] {signal.stock_name} 분석 과정: {tools_summary}")

        return opinion

    @property
    def used_tools(self) -> list[str]:
        return list(self._agent._used_tools)
