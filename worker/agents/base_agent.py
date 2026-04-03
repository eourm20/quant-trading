"""
BaseAgent — Tool Use 루프 공통 구현.

흐름:
  1. 초기 메시지 전달
  2. Claude가 tool_use 블록 반환 → 도구 실행 → 결과 재전달
  3. stop_reason == "end_turn" → 최종 텍스트 반환
  4. max_steps 초과 시 "[max_steps 초과 — 홀드]" 반환
"""

from __future__ import annotations
import json
import logging
import os
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '..', '.env'))

logger = logging.getLogger(__name__)

_ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")

if not _ANTHROPIC_KEY:
    raise RuntimeError("Agent 모드는 ANTHROPIC_API_KEY 필수입니다.")

from anthropic import Anthropic
_client = Anthropic(api_key=_ANTHROPIC_KEY)


class BaseAgent:
    """Tool Use 루프를 처리하는 기반 클래스."""

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
        self._system_prompt = system_prompt
        self._model = model
        self._max_steps = max_steps
        self._max_tokens = max_tokens
        self._used_tools: list[str] = []

    def run(self, initial_message: str) -> str:
        """Agent 루프 실행 후 최종 텍스트 반환."""
        messages = [{"role": "user", "content": initial_message}]
        self._used_tools = []

        for step in range(self._max_steps):
            response = _client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=[
                    {
                        "type": "text",
                        "text": self._system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                tools=self._tool_schemas,
                messages=messages,
            )

            if response.stop_reason == "end_turn":
                return self._extract_text(response)

            if response.stop_reason != "tool_use":
                logger.warning(f"[Agent] 예상치 못한 stop_reason: {response.stop_reason}")
                return self._extract_text(response)

            # 도구 실행
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = self._execute(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result, ensure_ascii=False, default=str),
                    })

            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})

        logger.warning(f"[Agent] max_steps({self._max_steps}) 초과")
        return "[max_steps 초과 — 홀드]"

    def _execute(self, name: str, inputs: dict):
        tool = self._tool_map.get(name)
        if not tool:
            logger.error(f"[Agent] 알 수 없는 도구: {name}")
            return {"error": f"도구 없음: {name}"}

        label = getattr(tool, "label", name)
        logger.info(f"[Agent] 도구 호출: {label} | 입력: {json.dumps(inputs, ensure_ascii=False)}")
        self._used_tools.append(label)

        try:
            return tool.execute(**inputs)
        except Exception as e:
            logger.exception(f"[Agent] {label} 실행 오류")
            return {"error": str(e)}

    @staticmethod
    def _extract_text(response) -> str:
        parts = []
        for block in response.content:
            if hasattr(block, "text"):
                parts.append(block.text)
        return "\n".join(parts).strip()
