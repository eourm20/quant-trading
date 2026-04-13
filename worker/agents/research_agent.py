"""
Research Agent: 장 마감 후 유망 종목 발굴.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from worker.agents.base_agent import BaseAgent
from worker.agents.tools.registry import load_research_tools

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """당신은 개인 투자자의 퀀트 트레이딩 시스템에서 유망 종목을 발굴하는 AI입니다.
시장 상황에 맞는 도구를 스스로 선택해 후보를 찾고, 검증된 종목만 관심종목에 등록하세요.

## 편입 원칙 (5가지 중 2가지 이상 충족)
1. 눌림목: 전고점 대비 -10~-20% 조정 후 지지선 근처
2. 저평가: PER/PBR 동종업계 대비 낮음
3. 테마미반영: 섹터 테마 대비 주가 덜 상승
4. 실적개선: 최근 실적 서프라이즈 또는 상향 전망
5. 잠재성장: 외인 순매수 지속 + 거래량 증가 + 주가 횡보(축적 단계)

## 금지 사항
- 단순 급등 추격 편입
- 섹터 악재 동반 종목 편입

## 판단 방식
- 도구 사용 순서는 고정하지 말고 상황에 맞게 선택
- 등록 여부는 확인된 데이터 기반으로만 판단
- 애매하면 보류 또는 미등록
- 유사 사례가 필요하면 search_screening_context / search_text_context / search_similar_signals 활용

## 출력 형식
분석한 종목별로:
- 종목명(코드): 편입 여부
- 충족 조건: (조건명)
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

    def _build_performance_summary(self) -> str:
        """성과 요약을 고정 입력으로 주입하기 위한 텍스트."""
        try:
            from data.db import get_conn, _now_kst

            now = _now_kst()
            since_30 = (now - timedelta(days=30)).strftime("%Y-%m-%d")

            with get_conn() as conn:
                s_row = conn.execute(
                    """
                    SELECT
                      COUNT(*) AS total,
                      AVG(result_7d) AS avg_7d,
                      AVG(result_30d) AS avg_30d,
                      SUM(CASE WHEN result_7d > 0 THEN 1 ELSE 0 END) AS pos_7d,
                      SUM(CASE WHEN result_7d IS NOT NULL THEN 1 ELSE 0 END) AS cnt_7d,
                      SUM(CASE WHEN result_30d > 0 THEN 1 ELSE 0 END) AS pos_30d,
                      SUM(CASE WHEN result_30d IS NOT NULL THEN 1 ELSE 0 END) AS cnt_30d
                    FROM screening_log
                    WHERE created_at >= ?
                    """,
                    (since_30,),
                ).fetchone()

                t_row = conn.execute(
                    """
                    SELECT
                      COUNT(*) AS total,
                      AVG(result_1d) AS avg_1d,
                      AVG(result_3d) AS avg_3d,
                      AVG(result_5d) AS avg_5d,
                      SUM(CASE WHEN result_3d > 0 THEN 1 ELSE 0 END) AS pos_3d,
                      SUM(CASE WHEN result_3d IS NOT NULL THEN 1 ELSE 0 END) AS cnt_3d
                    FROM trades
                    WHERE executed_at >= ?
                    """,
                    (since_30,),
                ).fetchone()

            def _pct(pos: int, cnt: int):
                return round((pos / cnt) * 100, 1) if cnt else None

            s_hit_7d = _pct(int(s_row["pos_7d"] or 0), int(s_row["cnt_7d"] or 0))
            s_hit_30d = _pct(int(s_row["pos_30d"] or 0), int(s_row["cnt_30d"] or 0))
            t_hit_3d = _pct(int(t_row["pos_3d"] or 0), int(t_row["cnt_3d"] or 0))

            return "\n".join(
                [
                    (
                        f"- Screening(30d): total={int(s_row['total'] or 0)}, "
                        f"hit7d={s_hit_7d if s_hit_7d is not None else 'N/A'}%, "
                        f"hit30d={s_hit_30d if s_hit_30d is not None else 'N/A'}%, "
                        f"avg7d={round(float(s_row['avg_7d']), 2) if s_row['avg_7d'] is not None else 'N/A'}%, "
                        f"avg30d={round(float(s_row['avg_30d']), 2) if s_row['avg_30d'] is not None else 'N/A'}%"
                    ),
                    (
                        f"- Trades(30d): total={int(t_row['total'] or 0)}, "
                        f"hit3d={t_hit_3d if t_hit_3d is not None else 'N/A'}%, "
                        f"avg1d={round(float(t_row['avg_1d']), 2) if t_row['avg_1d'] is not None else 'N/A'}%, "
                        f"avg3d={round(float(t_row['avg_3d']), 2) if t_row['avg_3d'] is not None else 'N/A'}%, "
                        f"avg5d={round(float(t_row['avg_5d']), 2) if t_row['avg_5d'] is not None else 'N/A'}%"
                    ),
                ]
            )
        except Exception as e:
            logger.warning(f"[ResearchAgent] 성과 요약 생성 실패: {e}")
            return "- 성과 요약 생성 실패"

    def run(
        self,
        kospi_rate: float = 0.0,
        kosdaq_rate: float = 0.0,
        max_candidates: int = 5,
    ) -> str:
        """
        kospi_rate, kosdaq_rate: 당일 지수 등락률
        max_candidates: 최대 분석 후보 수
        """
        limit = max(1, int(max_candidates or 1))
        self._agent.configure_tool("add_to_watchlist", max_additions=limit, addition_count=0)
        perf_summary = self._build_performance_summary()

        initial_message = f"""## 오늘 시장 환경
- KOSPI: {kospi_rate:+.2f}%
- KOSDAQ: {kosdaq_rate:+.2f}%

## 최근 성과 요약 (고정 반영 규칙)
아래 요약은 참고가 아니라 필수 반영 대상입니다. 추천 판단 시 반드시 우선 반영하세요.
{perf_summary}

거래량 급증, 외인 순매수, 하락 종목 스캔을 수행하고,
편입 조건 2가지 이상 충족하는 종목을 최대 {limit}개 분석하여
적합한 종목을 관심종목에 등록하세요.
add_to_watchlist 호출은 최대 {limit}회까지만 허용됩니다.

이미 watchlist에 있는 종목은 건너뛰어도 됩니다."""

        result = self._agent.run(initial_message)

        if self._agent._used_tools:
            tools_summary = " -> ".join(self._agent._used_tools)
            logger.info(f"[ResearchAgent] 분석 과정: {tools_summary}")

        return result

    @property
    def used_tools(self) -> list[str]:
        return list(self._agent._used_tools)

