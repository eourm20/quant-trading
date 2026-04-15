"""
외부 정보 도구: 뉴스, DART 공시/재무.
"""

from __future__ import annotations
import logging
from worker.agents.tools.registry import BaseTool

logger = logging.getLogger(__name__)


class GetNewsTool(BaseTool):
    name = "get_news"
    label = "뉴스 검색"
    description = (
        "종목 관련 최근 뉴스를 검색합니다. "
        "악재(소송·리콜·실적 쇼크)나 호재(수주·계약·실적 서프라이즈) 등 "
        "주가에 영향을 줄 이슈를 파악할 때 호출하세요. "
        "지표가 긍정적이어도 악재 뉴스가 있으면 홀드 또는 제외 근거가 됩니다."
    )
    input_schema = {
        "properties": {
            "stock_name": {
                "type": "string",
                "description": "종목명 (예: 삼성전자)",
            },
            "max_items": {
                "type": "integer",
                "description": "최대 뉴스 수",
                "default": 5,
            },
        },
        "required": ["stock_name"],
    }

    def execute(self, stock_name: str, max_items: int = 5) -> dict:
        try:
            from worker.clients.news_client import format_news_for_ai, NAVER_CLIENT_ID
            if not NAVER_CLIENT_ID:
                return {"error": "뉴스 API 미설정"}
            text = format_news_for_ai(stock_name, max_items=max_items)
            return {"news": text}
        except Exception as e:
            return {"error": str(e)}


class GetDartTool(BaseTool):
    name = "get_dart"
    label = "공시·재무 조회"
    description = (
        "DART에서 종목의 최근 공시(수주·지분변동·유상증자 등)와 재무지표를 조회합니다. "
        "펀더멘털 변화 여부 확인에 유용합니다. "
        "유상증자·대규모 지분매도 등 희석 이벤트가 있으면 매수를 보류하세요."
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
            from worker.clients.dart_client import format_full_context_for_ai, DART_API_KEY
            if not DART_API_KEY:
                return {"error": "DART API 미설정"}
            text = format_full_context_for_ai(stock_code)
            return {"dart": text}
        except Exception as e:
            return {"error": str(e)}


class GetMacroNewsTool(BaseTool):
    name = "get_macro_news"
    label = "거시경제 뉴스"
    description = (
        "미국 관세·무역, 연준 금리·FOMC, 전쟁·지정학 리스크 등 "
        "글로벌 거시경제 이슈 뉴스를 조회합니다. "
        "시장 전반에 영향을 주는 외부 요인 파악에 활용하세요. "
        "개별 종목 이슈보다 거시 변수 영향이 더 커 보일 때 우선순위가 올라갑니다."
    )
    input_schema = {
        "properties": {
            "max_total": {
                "type": "integer",
                "description": "최대 뉴스 수 (기본 6)",
                "default": 6,
            },
        },
        "required": [],
    }

    def execute(self, max_total: int = 6) -> dict:
        try:
            from worker.clients.news_client import get_macro_news_for_ai
            text = get_macro_news_for_ai(max_total=max_total)
            if not text:
                return {"news": "관련 뉴스 없음"}
            return {"news": text}
        except Exception as e:
            return {"error": str(e)}


class GetSectorNewsTool(BaseTool):
    name = "get_sector_news"
    label = "업종 뉴스"
    description = (
        "특정 업종·섹터의 업황 관련 뉴스를 조회합니다. "
        "후보 종목이 속한 테마의 수급과 악재/호재를 확인할 때 유용합니다. "
        "예: '방위산업', '반도체', '2차전지' 등 업종명을 입력하세요."
    )
    input_schema = {
        "properties": {
            "sector_name": {
                "type": "string",
                "description": "업종명 (예: 방위산업, 반도체, 바이오)",
            },
            "max_items": {
                "type": "integer",
                "description": "최대 뉴스 수 (기본 3)",
                "default": 3,
            },
        },
        "required": ["sector_name"],
    }

    def execute(self, sector_name: str, max_items: int = 3) -> dict:
        try:
            from worker.clients.news_client import format_sector_news_for_ai, NAVER_CLIENT_ID
            if not NAVER_CLIENT_ID:
                return {"error": "뉴스 API 미설정"}
            text = format_sector_news_for_ai(sector_name, max_items=max_items)
            if not text:
                return {"news": "관련 뉴스 없음"}
            return {"news": text}
        except Exception as e:
            return {"error": str(e)}


class GetGlobalMarketTool(BaseTool):
    name = "get_global_market"
    label = "글로벌 지수 조회"
    description = (
        "나스닥, S&P500, 달러/원 환율 등 주요 글로벌 지수를 조회합니다. "
        "해외 시장 영향(전쟁·관세·금리 충격 등)을 판단에 반영할 때 호출하세요. "
        "국내 개별 신호보다 장 전체 리스크가 더 중요해 보일 때 특히 유용합니다."
    )
    input_schema = {
        "properties": {},
        "required": [],
    }

    def execute(self) -> dict:
        try:
            from worker.clients.global_market import get_global_indices
            indices = get_global_indices()
            if not indices:
                return {"error": "글로벌 지수 조회 실패"}
            result = {}
            for name, data in indices.items():
                pct = data["change_pct"]
                sign = "+" if pct >= 0 else ""
                price = data["price"]
                if name == "달러/원":
                    result[name] = f"{price:,.0f}원 ({sign}{pct:.2f}%)"
                else:
                    result[name] = f"{price:,.2f}pt ({sign}{pct:.2f}%)"
            return result
        except Exception as e:
            return {"error": str(e)}


class MarketNewsBriefTool(BaseTool):
    name = "market_news_brief"
    label = "시장 브리프"
    description = (
        "당일 시장 흐름을 요약합니다. "
        "KOSPI/KOSDAQ 지수, 글로벌 지수, 외인 순매수/거래량 급증 상위(수급 proxy), "
        "시장 헤드라인 기반 섹터 분위기를 함께 반환합니다."
    )
    input_schema = {
        "properties": {
            "max_items": {
                "type": "integer",
                "description": "수급/헤드라인 최대 반영 건수",
                "default": 5,
            },
        },
        "required": [],
    }

    @staticmethod
    def _to_int(v) -> int:
        try:
            return abs(int(float(str(v or "0").replace(",", "").strip() or "0")))
        except Exception:
            return 0

    def execute(self, max_items: int = 5) -> dict:
        try:
            from worker.clients.kiwoom_client import KiwoomClient
            from worker.clients.global_market import get_global_indices
            from worker.clients.news_client import search_news, NAVER_CLIENT_ID

            max_items = max(1, int(max_items or 5))
            kw = KiwoomClient()

            kospi = kw.get_market_index("kospi") or {}
            kosdaq = kw.get_market_index("kosdaq") or {}

            global_indices = get_global_indices() or {}
            foreign_buy = (kw.get_foreign_net_buy() or [])[:max_items]
            volume_surge = (kw.get_volume_surge() or [])[:max_items]

            sector_headlines = []
            if NAVER_CLIENT_ID:
                try:
                    sector_headlines = search_news("국내 증시 업종 수급", display=max_items, sort="date")
                except Exception:
                    sector_headlines = []

            def _idx_payload(raw: dict) -> dict:
                return {
                    "current": str(raw.get("cur_prc") or raw.get("bstp_nmix_prpr") or ""),
                    "change_rate": str(raw.get("flu_rt") or raw.get("bstp_nmix_prdy_ctrt") or ""),
                }

            def _stock_payload(rows: list[dict]) -> list[dict]:
                out = []
                for r in rows:
                    out.append(
                        {
                            "stock_code": str(r.get("code") or r.get("stk_cd") or "").strip().lstrip("A"),
                            "stock_name": str(r.get("name") or r.get("stk_nm") or "").strip(),
                            "change_rate": str(r.get("flu_rt") or r.get("prdy_ctrt") or r.get("change_rate") or ""),
                            "volume": self._to_int(r.get("trde_qty") or r.get("acml_vol") or r.get("volume")),
                        }
                    )
                return out

            headline_lines = [
                f"{h.get('pub_date', '')} {str(h.get('title', '')).strip()[:70]}".strip()
                for h in sector_headlines
                if str(h.get("title", "")).strip()
            ]

            brief_lines = [
                f"KOSPI {(_idx_payload(kospi).get('change_rate') or 'N/A')} / "
                f"KOSDAQ {(_idx_payload(kosdaq).get('change_rate') or 'N/A')}",
                f"외인순매수 상위 {len(foreign_buy)}종목, 거래량급증 상위 {len(volume_surge)}종목",
            ]
            if headline_lines:
                brief_lines.append(f"섹터 헤드라인 {len(headline_lines)}건")

            return {
                "brief": " | ".join(brief_lines),
                "indices": {
                    "kospi": _idx_payload(kospi),
                    "kosdaq": _idx_payload(kosdaq),
                    "global": global_indices,
                },
                "flow": {
                    "foreign_net_buy_top": _stock_payload(foreign_buy),
                    "volume_surge_top": _stock_payload(volume_surge),
                },
                "sector_headlines": headline_lines[:max_items],
            }
        except Exception as e:
            return {"error": str(e)}


class RssMacroBriefTool(BaseTool):
    name = "rss_macro_brief"
    label = "RSS 거시 브리프"
    description = (
        "RSS 기반 거시/정책/사회 이슈를 요약합니다. "
        "당일 시장 판단 전에 외생 변수(정책, 국제, 사회 리스크) 컨텍스트를 확보할 때 사용하세요."
    )
    input_schema = {
        "properties": {
            "max_total": {
                "type": "integer",
                "description": "최대 헤드라인 수",
                "default": 6,
            },
        },
        "required": [],
    }

    def execute(self, max_total: int = 6) -> dict:
        try:
            from worker.clients.news_client import get_macro_news_for_ai

            text = get_macro_news_for_ai(max_total=max_total)
            if not text:
                return {"brief": "관련 RSS 이슈 없음", "headlines": []}

            lines = [ln.strip().lstrip("-").strip() for ln in str(text).splitlines() if ln.strip()]
            return {"brief": text, "headlines": lines}
        except Exception as e:
            return {"error": str(e)}
