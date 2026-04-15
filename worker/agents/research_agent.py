"""
Research Agent: 장 마감 후 유망 종목 발굴.
"""

from __future__ import annotations

import logging
import re
import json
from datetime import timedelta

from worker.agents.base_agent import BaseAgent
from worker.agents.tools.registry import load_research_tools
from worker.strategy_reflection import get_policy_snapshot, reflect_research, enqueue_policy_update
from worker.adaptive_policy import get_research_adaptive_policy

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
- 유사 사례가 필요하면 search_screening_context / search_text_context / search_similar_signals / search_agent_memory_context 활용

## 출력 형식
분석한 종목별로:
- 종목명(코드): 편입 여부
- 충족 조건: (조건명)
- 근거: (1~2문장)
- watchlist 등록: 완료 / 미등록 (사유)

마지막에 등록된 종목 수 요약."""


class ResearchAgent:
    """장 마감 후 유망 종목 발굴 Agent."""

    def __init__(
        self,
        max_steps: int = 10,
        max_tokens: int = 1200,
        target_unique_tools: int = 4,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
    ):
        tools = load_research_tools()
        self._agent = BaseAgent(
            tools=tools,
            system_prompt=_SYSTEM_PROMPT,
            model=model,
            api_key=api_key,
            base_url=base_url,
            max_steps=max_steps,
            max_tokens=max_tokens,
        )
        self._target_unique_tools = max(0, int(target_unique_tools or 0))
        self._preflight_used_tools: list[str] = []
        self._preflight_missing_fields: list[str] = []
        self._policy_version: str = "research-v0"
        self._adaptive_policy: dict = {}

    def _run_preflight(self, kospi_rate: float = 0.0, kosdaq_rate: float = 0.0, base_max_additions: int = 5) -> tuple[bool, dict, list[str]]:
        """리서치 실행 전 필수 컨텍스트 강제 수집/검증."""
        self._preflight_used_tools = []
        required = [
            "market_brief",
            "rss_macro_brief",
            "existing_exposure_check",
            "basic_disclosure_context",
            "screening_performance",
            "recent_review_note",
            "policy_snapshot",
            "adaptive_policy",
        ]
        ctx: dict = {}

        def _call(name: str, **kwargs):
            tool = self._agent._tool_map.get(name)
            self._preflight_used_tools.append(name)
            if not tool:
                return {"error": f"tool_not_found:{name}"}
            try:
                return tool.execute(**kwargs)
            except Exception as e:
                return {"error": str(e)}

        ctx["market_brief"] = _call("market_news_brief", max_items=5)
        ctx["rss_macro_brief"] = _call("rss_macro_brief", max_total=6)
        pf = _call("get_portfolio")

        holdings_codes = set()
        if isinstance(pf, dict):
            for h in (pf.get("holdings") or []):
                c = str(h.get("stock_code") or "").strip().lstrip("A")
                if c:
                    holdings_codes.add(c)

        watchlist_codes = set()
        try:
            from data.db import get_watchlist
            watchlist_codes = {str(w.get("code") or "").strip().lstrip("A") for w in get_watchlist()}
            watchlist_codes = {c for c in watchlist_codes if c}
        except Exception:
            watchlist_codes = set()

        overlaps = sorted(list(holdings_codes & watchlist_codes))
        ctx["existing_exposure_check"] = {
            "ok": True,
            "holdings_count": len(holdings_codes),
            "watchlist_count": len(watchlist_codes),
            "duplicate_codes": overlaps,
        }
        ctx["screening_performance"] = _call(
            "get_screening_history",
            days=60,
            limit=30,
            only_with_results=True,
        )
        try:
            from data.db import get_recent_daily_reviews
            reviews = get_recent_daily_reviews(limit=1) or []
            if reviews:
                r0 = reviews[0]
                ctx["recent_review_note"] = {
                    "created_at": r0.get("created_at", ""),
                    "summary": r0.get("summary", ""),
                    "detail": r0.get("detail", ""),
                }
            else:
                ctx["recent_review_note"] = {"error": "missing_recent_daily_review"}
        except Exception as e:
            ctx["recent_review_note"] = {"error": str(e)}

        proxy_code = ""
        if holdings_codes:
            proxy_code = next(iter(holdings_codes))
        elif watchlist_codes:
            proxy_code = next(iter(watchlist_codes))
        else:
            proxy_code = "005930"
        ctx["basic_disclosure_context"] = _call("get_dart", stock_code=proxy_code)
        policy_snapshot = get_policy_snapshot("research")
        self._policy_version = str(policy_snapshot.get("policy_version") or "research-v0")
        ctx["policy_snapshot"] = policy_snapshot
        adaptive = get_research_adaptive_policy(
            kospi_rate=float(kospi_rate or 0.0),
            kosdaq_rate=float(kosdaq_rate or 0.0),
            base_max_additions=int(base_max_additions or 5),
        )
        self._adaptive_policy = dict(adaptive or {})
        ctx["adaptive_policy"] = self._adaptive_policy

        validator = self._agent._tool_map.get("preflight_validator")
        if not validator:
            return False, ctx, ["preflight_validator"]
        validation = validator.execute(
            agent_type="research",
            required_fields=required,
            context=ctx,
        )
        missing = validation.get("missing_fields", []) if isinstance(validation, dict) else required
        ok = bool(isinstance(validation, dict) and validation.get("ok") and not missing)
        self._preflight_missing_fields = list(missing or [])
        return ok, ctx, self._preflight_missing_fields

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

    def _compact_result(self, text: str, max_lines: int = 12) -> str:
        """Keep only high-signal lines from free-form agent output."""
        raw = (text or "").replace("\r", "").strip()
        if not raw:
            return "- 결과 없음"

        lines = [ln.strip() for ln in raw.split("\n") if ln.strip()]
        keep: list[str] = []

        for ln in lines:
            norm = re.sub(r"^[#>*\-\s]+", "", ln).strip()
            if not norm:
                continue

            # Keep stock decision lines like "종목명(123456): ...", even when not numbered.
            if re.search(r"\([0-9]{6}\)\s*:", norm):
                keep.append(norm)
                continue

            if re.match(r"^\d+\.\s+", norm):
                keep.append(norm)
                continue

            if any(k in norm for k in ["watchlist", "등록", "미등록", "보류", "부적합", "편입", "요약", "완료"]):
                keep.append(norm)

        out: list[str] = []
        seen = set()
        for ln in keep:
            if ln in seen:
                continue
            seen.add(ln)
            out.append(ln)
            if len(out) >= max_lines:
                break

        if out:
            return "\n".join(out)

        fallback = [re.sub(r"\s+", " ", ln) for ln in lines[:max_lines]]
        return "\n".join(fallback)

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
        pre_ok, pre_ctx, missing = self._run_preflight(
            kospi_rate=kospi_rate,
            kosdaq_rate=kosdaq_rate,
            base_max_additions=max_candidates,
        )
        logger.info(
            f"[ResearchAgent] preflight_{'pass' if pre_ok else 'fail'} "
            f"missing_fields={missing} used_tools={self._preflight_used_tools} "
            f"policy_version={self._policy_version} adaptive={self._adaptive_policy}"
        )
        if not pre_ok:
            reflection_written = reflect_research(
                status="incomplete_context",
                result_text=f"INCOMPLETE_CONTEXT: missing_fields={','.join(missing)}",
                policy_version=self._policy_version,
            )
            enqueue_policy_update(
                "research",
                suggestion={"trigger": "incomplete_context", "missing_fields": missing},
                low_risk=True,
            )
            try:
                from data.db import save_strategy_note
                save_strategy_note(
                    "general",
                    "Research preflight fail",
                    (
                        "[REASSESS_REQUIRED] research preflight incomplete\n"
                        f"missing_fields={','.join(missing)}\n"
                        f"used_tools={','.join(self._preflight_used_tools)}"
                    ),
                )
            except Exception:
                pass
            logger.info(
                f"[ResearchAgent] reflection_written={reflection_written} "
                f"preflight_fail missing_fields={missing}"
            )
            return f"INCOMPLETE_CONTEXT: missing_fields={','.join(missing)}"

        limit = int((self._adaptive_policy or {}).get("max_additions") or max_candidates or 1)
        limit = max(1, limit)
        self._agent.configure_tool(
            "add_to_watchlist",
            max_additions=limit,
            addition_count=0,
            policy_gate_passed=True,
            policy_version=self._policy_version,
        )
        self._agent.configure_run(target_unique_tools=self._target_unique_tools)
        perf_summary = self._build_performance_summary()

        def _brief(v, limit=180):
            text = json.dumps(v, ensure_ascii=False, default=str)
            return text[:limit] + ("..." if len(text) > limit else "")

        initial_message = f"""## 오늘 시장 환경
- KOSPI: {kospi_rate:+.2f}%
- KOSDAQ: {kosdaq_rate:+.2f}%

## Preflight Context (validated)
- market_brief: {_brief(pre_ctx.get('market_brief', {}))}
- rss_macro_brief: {_brief(pre_ctx.get('rss_macro_brief', {}))}
- existing_exposure_check: {_brief(pre_ctx.get('existing_exposure_check', {}))}
- basic_disclosure_context: {_brief(pre_ctx.get('basic_disclosure_context', {}))}
- screening_performance: {_brief(pre_ctx.get('screening_performance', {}))}
- recent_review_note: {_brief(pre_ctx.get('recent_review_note', {}))}
- policy_snapshot: {_brief(pre_ctx.get('policy_snapshot', {}))}
- adaptive_policy: {_brief(pre_ctx.get('adaptive_policy', {}))}

## 최근 성과 요약 (고정 반영 규칙)
아래 요약은 참고가 아니라 필수 반영 대상입니다. 추천 판단 시 반드시 우선 반영하세요.
{perf_summary}

거래량 급증, 외인 순매수, 하락 종목 스캔을 수행하고,
편입 조건 2가지 이상 충족하는 종목을 최대 {limit}개 분석하여
적합한 종목을 관심종목에 등록하세요.
add_to_watchlist 호출은 최대 {limit}회까지만 허용됩니다.


Output format constraints (keep concise):
- Maximum 12 lines total.
- For each selected stock, output only: "<stock>(<code>): decision" / "reason" / "watchlist: done|not added(reason)".
- No long background explanation, no duplicated wording.
Tool coverage constraints:
- Use at least {self._target_unique_tools} distinct tools before final answer, unless hard failures occur.
- Include at least one memory/history tool among: search_agent_memory_context, search_screening_context, search_text_context, search_similar_signals, get_screening_history.
- If scan APIs fail, compensate with history/memory/performance tools instead of stopping early.
- Fallback chain when scan fails: search_agent_memory_context -> get_screening_history -> get_trade_performance -> search_text_context.
이미 watchlist에 있는 종목은 건너뛰어도 됩니다."""

        result = self._agent.run(initial_message)
        result = self._compact_result(result)
        reflection_written = reflect_research(
            status="completed",
            result_text=result,
            policy_version=self._policy_version,
        )
        enqueue_policy_update(
            "research",
            suggestion={
                "trigger": "post_run_reflection",
                "policy_version": self._policy_version,
                "result_preview": result[:180],
            },
            low_risk=True,
        )

        tools_summary = " -> ".join(self._agent._used_tools) if self._agent._used_tools else "none"
        logger.info(f"[ResearchAgent] 분석 과정: {tools_summary}")
        logger.info(f"[ResearchAgent] tool_coverage_score: {self._agent.coverage_score}")
        logger.info(
            f"[ResearchAgent] reflection_written={reflection_written} "
            f"policy_version={self._policy_version}"
        )

        return result

    @property
    def used_tools(self) -> list[str]:
        return list(self._agent._used_tools)

    @property
    def reasoning_chain(self) -> list[str]:
        return list(self._agent._reasoning_steps)

    @property
    def addition_count(self) -> int:
        tool = self._agent._tool_map.get("add_to_watchlist")
        if not tool:
            return 0
        return int(getattr(tool, "addition_count", 0) or 0)

    @property
    def coverage_score(self) -> str:
        return self._agent.coverage_score

