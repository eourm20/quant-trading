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

if not _OPENAI_KEY:
    raise RuntimeError("Agent 모드는 OPENAI_API_KEY 필수입니다.")

from openai import OpenAI
_client = OpenAI(api_key=_OPENAI_KEY)


class BaseAgent:
    """OpenAI Function Calling 기반 Tool Use 루프."""

    def __init__(
        self,
        tools: list,
        system_prompt: str,
        model: str = _MODEL,
        max_steps: int = 10,
        max_tokens: int = 1024,
    ):
        from worker.agents.tools.registry import build_schema
        self._tool_schemas = [build_schema(t) for t in tools]
        self._tool_map = {t.name: t for t in tools}
        self._available_tool_names = [t.name for t in tools if getattr(t, "name", "")]
        self._system_prompt = self._compose_system_prompt(system_prompt, tools)
        self._model = model
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
        """Agent 루프 실행 후 최종 텍스트 반환."""
        messages = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user",   "content": initial_message},
        ]
        self._used_tools = []
        self._used_tool_names = []
        self._reasoning_steps = []
        self._diversity_nudge_used = False

        for step in range(self._max_steps):
            response = _client.chat.completions.create(
                model=self._model,
                max_tokens=self._max_tokens,
                tools=self._tool_schemas,
                messages=messages,
            )

            choice = response.choices[0]

            # 최종 응답
            if choice.finish_reason == "stop":
                unique_tool_count = len(set(self._used_tool_names))
                if self._target_unique_tools > 0 and unique_tool_count < self._target_unique_tools:
                    logger.info(
                        f"[Agent] soft-guardrail: low tool diversity "
                        f"({unique_tool_count}/{self._target_unique_tools})"
                    )
                    # Soft nudge only once: offer one extra exploration turn, then accept final answer.
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

            # 도구 호출
            if choice.finish_reason == "tool_calls":
                # 도구 호출 전 GPT 추론 텍스트 캡처 (있을 때만)
                if choice.message.content:
                    self._reasoning_steps.append(choice.message.content)

                # assistant 메시지 전체를 그대로 추가 (tool_calls 포함)
                messages.append(choice.message)

                for tc in choice.message.tool_calls:
                    try:
                        inputs = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        inputs = {}

                    result = self._execute(tc.function.name, inputs)

                    # OpenAI: role="tool", tool_call_id 매핑
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(result, ensure_ascii=False, default=str),
                    })
                continue

            # 예상치 못한 finish_reason
            logger.warning(f"[Agent] 예상치 못한 finish_reason: {choice.finish_reason}")
            return choice.message.content or ""

        logger.warning(f"[Agent] max_steps({self._max_steps}) 초과")
        # Keep a parse-safe verdict format so downstream logic treats this as a normal HOLD.
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
            # preview = result_str if len(result_str) <= 120 else result_str[:120] + "…"
            # logger.info(f"[Agent] 도구 결과: {label} | {preview}")
            return result
        except Exception as e:
            logger.exception(f"[Agent] {label} 실행 오류")
            return {"error": str(e)}
