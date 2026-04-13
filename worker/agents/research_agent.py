"""
Research Agent — 장 마감 후 유망 종목 자동 발굴.
기존 worker/stock_analyzer.py의 AI 분석 파트를 Agent로 대체.
"""

from __future__ import annotations
import logging

from worker.agents.base_agent import BaseAgent
from worker.agents.tools.registry import load_research_tools

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """당신은 개인 투자자의 퀀트 트레이딩 시스템에서 유망 종목을 발굴하는 AI입니다.
시장 상황에 맞는 도구를 스스로 선택해 후보를 찾고, 검증된 종목만 관심종목에 등록하세요.

## 편입 원칙 (5가지 중 2가지 이상 충족)
1. 눌림목: 전고점 대비 -10~-20% 조정 후 지지선 근처
2. 저평가: PER/PBR 동종업계 대비 낮음
3. 테마미반영: 섹터 테마 대비 주가 덜 오름
4. 실적개선: 최근 실적 서프라이즈 또는 상향 전망
5. 잠재성장: 외인 순매수 지속 + 거래량 증가 + 주가 횡보 (축적 단계)

## 금지 사항
- 단순히 주가가 많이 올랐다는 이유만으로 편입
- 급등 종목 (+5% 초과) 편입
- 섹터 악재 동반 종목 편입

## 판단 방식
- 도구 사용 순서는 고정하지 말고 상황에 맞게 자율적으로 선택할 것
- 등록 여부는 실제 확인한 데이터에만 근거할 것
- 애매하면 등록하지 말고 보류 또는 미등록으로 남길 것

## 출력 형식
분석한 종목별로:
- 종목명(코드): 편입 여부
- 충족 조건: (조건명 나열)
- 근거: (1~2문장)
- watchlist 등록: 완료 / 미등록 (사유)

마지막에 등록된 종목 수 요약."""


class ResearchAgent:
    """장 마감 후 유망 종목 발굴 Agent."""

    def __init__(self, max_steps: int = 15):
        tools = load_research_tools()
        self._agent = BaseAgent(
            tools=tools,
            system_prompt=_SYSTEM_PROMPT,
            max_steps=max_steps,
            max_tokens=2048,
        )

    def run(
        self,
        kospi_rate: float = 0.0,
        kosdaq_rate: float = 0.0,
        max_candidates: int = 5,
    ) -> str:
        """
        kospi_rate, kosdaq_rate: 당일 지수 등락률
        max_candidates: 최대 분석 후보 수
        반환: 분석 결과 요약 문자열
        """
        limit = max(1, int(max_candidates or 1))
        self._agent.configure_tool("add_to_watchlist", max_additions=limit, addition_count=0)

        initial_message = f"""## 오늘 시장 환경
- KOSPI: {kospi_rate:+.2f}%
- KOSDAQ: {kosdaq_rate:+.2f}%

거래량 급증, 외인 순매수, 하락 종목 스캔을 수행하고,
편입 조건 2가지 이상 충족하는 종목을 최대 {limit}개 분석하여
적합한 종목을 관심종목에 등록하세요.
add_to_watchlist 호출은 최대 {limit}회까지만 허용됩니다.

이미 watchlist에 있는 종목은 건너뛰어도 됩니다."""

        result = self._agent.run(initial_message)

        if self._agent._used_tools:
            tools_summary = " → ".join(self._agent._used_tools)
            logger.info(f"[ResearchAgent] 분석 과정: {tools_summary}")

        return result

    @property
    def used_tools(self) -> list[str]:
        return list(self._agent._used_tools)
