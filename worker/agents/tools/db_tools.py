"""
DB 도구: 신호 이력, 진입 근거, watchlist 업데이트, 관심종목 추가,
         유사 신호 검색, 조건별/패턴별 적중률, 자기보정.
"""

from __future__ import annotations
import json
import logging
from worker.agents.tools.registry import BaseTool

logger = logging.getLogger(__name__)


class GetSignalHistoryTool(BaseTool):
    name = "get_signal_history"
    label = "AI 판단 이력 조회"
    description = (
        "특정 종목의 과거 AI 판단 이력(판정, 날짜, 결과 수익률)을 조회합니다. "
        "현재와 비슷한 국면에서 이 종목에 어떤 결정을 내렸는지, 같은 종목에 대해 너무 잦게 판단을 뒤집고 있지 않은지 확인할 때 유용합니다. "
        "일관성 확인 목적이라면 signal_type은 생략해 전체 이력을 보세요."
    )
    input_schema = {
        "properties": {
            "stock_code": {"type": "string", "description": "종목 코드"},
            "signal_type": {
                "type": "string",
                "description": "신호 유형 필터 — 'entry'/'exit'/'add'/'both'/'' (전체)",
                "default": "",
            },
            "limit": {"type": "integer", "description": "최대 조회 건수", "default": 5},
        },
        "required": ["stock_code"],
    }

    def execute(self, stock_code: str, signal_type: str = "", limit: int = 5) -> dict:
        try:
            from data.db import get_signal_history
            rows = get_signal_history(stock_code, signal_type=signal_type, limit=limit)
            return {"history": rows}
        except Exception as e:
            return {"error": str(e)}


class GetEntryReasonTool(BaseTool):
    name = "get_entry_reason"
    label = "진입 근거 조회"
    description = (
        "해당 종목의 최근 전략 노트를 조회합니다. "
        "진입 근거·매매 메모뿐 아니라, 이전 홀드 판단 시 저장된 전환조건도 포함됩니다. "
        "'RSI X 회복 시 매수 재검토' 같은 이전 조건을 현재 판단에 반영하세요. "
        "전략 노트는 종목명(한글)으로 기록되므로 stock_name을 반드시 전달하세요."
    )
    input_schema = {
        "properties": {
            "stock_name": {
                "type": "string",
                "description": "종목명 (한글). 예: '경방', '동화약품'. 이 값으로 검색하는 것이 정확합니다.",
                "default": "",
            },
            "stock_code": {
                "type": "string",
                "description": "종목 코드 (보조 검색용). stock_name과 함께 OR 조건으로 검색.",
                "default": "",
            },
            "limit": {"type": "integer", "description": "최대 조회 건수", "default": 10},
        },
        "required": [],
    }

    def execute(self, stock_name: str = "", stock_code: str = "", limit: int = 10) -> dict:
        try:
            from data.db import get_strategy_notes
            notes = get_strategy_notes(limit=50)
            keywords = [kw for kw in [stock_name, stock_code] if kw]
            if keywords:
                def _match(n):
                    text = str(n.get("summary", "")) + str(n.get("detail", ""))
                    return any(kw in text for kw in keywords)
                notes = [n for n in notes if _match(n)]
            return {"notes": notes[:limit]}
        except Exception as e:
            return {"error": str(e)}


class UpdateWatchlistTool(BaseTool):
    name = "update_watchlist"
    label = "조건값 변경"
    description = (
        "watchlist의 신호 조건 임계값을 변경합니다. "
        "변경 가능 필드: rsi_oversold, rsi_overbought, rsi_oversold_intraday, volume_surge_ratio. "
        "판단을 정당화하려고 임의로 숫자를 바꾸는 용도가 아니라, 구조적으로 기준이 잘못되었음이 명확할 때만 사용하세요. "
        "목표가/손절가는 quant_position_update를 사용하세요."
    )
    input_schema = {
        "properties": {
            "stock_code": {"type": "string", "description": "종목 코드"},
            "field": {
                "type": "string",
                "description": "변경할 필드명 (rsi_oversold / rsi_overbought / rsi_oversold_intraday / volume_surge_ratio)",
            },
            "value": {
                "type": "number",
                "description": "새 값",
            },
            "reason": {
                "type": "string",
                "description": "변경 근거 (로그용)",
                "default": "",
            },
        },
        "required": ["stock_code", "field", "value"],
    }

    _ALLOWED_FIELDS = {
        "rsi_oversold", "rsi_overbought",
        "rsi_oversold_intraday", "volume_surge_ratio",
    }

    def execute(self, stock_code: str, field: str, value: float, reason: str = "") -> dict:
        if field not in self._ALLOWED_FIELDS:
            return {"error": f"변경 불가 필드: {field}. 허용: {', '.join(self._ALLOWED_FIELDS)}"}
        try:
            from data.db import update_stock_field
            update_stock_field(stock_code, field, value)
            logger.info(f"[Agent] watchlist 업데이트: {stock_code} {field}={value} ({reason})")
            return {"ok": True, "stock_code": stock_code, "field": field, "value": value}
        except Exception as e:
            return {"error": str(e)}


class AddToWatchlistTool(BaseTool):
    """Research Agent 전용 — 관심종목 신규 등록."""

    name = "add_to_watchlist"
    label = "관심종목 등록"
    description = (
        "신규 종목을 관심종목(watchlist)에 등록합니다. 이미 존재하면 무시됩니다. "
        "Research Agent가 충분한 검증을 마친 뒤 최종 편입 결론을 실행할 때만 사용하세요. "
        "스캔 결과만 보고 바로 등록하지 말고 기술적·기본적·뉴스 리스크를 먼저 확인하세요."
    )
    input_schema = {
        "properties": {
            "stock_code": {"type": "string", "description": "종목 코드"},
            "stock_name": {"type": "string", "description": "종목명"},
            "horizon": {
                "type": "string",
                "description": "매매 기간: '단기'/'중기'/'장기'/''",
                "default": "중기",
            },
            "conditions": {
                "type": "object",
                "description": "신호 조건 JSON (선택). 예: {\"rsi_oversold\": 40}",
                "default": {},
            },
            "reason": {
                "type": "string",
                "description": "편입 근거 (전략 노트 기록용)",
                "default": "",
            },
        },
        "required": ["stock_code", "stock_name"],
    }

    def __init__(self):
        # Runtime guard (configured by ResearchAgent.run)
        self.max_additions: int = 5
        self.addition_count: int = 0

    def execute(
        self,
        stock_code: str,
        stock_name: str,
        horizon: str = "중기",
        conditions: dict | None = None,
        reason: str = "",
    ) -> dict:
        conditions = conditions or {}
        try:
            from data.db import (
                upsert_stock,
                get_watchlist,
                get_position,
                update_position_field,
                save_screening_log,
                update_screening_action,
            )
            from worker.watchlist_policy import normalize_watchlist_payload
            if self.addition_count >= int(self.max_additions or 0):
                logger.warning(
                    f"[Agent] add_to_watchlist 한도 도달: {self.addition_count}/{self.max_additions} "
                    f"({stock_name} {stock_code})"
                )
                return {
                    "error": (
                        f"watchlist 등록 한도 도달: {self.addition_count}/{self.max_additions}. "
                        "추가 등록 없이 분석만 수행하세요."
                    )
                }
            existing = [s for s in get_watchlist() if s["code"] == stock_code]
            if existing:
                try:
                    _log_id = save_screening_log(
                        stock_code=stock_code,
                        stock_name=stock_name,
                        source="research_agent",
                        recommendation="관심종목 등록",
                        reason=reason or "already exists in watchlist",
                        ai_response=(
                            "registered by research_agent (already exists)\n\n"
                            "[RAG_CONTEXT_JSON]\n"
                            "```json\n"
                            + json.dumps(
                                {
                                    "stock_code": stock_code,
                                    "stock_name": stock_name,
                                    "recommendation": "관심종목 등록",
                                    "reason": reason or "already exists in watchlist",
                                    "source": "research_agent",
                                },
                                ensure_ascii=False,
                                indent=2,
                            )
                            + "\n```"
                        ),
                    )
                    update_screening_action(_log_id, "auto_accepted")
                except Exception:
                    pass
                return {"ok": True, "already_exists": True, "stock_code": stock_code}
            watchlist_payload, position_payload = normalize_watchlist_payload(
                horizon=horizon,
                analysis=None,
                raw_conditions=conditions,
            )
            upsert_stock(stock_code, stock_name, enabled=True, conditions=watchlist_payload)
            if position_payload and get_position(stock_code):
                for field, value in position_payload.items():
                    update_position_field(stock_code, field, value)
            try:
                from data.db import save_strategy_note
                save_strategy_note(
                    "watchlist",
                    f"{stock_name} watchlist 파이프라인 등록",
                    (
                        f"stock_code={stock_code}\n"
                        f"horizon={horizon}\n"
                        f"reason={reason or '-'}\n"
                        f"watchlist_payload={json.dumps(watchlist_payload, ensure_ascii=False)}\n"
                        f"position_payload={json.dumps(position_payload, ensure_ascii=False)}"
                    ),
                )
            except Exception as _note_e:
                logger.warning(f"[Agent] strategy_note 저장 실패: {stock_name}({stock_code}) {_note_e}")
            self.addition_count += 1
            try:
                _log_id = save_screening_log(
                    stock_code=stock_code,
                    stock_name=stock_name,
                    source="research_agent",
                    recommendation="관심종목 등록",
                    reason=reason or "",
                    ai_response=(
                        "registered by research_agent\n\n"
                        "[RAG_CONTEXT_JSON]\n"
                        "```json\n"
                        + json.dumps(
                            {
                                "stock_code": stock_code,
                                "stock_name": stock_name,
                                "recommendation": "관심종목 등록",
                                "reason": reason or "",
                                "source": "research_agent",
                                "horizon": horizon,
                                "watchlist_payload": watchlist_payload,
                                "position_payload": position_payload,
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n```"
                    ),
                )
                update_screening_action(_log_id, "auto_accepted")
            except Exception as _log_e:
                logger.warning(f"[Agent] screening_log 저장 실패: {stock_name}({stock_code}) {_log_e}")
            logger.info(f"[Agent] watchlist 추가: {stock_name}({stock_code}) horizon={horizon} 근거={reason}")
            return {
                "ok": True,
                "stock_code": stock_code,
                "stock_name": stock_name,
                "addition_count": self.addition_count,
                "max_additions": self.max_additions,
            }
        except Exception as e:
            return {"error": str(e)}


class SearchSimilarSignalsTool(BaseTool):
    """유사 지표 상황에서의 과거 AI 판단 + 결과 조회 (SQL 범위 필터 RAG)."""

    name = "search_similar_signals"
    label = "유사 신호 검색"
    description = (
        "현재 RSI, 추세, 거래량 등과 유사했던 과거 신호를 검색하여 "
        "당시 AI 판단(verdict)과 실제 수익률을 반환합니다. "
        "주 판단 도구라기보다 확신이 애매한 상황에서 과거 유사 사례를 참고하는 보조 도구입니다. "
        "결과가 0건이면 내부적으로 범위를 완화해 재검색합니다(엄격→완화→광범위). "
        "현재 종목의 실제 포지션·현금·뉴스 확인을 대체하지는 않습니다."
    )
    input_schema = {
        "properties": {
            "rsi": {"type": "number", "description": "현재 RSI 값 (±5 범위로 검색)"},
            "trend": {"type": "string", "description": "현재 추세 ('상승'/'하락'/'횡보')"},
            "signal_type": {"type": "string", "description": "신호 유형 필터 ('entry'/'exit'/'add'/'')", "default": ""},
            "volume_ratio": {"type": "number", "description": "현재 거래량 배율 (0.5~2배 범위로 검색)"},
            "above_ma20": {"type": "boolean", "description": "MA20 위에 있는지 여부"},
            "limit": {"type": "integer", "description": "최대 결과 수", "default": 5},
            "days": {"type": "integer", "description": "검색 기간(일)", "default": 90},
        },
        "required": [],
    }

    def execute(
        self,
        rsi: float | None = None,
        trend: str | None = None,
        signal_type: str = "",
        volume_ratio: float | None = None,
        above_ma20: bool | None = None,
        limit: int = 5,
        days: int = 90,
    ) -> dict:
        try:
            from data.db import search_similar_signals
            # 1) strict: 요청값 그대로
            rows = search_similar_signals(
                rsi=rsi, trend=trend, signal_type=signal_type,
                volume_ratio=volume_ratio, above_ma20=above_ma20,
                limit=limit, days=days,
            )
            stage = "strict"
            used_filters = {
                "rsi": rsi,
                "trend": trend,
                "signal_type": signal_type or "",
                "volume_ratio": volume_ratio,
                "above_ma20": above_ma20,
                "days": days,
            }

            # 2) relaxed: 허용 오차 확대 + above_ma20 완화 + 기간 확장
            if not rows:
                rows = search_similar_signals(
                    rsi=rsi,
                    trend=trend,
                    signal_type=signal_type,
                    volume_ratio=volume_ratio,
                    above_ma20=None,
                    limit=limit,
                    days=min(max(days, 180), 365),
                    rsi_tolerance=10.0,
                    volume_low_multiplier=0.25,
                    volume_high_multiplier=3.0,
                )
                if rows:
                    stage = "relaxed"
                    used_filters.update({
                        "above_ma20": None,
                        "days": min(max(days, 180), 365),
                        "rsi_tolerance": 10.0,
                        "volume_range": "x0.25~x3.0",
                    })

            # 3) broad: trend/signal_type까지 완화
            if not rows:
                rows = search_similar_signals(
                    rsi=rsi,
                    trend=None,
                    signal_type="",
                    volume_ratio=volume_ratio,
                    above_ma20=None,
                    limit=limit,
                    days=365,
                    rsi_tolerance=12.0,
                    volume_low_multiplier=0.2,
                    volume_high_multiplier=4.0,
                )
                if rows:
                    stage = "broad"
                    used_filters.update({
                        "trend": None,
                        "signal_type": "",
                        "above_ma20": None,
                        "days": 365,
                        "rsi_tolerance": 12.0,
                        "volume_range": "x0.2~x4.0",
                    })

            return {
                "count": len(rows),
                "signals": rows,
                "search_stage": stage,
                "fallback_used": stage != "strict",
                "used_filters": used_filters,
            }
        except Exception as e:
            return {"error": str(e)}


class GetConditionAccuracyTool(BaseTool):
    """트리거 조건별 적중률 통계 — 어떤 조건이 효과적인지 파악."""

    name = "get_condition_accuracy"
    label = "조건별 적중률"
    description = (
        "각 신호 조건(RSI 과매도, 골든크로스 등)별로 적중률과 평균 수익률을 반환합니다. "
        "개별 신호를 바로 매수/매도로 단정하기보다, 감지된 조건의 신뢰도를 보정하고 구조적 문제를 점검할 때 유용합니다."
    )
    input_schema = {
        "properties": {
            "days": {"type": "integer", "description": "분석 기간(일)", "default": 30},
            "min_count": {"type": "integer", "description": "최소 신호 건수 (이하 제외)", "default": 3},
        },
        "required": [],
    }

    def execute(self, days: int = 30, min_count: int = 3) -> dict:
        try:
            from data.db import get_condition_accuracy
            rows = get_condition_accuracy(days=days, min_count=min_count)
            return {"count": len(rows), "conditions": rows}
        except Exception as e:
            return {"error": str(e)}


class GetPatternAccuracyTool(BaseTool):
    """차트 패턴별 적중률 통계 — 어떤 패턴이 신뢰도 높은지 파악."""

    name = "get_pattern_accuracy"
    label = "패턴별 적중률"
    description = (
        "망치형, 골든크로스 등 차트 패턴별 적중률과 평균 수익률을 반환합니다. "
        "차트에서 감지한 패턴이 실제로 신뢰할 만한지 교차검증할 때 유용한 보조 도구입니다."
    )
    input_schema = {
        "properties": {
            "days": {"type": "integer", "description": "분석 기간(일)", "default": 30},
            "min_count": {"type": "integer", "description": "최소 발생 건수 (이하 제외)", "default": 2},
        },
        "required": [],
    }

    def execute(self, days: int = 30, min_count: int = 2) -> dict:
        try:
            from data.db import get_pattern_accuracy
            rows = get_pattern_accuracy(days=days, min_count=min_count)
            return {"count": len(rows), "patterns": rows}
        except Exception as e:
            return {"error": str(e)}


class GetScreeningHistoryTool(BaseTool):
    """스크리닝 로그 조회 (7d/30d 성과 포함)."""

    name = "get_screening_history"
    label = "스크리닝 성과 조회"
    description = (
        "screening_log에서 스크리닝 이력과 사후 성과(result_7d/result_30d)를 조회합니다. "
        "특정 종목의 과거 추천 근거, 추천 타입, 사후 성과를 함께 확인해 "
        "후보 유지/제외 판단의 일관성을 높일 때 유용합니다."
    )
    input_schema = {
        "properties": {
            "stock_code": {"type": "string", "description": "종목코드(선택)"},
            "days": {"type": "integer", "description": "조회 기간(일)", "default": 30},
            "limit": {"type": "integer", "description": "최대 조회 건수", "default": 20},
            "recommendation": {
                "type": "string",
                "description": "추천 필터(예: 관심종목 등록/보류/부적합)",
                "default": "",
            },
            "only_with_results": {
                "type": "boolean",
                "description": "성과 컬럼(result_7d/30d)이 하나라도 있는 행만 조회",
                "default": False,
            },
        },
        "required": [],
    }

    def execute(
        self,
        stock_code: str = "",
        days: int = 30,
        limit: int = 20,
        recommendation: str = "",
        only_with_results: bool = False,
    ) -> dict:
        try:
            from data.db import get_conn, _now_kst
            from datetime import timedelta

            since = (_now_kst() - timedelta(days=max(1, int(days)))).strftime("%Y-%m-%d")
            where = ["created_at >= ?"]
            params: list = [since]

            if stock_code:
                where.append("stock_code = ?")
                params.append(stock_code)
            if recommendation:
                where.append("recommendation = ?")
                params.append(recommendation)
            if only_with_results:
                where.append("(result_7d IS NOT NULL OR result_30d IS NOT NULL)")

            sql = (
                "SELECT id, created_at, stock_code, stock_name, source, recommendation, reason, "
                "rr_ratio, current_price, result_7d, result_30d, market_snapshot "
                "FROM screening_log "
                f"WHERE {' AND '.join(where)} "
                "ORDER BY created_at DESC LIMIT ?"
            )
            params.append(max(1, int(limit)))
            with get_conn() as conn:
                rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

            return {"count": len(rows), "rows": rows}
        except Exception as e:
            return {"error": str(e)}


class GetTradePerformanceTool(BaseTool):
    """실거래 성과 조회 (1d/3d/5d)."""

    name = "get_trade_performance"
    label = "실거래 성과 조회"
    description = (
        "trades 테이블에서 체결 이력과 사후 성과(result_1d/result_3d/result_5d)를 조회합니다. "
        "종목/매수·매도 방향별 성과를 점검해 AI 판단 품질을 검증할 때 사용합니다."
    )
    input_schema = {
        "properties": {
            "stock_code": {"type": "string", "description": "종목코드(선택)"},
            "days": {"type": "integer", "description": "조회 기간(일)", "default": 60},
            "limit": {"type": "integer", "description": "최대 조회 건수", "default": 30},
            "side": {
                "type": "string",
                "description": "매수/매도 필터(예: 매수, 매도, BUY, SELL)",
                "default": "",
            },
            "only_with_results": {
                "type": "boolean",
                "description": "성과 컬럼(result_1d/3d/5d)이 하나라도 있는 행만 조회",
                "default": True,
            },
        },
        "required": [],
    }

    def execute(
        self,
        stock_code: str = "",
        days: int = 60,
        limit: int = 30,
        side: str = "",
        only_with_results: bool = True,
    ) -> dict:
        try:
            from data.db import get_conn, _now_kst
            from datetime import timedelta

            since = (_now_kst() - timedelta(days=max(1, int(days)))).strftime("%Y-%m-%d")
            where = ["executed_at >= ?"]
            params: list = [since]

            if stock_code:
                where.append("stock_code = ?")
                params.append(stock_code)
            if side:
                where.append("side = ?")
                params.append(side)
            if only_with_results:
                where.append("(result_1d IS NOT NULL OR result_3d IS NOT NULL OR result_5d IS NOT NULL)")

            sql = (
                "SELECT trade_id, executed_at, stock_code, stock_name, side, quantity, price, "
                "result_1d, result_3d, result_5d "
                "FROM trades "
                f"WHERE {' AND '.join(where)} "
                "ORDER BY executed_at DESC, trade_id DESC LIMIT ?"
            )
            params.append(max(1, int(limit)))
            with get_conn() as conn:
                rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

            return {"count": len(rows), "rows": rows}
        except Exception as e:
            return {"error": str(e)}


class SelfCorrectionTool(BaseTool):
    """자기보정 — 저성과 조건을 감지하고 임계값 변경을 제안."""

    name = "self_correction"
    label = "자기보정 분석"
    description = (
        "적중률이 낮은 조건을 자동 감지하고 watchlist 임계값 변경을 제안합니다. "
        "개별 종목 한 건의 결론이 애매하다는 이유로 남용하지 말고, 반복적으로 성과가 나쁜 조건이 보여 구조 개선이 필요할 때만 사용하세요. "
        "실제 변경은 update_watchlist를 통해 별도로 처리하세요."
    )
    input_schema = {
        "properties": {
            "days": {"type": "integer", "description": "분석 기간(일)", "default": 30},
            "hit_rate_threshold": {
                "type": "number",
                "description": "이 적중률 미만 조건을 문제로 판단 (%)",
                "default": 40.0,
            },
        },
        "required": [],
    }

    def execute(self, days: int = 30, hit_rate_threshold: float = 40.0) -> dict:
        try:
            from data.db import get_condition_accuracy, get_verdict_accuracy, get_watchlist

            condition_stats = get_condition_accuracy(days=days, min_count=3)
            verdict_stats = get_verdict_accuracy(days=days)
            watchlist = {s["code"]: s for s in get_watchlist()}

            # 저성과 조건 추출
            low_perf = [c for c in condition_stats if c["hit_rate_3d"] < hit_rate_threshold]

            # 전체 verdict 성과 요약
            verdict_summary = {
                v: {"count": s["count"], "hit_rate_3d": s["hit_rate_3d"], "avg_3d": s["avg_3d"]}
                for v, s in verdict_stats.items()
            }

            return {
                "period_days": days,
                "verdict_summary": verdict_summary,
                "low_performance_conditions": low_perf,
                "suggestion": (
                    "적중률 낮은 조건들을 검토하세요. "
                    "RSI 기준이 너무 넓거나 시장 환경과 맞지 않을 수 있습니다. "
                    "update_watchlist로 임계값을 조정하거나 해당 조건을 비활성화하세요."
                ) if low_perf else "모든 조건이 정상 범위입니다.",
            }
        except Exception as e:
            return {"error": str(e)}
