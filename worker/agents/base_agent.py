"""
BaseAgent — Tool Use 루프 공통 구현 (OpenAI Function Calling 기반).

흐름:
  1. 초기 메시지 전달
  2. GPT가 tool_calls 반환 → 도구 실행 → 결과 재전달
  3. finish_reason == "stop" → 최종 텍스트 반환
  4. max_steps 초과 시 표준 verdict 포맷의 "[홀드]" 반환
"""

from __future__ import annotations
import json
import logging
import os
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '..', '.env'))

logger = logging.getLogger(__name__)

_OPENAI_KEY = os.getenv("OPENAI_API_KEY", "").strip()
_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1")

from openai import OpenAI


class BaseAgent:
    """OpenAI Function Calling 기반 Tool Use 루프."""

    def __init__(
        self,
        tools: list,
        system_prompt: str,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        max_steps: int = 10,
        max_tokens: int = 1024,
    ):
        from worker.agents.tools.registry import build_schema
        resolved_api_key = (api_key or _OPENAI_KEY or "").strip()
        if not resolved_api_key:
            raise RuntimeError(
                "Agent 모드 API 키가 없습니다. OPENAI_API_KEY 또는 worker.yaml의 에이전트 API 키를 설정하세요."
            )
        resolved_base_url = (base_url or "").strip() or None
        self._tool_schemas = [build_schema(t) for t in tools]
        self._tool_map = {t.name: t for t in tools}
        self._available_tool_names = [t.name for t in tools if getattr(t, "name", "")]
        self._system_prompt = self._compose_system_prompt(system_prompt, tools)
        self._model = (model or _MODEL)
        self._client = OpenAI(api_key=resolved_api_key, base_url=resolved_base_url)
        self._max_steps = max_steps
        self._max_tokens = max_tokens
        self._used_tools: list[str] = []
        self._used_tool_names: list[str] = []
        self._reasoning_steps: list[str] = []
        self._target_unique_tools: int = 0
        self._diversity_nudge_used: bool = False

    def _compose_system_prompt(self, base_prompt: str, tools: list) -> str:
        """Attach a compact tool guide so model can choose tools more autonomously."""
        return f"{base_prompt}\n\n{self._build_tool_guide(tools)}"

    def _build_tool_guide(self, tools: list) -> str:
        category_order = [
            "핵심 시세/차트",
            "포지션/자금",
            "뉴스/공시/거시",
            "이력/RAG/통계",
            "실행/수정",
            "스캔",
            "기타",
        ]
        tool_category = {
            "get_current_price": "핵심 시세/차트",
            "get_chart": "핵심 시세/차트",
            "get_market_index": "핵심 시세/차트",
            "get_portfolio": "포지션/자금",
            "get_deposit": "포지션/자금",
            "get_positions": "포지션/자금",
            "position_context_brief": "포지션/자금",
            "get_order_status": "포지션/자금",
            "get_realized_pnl": "포지션/자금",
            "get_news": "뉴스/공시/거시",
            "get_dart": "뉴스/공시/거시",
            "get_macro_news": "뉴스/공시/거시",
            "get_sector_news": "뉴스/공시/거시",
            "get_global_market": "뉴스/공시/거시",
            "market_news_brief": "뉴스/공시/거시",
            "rss_macro_brief": "뉴스/공시/거시",
            "get_signal_history": "이력/RAG/통계",
            "get_entry_reason": "이력/RAG/통계",
            "preflight_validator": "이력/RAG/통계",
            "search_similar_signals": "이력/RAG/통계",
            "search_text_context": "이력/RAG/통계",
            "search_screening_context": "이력/RAG/통계",
            "search_agent_memory_context": "이력/RAG/통계",
            "get_screening_history": "이력/RAG/통계",
            "get_trade_performance": "이력/RAG/통계",
            "get_condition_accuracy": "이력/RAG/통계",
            "get_pattern_accuracy": "이력/RAG/통계",
            "self_correction": "이력/RAG/통계",
            "update_watchlist": "실행/수정",
            "execute_order": "실행/수정",
            "add_to_watchlist": "실행/수정",
            "scan_volume_surge": "스캔",
            "scan_foreign_buy": "스캔",
            "scan_decline_rank": "스캔",
        }
        restricted_tools = {
            "self_correction": "불확실성 해소용이 아니라 구조 개선이 필요할 때만 사용",
            "update_watchlist": "판단 정당화 목적의 임의 조정 금지",
            "execute_order": "자동매매 모드에서만 사용",
            "add_to_watchlist": "검증 완료 후 최종 편입 단계에서만 사용",
        }

        grouped: dict[str, list] = {k: [] for k in category_order}
        grouped["기타"] = []

        for t in tools:
            name = getattr(t, "name", "")
            cat = tool_category.get(name, "기타")
            grouped.setdefault(cat, []).append(t)

        lines = [
            "## 도구 카탈로그 (요약)",
            "- 호출 순서는 고정하지 말고 불확실성 감소 효과가 큰 도구부터 사용.",
            "- 이력/RAG는 보조 도구이며 1차 사실 확인(시세·차트·포지션·뉴스) 대체 금지.",
        ]
        lines.append("- Rule of thumb: use tools that reduce uncertainty fastest.")
        lines.append("- If one API fails, switch to a complementary tool and continue.")
        lines.append("- Avoid repeated same-tool calls unless parameters materially differ.")

        for cat in category_order:
            items = grouped.get(cat) or []
            if not items:
                continue
            tool_names = []
            for t in items:
                n = getattr(t, "name", "")
                if n:
                    tool_names.append(n)
            if tool_names:
                lines.append(f"- [{cat}] {', '.join(tool_names)}")

        restricted_lines = []
        for n, msg in restricted_tools.items():
            if n in self._tool_map:
                restricted_lines.append(f"{n}: {msg}")
        if restricted_lines:
            lines.append("- 제한 도구:")
            lines.extend([f"  - {x}" for x in restricted_lines])

        return "\n".join(lines)

    def configure_tool(self, name: str, **attrs) -> None:
        """Set runtime attributes on a tool instance when a run needs hard guards."""
        tool = self._tool_map.get(name)
        if not tool:
            return
        for key, value in attrs.items():
            setattr(tool, key, value)

    def configure_run(self, target_unique_tools: int = 0) -> None:
        """Configure per-run soft guardrails (no hard enforcement)."""
        self._target_unique_tools = max(0, int(target_unique_tools or 0))
        self._diversity_nudge_used = False

    @staticmethod
    def _clip_text(text: str, max_len: int = 180) -> str:
        text = str(text or "").replace("\n", " ").strip()
        if len(text) <= max_len:
            return text
        return text[: max_len - 1] + "..."

    def _tool_purpose(self, tool_name: str) -> str:
        purpose_map = {
            "get_current_price": "현재 시세를 확인해 신호의 즉시성 검증",
            "get_chart": "추세/모멘텀 지표를 확보해 기술적 근거 검증",
            "get_market_index": "지수 흐름으로 시장 레짐 확인",
            "get_portfolio": "포트폴리오 상태 확인으로 과도한 리스크 방지",
            "get_deposit": "가용 현금 확인으로 주문 가능성 검증",
            "get_positions": "보유 포지션 확인으로 중복/충돌 주문 방지",
            "position_context_brief": "보유 맥락(평단/비중/손익) 확인",
            "get_order_status": "주문 체결/미체결 상태 확인",
            "get_realized_pnl": "실현 손익 기반 성과 점검",
            "get_news": "개별 종목 뉴스 이벤트 리스크 확인",
            "get_dart": "공시 기반 펀더멘털 이벤트 확인",
            "get_macro_news": "거시 이벤트 영향 확인",
            "get_sector_news": "업종 모멘텀/리스크 확인",
            "get_global_market": "해외 시장 분위기 확인",
            "market_news_brief": "시장 주요 이슈를 요약 확인",
            "rss_macro_brief": "거시 이슈 요약으로 리스크 점검",
            "get_signal_history": "동일 종목/유형의 과거 판단 결과 검증",
            "get_entry_reason": "기존 진입 논리와 현재 조건의 일치성 확인",
            "preflight_validator": "필수 컨텍스트 누락 여부 검증",
            "search_similar_signals": "유사 사례의 사후 성과 근거 확인",
            "search_text_context": "텍스트 컨텍스트 보강",
            "search_screening_context": "스크리닝 맥락 보강",
            "search_agent_memory_context": "에이전트 과거 메모리 기반 맥락 확인",
            "get_screening_history": "스크리닝 이력 비교",
            "get_trade_performance": "최근 매매 성과 기반 전략 유효성 점검",
            "get_condition_accuracy": "조건식 적중률 검증",
            "get_pattern_accuracy": "패턴 기반 성과 검증",
            "self_correction": "판단 일관성 점검 및 오류 교정",
            "update_watchlist": "감시 조건/임계값 업데이트",
            "execute_order": "최종 주문 실행",
            "add_to_watchlist": "신규 감시 종목 편입",
            "scan_volume_surge": "거래량 급증 종목 탐색",
            "scan_foreign_buy": "외국인 수급 유입 종목 탐색",
            "scan_decline_rank": "과도 하락 종목 탐색",
        }
        return purpose_map.get(tool_name, "판단 불확실성을 줄이기 위한 보조 검증")

    def _summarize_result(self, result) -> tuple[str, str]:
        try:
            if isinstance(result, dict):
                if result.get("error"):
                    err = self._clip_text(result.get("error"), 120)
                    return f"error={err}", "fail(error)"
                keys = ",".join(sorted([str(k) for k in result.keys()])[:8]) or "-"
                evidence = f"result_keys={keys}"
                for key in ("count", "total", "items", "rows", "signals"):
                    val = result.get(key)
                    if isinstance(val, int):
                        evidence += f", {key}={val}"
                        break
                    if isinstance(val, list):
                        evidence += f", {key}={len(val)}"
                        break
                return self._clip_text(evidence, 180), "ok"
            if isinstance(result, list):
                return f"list_len={len(result)}", "ok"
            return self._clip_text(f"type={type(result).__name__}", 180), "ok"
        except Exception as e:
            return f"summary_error={self._clip_text(str(e), 80)}", "unknown"

    def _append_reasoning_record(self, tool_name: str, inputs: dict, result) -> None:
        args_text = "-"
        try:
            if isinstance(inputs, dict) and inputs:
                pairs = [f"{k}={inputs[k]}" for k in sorted(inputs.keys())[:4]]
                args_text = self._clip_text(", ".join(pairs), 120)
        except Exception:
            pass

        purpose = self._tool_purpose(tool_name)
        evidence, conclusion = self._summarize_result(result)
        record = (
            f"tool={tool_name} | purpose={purpose} | "
            f"evidence=args[{args_text}]; {evidence} | conclusion={conclusion}"
        )
        self._reasoning_steps.append(record)

    @property
    def unique_tool_count(self) -> int:
        return len(set(self._used_tool_names))

    @property
    def coverage_score(self) -> str:
        total = len(self._available_tool_names) or 1
        used = self.unique_tool_count
        pct = int(round((used / total) * 100))
        return f"{used}/{total} ({pct}%)"

    def run(self, initial_message: str) -> str:
        """Agent loop execution and final text response."""
        messages = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": initial_message},
        ]
        self._used_tools = []
        self._used_tool_names = []
        self._reasoning_steps = []
        self._diversity_nudge_used = False

        for step in range(self._max_steps):
            response = self._client.chat.completions.create(
                model=self._model,
                max_tokens=self._max_tokens,
                tools=self._tool_schemas,
                messages=messages,
                timeout=60.0,
            )

            choice = response.choices[0]

            if choice.finish_reason == "stop":
                unique_tool_count = len(set(self._used_tool_names))
                if self._target_unique_tools > 0 and unique_tool_count < self._target_unique_tools:
                    logger.info(
                        f"[Agent] soft-guardrail: low tool diversity "
                        f"({unique_tool_count}/{self._target_unique_tools})"
                    )
                    if not self._diversity_nudge_used and step < (self._max_steps - 1):
                        self._diversity_nudge_used = True
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    f"Tool coverage is currently {unique_tool_count}/{self._target_unique_tools}. "
                                    "Please do one more brief cross-check with complementary tools before finalizing. "
                                    "If a tool fails or is unsupported, use an alternative and proceed."
                                ),
                            }
                        )
                        continue
                return choice.message.content or ""

            if choice.finish_reason == "tool_calls":
                messages.append(choice.message)

                for tc in choice.message.tool_calls:
                    try:
                        inputs = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        inputs = {}

                    result = self._execute(tc.function.name, inputs)
                    self._append_reasoning_record(tc.function.name, inputs, result)

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": json.dumps(result, ensure_ascii=False, default=str),
                        }
                    )
                continue

            logger.warning(f"[Agent] unexpected finish_reason: {choice.finish_reason}")
            return choice.message.content or ""

        logger.warning(f"[Agent] max_steps({self._max_steps}) exceeded")
        return "[홀드]\n• 근거1: 분석 단계 수(max_steps) 초과로 보수적으로 홀드를 선택합니다."
    def _execute(self, name: str, inputs: dict):
        tool = self._tool_map.get(name)
        if not tool:
            logger.error(f"[Agent] 알 수 없는 도구: {name}")
            return {"error": f"도구 없음: {name}"}

        label = getattr(tool, "label", name)
        # logger.info(f"[Agent] 도구 호출: {label} | 입력: {json.dumps(inputs, ensure_ascii=False)}")
        self._used_tools.append(label)
        self._used_tool_names.append(name)

        try:
            result = tool.execute(**inputs)
            # result_str = json.dumps(result, ensure_ascii=False, default=str)
            # preview = result_str if len(result_str) <= 120 else result_str[:120] + "..."
            # logger.info(f"[Agent] 도구 결과: {label} | {preview}")
            return result
        except Exception as e:
            logger.exception(f"[Agent] {label} 실행 오류")
            return {"error": str(e)}


