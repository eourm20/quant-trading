"""
시장 데이터 도구: 현재가, 차트/지표, 시장 지수, 스캔 도구.
"""

from __future__ import annotations
import logging
from worker.agents.tools.registry import BaseTool

logger = logging.getLogger(__name__)


def _kiwoom():
    from worker.clients.kiwoom_client import KiwoomClient
    return KiwoomClient()


def _p(v) -> int:
    """문자열 → 정수 변환 (부호 포함)."""
    try:
        return int(str(v or "0").replace(",", "").lstrip())
    except Exception:
        return 0


class GetCurrentPriceTool(BaseTool):
    name = "get_current_price"
    label = "현재가 조회"
    description = (
        "종목의 현재가, 등락률, 거래량 등 실시간 시세를 조회합니다. "
        "신호 접수 시점과 현재 가격 사이에 급변이 의심될 때 호출하세요. "
        "get_chart로 이미 충분한 경우 생략 가능합니다."
    )
    input_schema = {
        "properties": {
            "stock_code": {
                "type": "string",
                "description": "종목 코드 (예: 005930)",
            },
        },
        "required": ["stock_code"],
    }

    def execute(self, stock_code: str) -> dict:
        try:
            data = _kiwoom().get_current_price(stock_code)
            cur = abs(_p(data.get("cur_prc") or data.get("stk_prpr") or data.get("prpr")))
            change_rate = data.get("flu_rt") or data.get("prdy_ctrt") or "0"
            volume = _p(data.get("trde_qty") or data.get("acml_vol"))
            return {
                "stock_code": stock_code,
                "current_price": cur,
                "change_rate": change_rate,
                "volume": volume,
            }
        except Exception as e:
            return {"error": str(e)}


class GetChartTool(BaseTool):
    name = "get_chart"
    label = "차트·지표 분석"
    description = (
        "일봉 90일 데이터를 조회하고 RSI, MA, MACD, 볼린저, 스토캐스틱, CCI, "
        "일목균형표, OBV, 캔들패턴, 차트패턴, 피보나치 등 기술적 지표를 계산합니다. "
        "Judgment Agent의 기본 사실 확인 도구이며, Research Agent도 후보별 기술적 검증에 우선 사용하세요."
    )
    input_schema = {
        "properties": {
            "stock_code": {"type": "string", "description": "종목 코드"},
            "horizon": {
                "type": "string",
                "description": "매매 기간 — '단기'/'중기'/'장기'/'' (RSI 기간에 영향)",
                "default": "",
            },
        },
        "required": ["stock_code"],
    }

    def execute(self, stock_code: str, horizon: str = "") -> dict:
        try:
            kiwoom = _kiwoom()
            daily_data = kiwoom.get_daily_ohlcv(stock_code, period=90)
            close_prices, high_prices, low_prices, open_prices, volumes = [], [], [], [], []
            for d in daily_data:
                cp = abs(_p(d.get("cur_prc")))
                hp = abs(_p(d.get("high_pric")))
                lp = abs(_p(d.get("lwst_pric") or d.get("low_pric")))
                op = abs(_p(d.get("strt_pric") or d.get("opn_pric")))
                vol = _p(d.get("trde_qty"))
                if cp:
                    close_prices.append(cp)
                if hp:
                    high_prices.append(hp)
                if lp:
                    low_prices.append(lp)
                if op:
                    open_prices.append(op)
                if vol >= 0:
                    volumes.append(vol)

            if len(close_prices) < 5:
                return {"error": "데이터 부족 (최소 5일 필요)"}

            current_price = close_prices[0]

            from worker.indicators import calculate_chart_summary
            chart = calculate_chart_summary(
                close_prices,
                high_prices,
                current_price,
                low_prices=low_prices,
                open_prices=open_prices,
                volumes=volumes,
            )

            # ChartSummary → dict 직렬화 (None 제거)
            result = {}
            for field in chart.__dataclass_fields__:
                val = getattr(chart, field)
                if val is None:
                    continue
                if isinstance(val, float):
                    result[field] = round(val, 4)
                elif isinstance(val, dict):
                    result[field] = {k: round(v, 2) if isinstance(v, float) else v
                                     for k, v in val.items() if v is not None}
                elif isinstance(val, list):
                    result[field] = val
                else:
                    result[field] = val

            return result
        except Exception as e:
            logger.exception(f"[GetChartTool] {stock_code}")
            return {"error": str(e)}


class GetMarketIndexTool(BaseTool):
    name = "get_market_index"
    label = "시장 지수 조회"
    description = "KOSPI 또는 KOSDAQ 지수 현재가 및 등락률을 조회합니다."
    input_schema = {
        "properties": {
            "market": {
                "type": "string",
                "description": "'kospi' 또는 'kosdaq'",
                "default": "kospi",
            },
        },
        "required": [],
    }

    def execute(self, market: str = "kospi") -> dict:
        try:
            data = _kiwoom().get_market_index(market)
            return {
                "market": market,
                "current": data.get("cur_prc") or data.get("bstp_nmix_prpr"),
                "change_rate": data.get("flu_rt") or data.get("bstp_nmix_prdy_ctrt"),
            }
        except Exception as e:
            return {"error": str(e)}


# ── 스캔 도구 (Research Agent 전용) ──────────────────────────────────────────

class ScanVolumeSurgeTool(BaseTool):
    name = "scan_volume_surge"
    label = "거래량 급증 스캔"
    description = (
        "거래량이 급증한 종목을 스캔합니다 (ka10023). "
        "시장에 새 수급이 붙는 종목을 넓게 찾는 출발점으로 유용합니다."
    )
    input_schema = {
        "properties": {
            "limit": {
                "type": "integer",
                "description": "최대 결과 수",
                "default": 20,
            },
        },
        "required": [],
    }

    def execute(self, limit: int = 20) -> dict:
        try:
            kiwoom = _kiwoom()
            rows = kiwoom.get_volume_surge() or []
            return {"stocks": rows[:limit]}
        except Exception as e:
            logger.warning("[ScanVolumeSurgeTool] 거래량 급증 스캔 실패: %s", e, exc_info=True)
            return {"error": str(e)}


class ScanForeignBuyTool(BaseTool):
    name = "scan_foreign_buy"
    label = "외인 순매수 스캔"
    description = (
        "외국인 순매수 상위 종목을 스캔합니다 (ka10035). "
        "잠재성장이나 수급 축적 후보를 찾고 싶을 때 우선 고려하세요."
    )
    input_schema = {
        "properties": {
            "limit": {
                "type": "integer",
                "description": "최대 결과 수",
                "default": 20,
            },
        },
        "required": [],
    }

    def execute(self, limit: int = 20) -> dict:
        try:
            kiwoom = _kiwoom()
            rows = kiwoom.get_foreign_net_buy() or []
            return {"stocks": rows[:limit]}
        except Exception as e:
            logger.warning("[ScanForeignBuyTool] 외인 순매수 스캔 실패: %s", e, exc_info=True)
            return {"error": str(e)}


class ScanDeclineRankTool(BaseTool):
    name = "scan_decline_rank"
    label = "하락 종목 스캔"
    description = (
        "등락률 하위 종목(하락 상위)을 스캔합니다 (ka10027). "
        "눌림목이나 과도한 조정 후보를 찾을 때 유용하지만, 급락 악재주를 그대로 편입하지 않도록 뉴스·공시 검증이 뒤따라야 합니다."
    )
    input_schema = {
        "properties": {
            "limit": {
                "type": "integer",
                "description": "최대 결과 수",
                "default": 20,
            },
        },
        "required": [],
    }

    def execute(self, limit: int = 20) -> dict:
        try:
            kiwoom = _kiwoom()
            rows = kiwoom.get_decline_rank() or []
            return {"stocks": rows[:limit]}
        except Exception as e:
            logger.warning("[ScanDeclineRankTool] 하락 종목 스캔 실패: %s", e, exc_info=True)
            return {"error": str(e)}
