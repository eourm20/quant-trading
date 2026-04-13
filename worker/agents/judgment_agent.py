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
신호 정보를 받으면 필요한 도구를 스스로 선택해 사실을 확인한 뒤, 과감하되 규율 있게 판단하세요.

{_get_trading_knowledge()}

## 운용 원칙 (절대 준수)
- 물타기 최대 1회 원칙 (averaging_down add 신호)
- momentum_add는 현재가 > 평단일 때만 유효
- 손절가 도달 시 즉시 매도 원칙
- 현금 비중 5% 미만이면 신규 매수 보류
- 포트 전체 수익률 -10% 이하: [홀드] 우선
- 추천수량은 실질 매수 여력(현금 - 물타기 예비금) 이내

## 판단 방식
- 도구 사용 순서는 고정하지 말고 상황에 맞게 자율적으로 선택할 것
- 확신이 부족하면 성급한 매수/매도보다 [홀드]를 우선할 것
- 근거는 실제로 확인한 데이터에만 기반할 것

## 출력 형식 (반드시 준수)
[매수 or 추가매수(매수) or 물타기(매수) or 매도 or 홀드]
• 근거1: (필수, 1문장)
• 근거2: (선택)
• 근거3: (선택)
[주문시장] KRX or NXT or SOR (홀드이면 생략)
[주문방식] 시장가 or 지정가 (홀드이면 생략. 원칙: 손절/긴급 매도만 시장가, 나머지는 지정가(현재가))
[추천수량] N주 (약 XXX만원) (홀드이면 생략. 매도이면 보유 전량 또는 부분 매도 수량 명시 필수)
[전환조건] 홀드 시 매수/매도 전환 트리거 명시 (홀드가 아니면 생략)
[임계값] 변경 불필요하면 반드시 생략 (field=value 형식)

마크다운 헤더(#, ##) 사용 금지. 총 250단어 이내."""


class JudgmentAgent:
    """신호 1건에 대한 매매 판단 Agent."""

    def __init__(self, max_steps: int = 15):
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
        is_holding = signal.in_portfolio
        signal_type = signal.signal_type

        self._agent.configure_run(target_unique_tools=5)

        initial_message = f"""## 신호 정보
- 종목: {signal.stock_name} ({signal.stock_code})
- 신호 유형: {signal_type_label}
- 트리거 조건:
{conditions_text}
- 현재가: {signal.current_price:,}원
- RSI(현재 계산값): {signal.rsi if signal.rsi else 'N/A'}
- 거래량 배율: {f'{signal.volume_ratio}배' if signal.volume_ratio else 'N/A'}
- 매매 기간(horizon): {horizon or '미설정'}
- 보유 여부: {'보유 중' if is_holding else '미보유'}

상황을 파악하고 필요한 도구를 직접 선택하여 매매 판단을 내려주세요.
Fallback chain if primary tool fails: search_agent_memory_context -> search_similar_signals -> get_trade_performance -> search_text_context."""

        opinion = self._agent.run(initial_message)
        tools_summary = " -> ".join(self._agent._used_tools) if self._agent._used_tools else "none"
        logger.info(f"[JudgmentAgent] {signal.stock_name} analysis flow: {tools_summary}")
        logger.info(f"[JudgmentAgent] tool_coverage_score: {self._agent.coverage_score}")
        return opinion

    @property
    def used_tools(self) -> list[str]:
        return list(self._agent._used_tools)

    @property
    def reasoning_chain(self) -> list[str]:
        return list(self._agent._reasoning_steps)

    @property
    def coverage_score(self) -> str:
        return self._agent.coverage_score

