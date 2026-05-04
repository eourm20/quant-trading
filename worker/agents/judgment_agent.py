"""
Judgment Agent — 신호 수신 시 AI가 도구를 스스로 호출하여 매수/매도/홀드 판단.

기존 claude_judge.get_trade_opinion()의 Agent 버전.
"""

from __future__ import annotations
import logging
import json
import re

from worker.agents.base_agent import BaseAgent
from worker.agents.tools.registry import load_judgment_tools
from worker.strategy_reflection import get_policy_snapshot, reflect_judgment, enqueue_policy_update
from worker.adaptive_policy import get_judgment_adaptive_policy
from data.db import get_conn

logger = logging.getLogger(__name__)

# 기존 claude_judge에서 _TRADING_KNOWLEDGE를 재사용
def _get_trading_knowledge() -> str:
    try:
        from worker.claude_judge import _TRADING_KNOWLEDGE_ACTIVE
        return _TRADING_KNOWLEDGE_ACTIVE
    except Exception:
        return ""


_SYSTEM_PROMPT = f"""당신은 개인 투자자의 퀀트 트레이딩 시스템에서 최종 매매 판단을 내리는 AI입니다.
신호 정보를 받으면 필요한 도구를 스스로 선택해 사실을 확인한 뒤, 과감하되 규율 있게 판단하세요.

{_get_trading_knowledge()}

## 운용 원칙 (절대 준수)
- 물타기 최대 1회 원칙 (averaging_down add 신호)
- momentum_add는 현재가 > 평단일 때만 유효
- 손절가 도달 시 즉시 매도 원칙
- 약한 exit 신호(과매수 단독/거래량 미동반)는 전량매도 금지, 부분매도(30~50%) 또는 홀드 우선
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

    def __init__(
        self,
        max_steps: int = 7,
        max_tokens: int = 700,
        target_unique_tools: int = 0,
        context_char_limit: int = 200,
        history_limit: int = 5,
        news_limit: int = 5,
        macro_limit: int = 6,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
    ):
        tools = load_judgment_tools()
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
        self._context_char_limit = max(80, int(context_char_limit or 200))
        self._history_limit = max(1, int(history_limit or 5))
        self._news_limit = max(1, int(news_limit or 5))
        self._macro_limit = max(1, int(macro_limit or 6))
        self._preflight_used_tools: list[str] = []
        self._preflight_missing_fields: list[str] = []
        self._policy_version: str = "judgment-v0"
        self._adaptive_policy: dict = {}

    def _run_preflight(self, signal, kospi_rate: float = 0.0, kosdaq_rate: float = 0.0) -> tuple[bool, dict, list[str]]:
        """필수 컨텍스트를 코드 파이프라인으로 강제 수집/검증."""
        self._preflight_used_tools = []
        required = [
            "position_context",
            "previous_judgment",
            "performance_review",
            "market_brief",
            "stock_news",
            "macro_brief",
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

        ctx["position_context"] = _call(
            "position_context_brief",
            stock_code=signal.stock_code,
            stock_name=signal.stock_name,
            notes_limit=5,
        )
        ctx["previous_judgment"] = _call(
            "get_signal_history",
            stock_code=signal.stock_code,
            signal_type=signal.signal_type or "",
            limit=self._history_limit,
        )
        ctx["performance_review"] = _call(
            "get_trade_performance",
            stock_code=signal.stock_code,
            days=60,
            limit=20,
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
                    "source": "daily_review",
                }
            else:
                # Optional context: do not fail preflight when daily review is not ready yet.
                ctx["recent_review_note"] = {
                    "optional_missing": True,
                    "reason": "missing_recent_daily_review",
                }
        except Exception as e:
            # Keep as optional hint; never escalate to preflight error.
            ctx["recent_review_note"] = {
                "optional_missing": True,
                "reason": f"daily_review_fetch_error:{e}",
            }
        ctx["market_brief"] = _call("market_news_brief", max_items=self._news_limit)
        ctx["stock_news"] = _call("get_news", stock_name=signal.stock_name, max_items=self._news_limit)
        ctx["macro_brief"] = _call("rss_macro_brief", max_total=self._macro_limit)
        policy_snapshot = get_policy_snapshot("judgment")
        self._policy_version = str(policy_snapshot.get("policy_version") or "judgment-v0")
        ctx["policy_snapshot"] = policy_snapshot
        adaptive = get_judgment_adaptive_policy(
            signal_type=str(getattr(signal, "signal_type", "") or ""),
            kospi_rate=float(kospi_rate or 0.0),
            kosdaq_rate=float(kosdaq_rate or 0.0),
            trigger_count=len(getattr(signal, "triggered_conditions", []) or []),
        ).as_dict()
        self._adaptive_policy = adaptive
        ctx["adaptive_policy"] = adaptive

        validator = self._agent._tool_map.get("preflight_validator")
        if not validator:
            return False, ctx, ["preflight_validator"]
        validation = validator.execute(
            agent_type="judgment",
            required_fields=required,
            context=ctx,
        )
        missing = validation.get("missing_fields", []) if isinstance(validation, dict) else required
        ok = bool(isinstance(validation, dict) and validation.get("ok") and not missing)
        self._preflight_missing_fields = list(missing or [])
        return ok, ctx, self._preflight_missing_fields

    def run(self, signal, kospi_rate: float = 0.0, kosdaq_rate: float = 0.0) -> str:
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

        pre_ok, pre_ctx, missing = self._run_preflight(signal, kospi_rate=kospi_rate, kosdaq_rate=kosdaq_rate)
        logger.info(
            f"[JudgmentAgent] preflight_{'pass' if pre_ok else 'fail'} "
            f"stock={signal.stock_name}({signal.stock_code}) "
            f"missing_fields={missing} used_tools={self._preflight_used_tools} "
            f"policy_version={self._policy_version} adaptive={self._adaptive_policy}"
        )
        if not pre_ok:
            reflection_written = reflect_judgment(
                stock_code=signal.stock_code,
                stock_name=signal.stock_name,
                opinion="INCOMPLETE_CONTEXT",
                preflight_ok=False,
                missing_fields=missing,
                policy_version=self._policy_version,
            )
            enqueue_policy_update(
                "judgment",
                suggestion={"trigger": "incomplete_context", "missing_fields": missing},
                low_risk=True,
            )
            try:
                from data.db import save_strategy_note
                save_strategy_note(
                    "watchlist",
                    f"{signal.stock_name} preflight fail",
                    (
                        "[REASSESS_REQUIRED] judgment preflight incomplete\n"
                        f"stock_code={signal.stock_code}\n"
                        f"missing_fields={','.join(missing)}\n"
                        f"used_tools={','.join(self._preflight_used_tools)}"
                    ),
                )
            except Exception:
                pass
            logger.info(
                f"[JudgmentAgent] reflection_written={reflection_written} "
                f"preflight_fail missing_fields={missing}"
            )
            return f"INCOMPLETE_CONTEXT: missing_fields={','.join(missing)}"

        self._agent.configure_run(target_unique_tools=self._target_unique_tools)

        def _brief(v, limit=None):
            if limit is None:
                limit = self._context_char_limit
            text = json.dumps(v, ensure_ascii=False, default=str)
            return text[:limit] + ("..." if len(text) > limit else "")

        def _extract_last_hold_transition_condition(stock_code: str) -> str:
            if not stock_code:
                return ""
            try:
                with get_conn() as conn:
                    row = conn.execute(
                        """
                        SELECT claude_opinion
                        FROM signals
                        WHERE stock_code = ?
                          AND claude_opinion IS NOT NULL
                          AND claude_opinion LIKE '%[홀드]%'
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        (stock_code,),
                    ).fetchone()
                if not row:
                    return ""
                opinion = str(row["claude_opinion"] or "")
                for line in opinion.splitlines():
                    txt = line.strip()
                    if txt.startswith("[전환조건]"):
                        return txt[len("[전환조건]"):].strip()
            except Exception:
                return ""
            return ""

        def _evaluate_transition_with_available_metrics(condition_text: str) -> tuple[str, list[str], list[str]]:
            if not condition_text:
                return "none", [], []
            hits: list[str] = []
            misses: list[str] = []
            unknown: list[str] = []

            try:
                cur_rsi = float(signal.rsi) if signal.rsi is not None else None
            except Exception:
                cur_rsi = None
            try:
                cur_vol = float(signal.volume_ratio) if signal.volume_ratio is not None else None
            except Exception:
                cur_vol = None

            matched_any = False
            for m in re.finditer(r"RSI\s*(\d+(?:\.\d+)?)\s*(이상|이하)", condition_text):
                matched_any = True
                v = float(m.group(1))
                op = m.group(2)
                if cur_rsi is None:
                    unknown.append(f"RSI {v:g}{op} (현재 RSI 없음)")
                elif op == "이상" and cur_rsi >= v:
                    hits.append(f"RSI {cur_rsi:.2f} >= {v:g}")
                elif op == "이하" and cur_rsi <= v:
                    hits.append(f"RSI {cur_rsi:.2f} <= {v:g}")
                else:
                    misses.append(f"RSI {cur_rsi:.2f}, 조건 {v:g}{op}")

            for m in re.finditer(r"거래량(?:\s*배율)?\s*(\d+(?:\.\d+)?)\s*배\s*이상", condition_text):
                matched_any = True
                v = float(m.group(1))
                if cur_vol is None:
                    unknown.append(f"거래량 {v:g}배 이상 (현재 거래량 배율 없음)")
                elif cur_vol >= v:
                    hits.append(f"거래량 {cur_vol:.2f}배 >= {v:g}배")
                else:
                    misses.append(f"거래량 {cur_vol:.2f}배, 조건 {v:g}배 이상")

            if not matched_any:
                unknown.append("정형 평가 가능한 RSI/거래량 규칙 미검출")

            if hits and not misses:
                status = "met_or_partially_met"
            elif misses:
                status = "not_met"
            else:
                status = "unknown"
            return status, hits, (misses + unknown)

        prev_transition = _extract_last_hold_transition_condition(getattr(signal, "stock_code", "") or "")
        transition_status, transition_hits, transition_gaps = _evaluate_transition_with_available_metrics(prev_transition)

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

## Preflight Context (validated)
- position_context: {_brief(pre_ctx.get('position_context', {}))}
- previous_judgment: {_brief(pre_ctx.get('previous_judgment', {}))}
- market_brief: {_brief(pre_ctx.get('market_brief', {}))}
- stock_news: {_brief(pre_ctx.get('stock_news', {}))}
- macro_brief: {_brief(pre_ctx.get('macro_brief', {}))}
- performance_review: {_brief(pre_ctx.get('performance_review', {}))}
- recent_review_note: {_brief(pre_ctx.get('recent_review_note', {}))}
- policy_snapshot: {_brief(pre_ctx.get('policy_snapshot', {}))}
- adaptive_policy: {_brief(pre_ctx.get('adaptive_policy', {}))}

## 이전 홀드 전환조건 컨텍스트
- previous_hold_transition_condition: {prev_transition or '없음'}
- transition_check_status: {transition_status}
- satisfied_signals: {', '.join(transition_hits) if transition_hits else '없음'}
- unsatisfied_or_unverified: {', '.join(transition_gaps) if transition_gaps else '없음'}

상황을 파악하고 필요한 도구를 직접 선택하여 매매 판단을 내려주세요.
Fallback chain if primary tool fails: search_agent_memory_context -> search_similar_signals -> get_trade_performance -> search_text_context."""

        opinion = self._agent.run(initial_message)
        reflection_written = reflect_judgment(
            stock_code=signal.stock_code,
            stock_name=signal.stock_name,
            opinion=opinion,
            preflight_ok=True,
            missing_fields=[],
            policy_version=self._policy_version,
        )
        enqueue_policy_update(
            "judgment",
            suggestion={
                "trigger": "post_run_reflection",
                "stock_code": signal.stock_code,
                "signal_type": signal.signal_type or "",
                "policy_version": self._policy_version,
            },
            low_risk=True,
        )
        tools_summary = " -> ".join(self._agent._used_tools) if self._agent._used_tools else "none"
        logger.info(f"[JudgmentAgent] {signal.stock_name} analysis flow: {tools_summary}")
        logger.info(f"[JudgmentAgent] tool_coverage_score: {self._agent.coverage_score}")
        logger.info(
            f"[JudgmentAgent] reflection_written={reflection_written} "
            f"policy_version={self._policy_version}"
        )
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

    @property
    def policy_version(self) -> str:
        return self._policy_version

