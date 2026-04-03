"""
BaseAgent — Tool Use 루프 공통 구현 (OpenAI Function Calling 기반).

흐름:
  1. 초기 메시지 전달
  2. GPT가 tool_calls 반환 → 도구 실행 → 결과 재전달
  3. finish_reason == "stop" → 최종 텍스트 반환
  4. max_steps 초과 시 "[max_steps 초과 — 홀드]" 반환
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
        self._system_prompt = system_prompt
        self._model = model
        self._max_steps = max_steps
        self._max_tokens = max_tokens
        self._used_tools: list[str] = []

    def run(self, initial_message: str) -> str:
        """Agent 루프 실행 후 최종 텍스트 반환."""
        messages = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user",   "content": initial_message},
        ]
        self._used_tools = []

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
                return choice.message.content or ""

            # 도구 호출
            if choice.finish_reason == "tool_calls":
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
