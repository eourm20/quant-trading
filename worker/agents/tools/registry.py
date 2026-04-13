"""
도구 등록 + 실행 디스패처.
각 도구 모듈은 BaseTool 서브클래스를 정의하고 여기서 임포트하여 등록한다.
"""

from __future__ import annotations
import logging
from typing import Any

logger = logging.getLogger(__name__)


class BaseTool:
    """모든 Agent 도구의 기반 클래스."""

    name: str           # API에 노출되는 이름
    label: str          # 워커 로그 + 텔레그램 표시용 한글 이름
    description: str    # Claude가 도구 선택에 사용하는 설명
    input_schema: dict  # JSON Schema (properties + required)

    def execute(self, **kwargs) -> Any:
        raise NotImplementedError


def build_schema(tool: BaseTool) -> dict:
    """BaseTool → OpenAI function calling 스키마 dict 변환."""
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": {
                "type": "object",
                **tool.input_schema,
            },
        },
    }


def load_judgment_tools() -> list[BaseTool]:
    """Judgment Agent용 도구 목록 반환."""
    from worker.agents.tools.market_tools import (
        GetCurrentPriceTool,
        GetChartTool,
        GetMarketIndexTool,
    )
    from worker.agents.tools.portfolio_tools import (
        GetPortfolioTool,
        GetDepositTool,
        GetPositionsTool,
        GetOrderStatusTool,
    )
    from worker.agents.tools.info_tools import (
        GetNewsTool,
        GetDartTool,
        GetMacroNewsTool,
        GetSectorNewsTool,
        GetGlobalMarketTool,
    )
    from worker.agents.tools.db_tools import (
        GetSignalHistoryTool,
        GetEntryReasonTool,
        UpdateWatchlistTool,
        SearchSimilarSignalsTool,
        GetConditionAccuracyTool,
        GetPatternAccuracyTool,
        SelfCorrectionTool,
    )
    from worker.agents.tools.rag_tools import SearchTextContextTool, SearchScreeningContextTool
    from worker.agents.tools.order_tools import ExecuteOrderTool, AUTO_TRADE

    tools = [
        GetCurrentPriceTool(),
        GetChartTool(),
        GetMarketIndexTool(),
        GetPortfolioTool(),
        GetDepositTool(),
        GetPositionsTool(),
        GetOrderStatusTool(),
        GetNewsTool(),
        GetDartTool(),
        GetMacroNewsTool(),
        GetSectorNewsTool(),
        GetGlobalMarketTool(),
        GetSignalHistoryTool(),
        GetEntryReasonTool(),
        SearchSimilarSignalsTool(),
        GetConditionAccuracyTool(),
        GetPatternAccuracyTool(),
        SelfCorrectionTool(),
        SearchTextContextTool(),
        SearchScreeningContextTool(),
        UpdateWatchlistTool(),
    ]
    if AUTO_TRADE:
        tools.append(ExecuteOrderTool())
    return tools


def load_research_tools() -> list[BaseTool]:
    """Research Agent용 도구 목록 반환 (judgment 도구 + 스캔 도구)."""
    from worker.agents.tools.market_tools import (
        GetCurrentPriceTool,
        GetChartTool,
        GetMarketIndexTool,
        ScanVolumeSurgeTool,
        ScanForeignBuyTool,
        ScanDeclineRankTool,
    )
    from worker.agents.tools.portfolio_tools import (
        GetPortfolioTool,
        GetDepositTool,
    )
    from worker.agents.tools.info_tools import (
        GetNewsTool,
        GetDartTool,
        GetMacroNewsTool,
        GetSectorNewsTool,
        GetGlobalMarketTool,
    )
    from worker.agents.tools.db_tools import AddToWatchlistTool
    from worker.agents.tools.rag_tools import SearchScreeningContextTool

    return [
        GetCurrentPriceTool(),
        GetChartTool(),
        GetMarketIndexTool(),
        ScanVolumeSurgeTool(),
        ScanForeignBuyTool(),
        ScanDeclineRankTool(),
        GetPortfolioTool(),
        GetDepositTool(),
        GetNewsTool(),
        GetDartTool(),
        GetMacroNewsTool(),
        GetSectorNewsTool(),
        GetGlobalMarketTool(),
        SearchScreeningContextTool(),
        AddToWatchlistTool(),
    ]
