"""
자동 종목 스크리닝 (워커 전용)
- 장 마감 후(15:40) 유망 종목 자동 발굴 → AI 분석 → watchlist 자동 추가
- 수동 종목 분석은 Claude Desktop에서 MCP 도구로 직접 수행
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timezone, timedelta

_KST = timezone(timedelta(hours=9))


def now_kst() -> datetime:
    return datetime.now(_KST).replace(tzinfo=None)

from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

logger = logging.getLogger(__name__)

try:
    import yaml as _yaml
    _cfg_path = os.path.join(os.path.dirname(__file__), '..', 'config', 'worker.yaml')
    with open(_cfg_path, encoding="utf-8") as _f:
        _PREFILTER_CONFIG = _yaml.safe_load(_f).get("prefilter", {})
except Exception:
    _PREFILTER_CONFIG = {}

_ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
_OPENAI_KEY = os.getenv("OPENAI_API_KEY", "").strip()

if _ANTHROPIC_KEY:
    from anthropic import Anthropic
    _ai_client = Anthropic(api_key=_ANTHROPIC_KEY)
    _AI_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
    _AI_MODEL_MINI = os.getenv("CLAUDE_MODEL_MINI", "claude-haiku-4-5-20251001")
    _AI_BACKEND = "anthropic"
elif _OPENAI_KEY:
    from openai import OpenAI
    _ai_client = OpenAI(api_key=_OPENAI_KEY)
    _AI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1")
    _AI_MODEL_MINI = os.getenv("OPENAI_MODEL_MINI", "gpt-4.1-mini")
    _AI_BACKEND = "openai"
else:
    _ai_client = None
    _AI_MODEL = ""
    _AI_MODEL_MINI = ""
    _AI_BACKEND = ""


def _fmt_int(value, default: int = 0) -> int:
    """None/문자열/숫자를 안전하게 정수로 변환."""
    try:
        if value is None or value == "":
            return default
        return int(float(str(value).replace(",", "").strip()))
    except Exception:
        return default


def _build_market_text(kiwoom) -> str:
    try:
        kospi = kiwoom.get_market_index("kospi")
        kosdaq = kiwoom.get_market_index("kosdaq")
        kospi_rate = kospi.get("flu_rt") or kospi.get("prdy_ctrt") or "N/A"
        kosdaq_rate = kosdaq.get("flu_rt") or kosdaq.get("prdy_ctrt") or "N/A"
        return f"코스피 {kospi_rate}% / 코스닥 {kosdaq_rate}%"
    except Exception:
        return "시장 지수 조회 실패"


def _extract_json_block(text: str) -> str | None:
    """응답 텍스트에서 recommendation 키가 포함된 첫 JSON 객체 블록 추출."""
    m = re.search(r'\{.*"recommendation".*\}', text, re.DOTALL)
    if not m:
        return None
    chunk = text[m.start():]
    depth = 0
    for i, ch in enumerate(chunk):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        if depth == 0:
            return chunk[: i + 1]
    return m.group()


def _repair_screening_json(raw_text: str) -> str:
    """파싱 실패 시 LLM에게 JSON 정규화 재요청."""
    repair_prompt = (
        "아래 텍스트를 JSON 객체 1개로만 정규화해서 반환해.\n"
        "설명/코드블록/주석 없이 순수 JSON만 출력.\n"
        "키 이름은 유지하고, 잘못된 따옴표/쉼표/중괄호를 고쳐.\n\n"
        f"{raw_text}"
    )
    if _AI_BACKEND == "anthropic":
        response = _ai_client.messages.create(
            model=_AI_MODEL_MINI,
            max_tokens=1200,
            messages=[{"role": "user", "content": repair_prompt}],
        )
        return response.content[0].text
    response = _ai_client.chat.completions.create(
        model=_AI_MODEL_MINI,
        max_tokens=1200,
        messages=[{"role": "user", "content": repair_prompt}],
    )
    return response.choices[0].message.content or ""


# ═══════════════════════════ 후보 수집 ═══════════════════════════

def _screen_candidates() -> list[dict]:
    """키움 API로 유망 종목 후보 수집. 기존 watchlist 종목은 제외.

    소스 우선순위: HTS 조건검색 → 조용한 축적 → 외인 순매수 → 거래량 급증 → 등락률 하위
    HTS 후보는 source_tier="hts" 태그 → 프리필터 완화 적용.

    Returns:
        [{stock_code, stock_name, source, source_tier}, ...]  최대 50개
    """
    from worker.clients.kiwoom_client import KiwoomClient
    from data.db import get_watchlist

    kiwoom = KiwoomClient()
    existing_codes = {s["code"] for s in get_watchlist()}

    candidates = []
    seen_codes: set[str] = set()

    def _add(code: str, name: str, source: str, tier: str = "general") -> bool:
        if code and len(code) == 6 and code not in existing_codes and code not in seen_codes:
            seen_codes.add(code)
            candidates.append({"stock_code": code, "stock_name": name, "source": source, "source_tier": tier})
            return True
        return False

    def _extract(items, source, tier="general", limit=20):
        for item in items[:limit]:
            code = str(item.get("stk_cd") or item.get("shtn_iscd") or "").strip()
            name = str(item.get("hts_kor_isnm") or item.get("stk_nm") or "").strip()
            _add(code, name, source, tier)

    # ── 1순위: HTS 조건검색 최대 20개 ──
    try:
        logger.info("[추천스캔][HTS] 조건검색 호출 시작")
        cond_stocks = kiwoom.run_all_quant_conditions()
        logger.info(f"[추천스캔][HTS] 조건검색 반환: {len(cond_stocks)}종목")
        if cond_stocks:
            source_count: dict[str, int] = {}
            for item in cond_stocks:
                src = str(item.get("source", "")).replace("조건검색:", "").strip()
                if src:
                    source_count[src] = source_count.get(src, 0) + 1
            logger.info(f"[HTS-TRACE] 조건별: {source_count}")
            hts_added = 0
            for item in cond_stocks:
                if hts_added >= 20:
                    break
                if _add(item["stock_code"], item.get("stock_name", ""), item.get("source", "조건검색"), "hts"):
                    hts_added += 1
        else:
            logger.warning("[추천스캔][HTS] 조건검색 결과 0건")
    except Exception as e:
        logger.warning(f"HTS 조건검색 실패: {e}")

    hts_count = len(candidates)
    time.sleep(1)

    # ── 2순위: 조용한 축적 최대 10개 ──
    try:
        quiet = kiwoom.get_quiet_accumulation(change_threshold=3.0, max_results=10)
        _extract(quiet, "조용한 축적", "general", 10)
    except Exception as e:
        logger.warning(f"조용한 축적 조회 실패: {e}")

    time.sleep(1)

    # ── 3순위: 외인 연속 순매수 최대 10개 ──
    try:
        _extract(kiwoom.get_foreign_net_buy(), "외인 순매수", "general", 10)
    except Exception as e:
        logger.warning(f"외인 순매수 조회 실패: {e}")

    time.sleep(1)

    # ── 4순위: 거래량 급증 최대 5개 ──
    try:
        _extract(kiwoom.get_volume_surge(), "거래량 급증", "general", 5)
    except Exception as e:
        logger.warning(f"거래량 급증 조회 실패: {e}")

    time.sleep(1)

    # ── 5순위: 등락률 하위 최대 5개 ──
    try:
        _extract(kiwoom.get_decline_rank(), "눌림목 후보", "general", 5)
    except Exception as e:
        logger.warning(f"등락률 하위 조회 실패: {e}")

    logger.info(f"[스크리닝] 후보 {len(candidates)}개 발견 (HTS {hts_count}개 우선)")
    return candidates


# ═══════════════════════════ 프리필터 (AI 분석 전) ═══════════════════════════

def _prefilter_candidates(candidates: list[dict], kiwoom, lightweight: bool = False) -> list[dict]:
    """AI 분석 전에 명백히 부적합한 종목을 숫자 기반으로 제거.

    제거 조건 (1개라도 해당 시 제외):
    - 시가총액 500억 미만
    - 당일 등락률 +7% 초과 / -10% 미만
    - 일봉 RSI > 65
    - MA5 < MA20 × 0.99 (1% 이상 역배열)
    - 거래량 비율 10배 초과 (이미 급등 소진 구간)
    - 4일 연속 양봉 (과열)
    """
    from worker.indicators import calculate_rsi

    # ── 시장 상황 조회 → 임계값 동적 조정 ──
    _pf = _PREFILTER_CONFIG
    _change_upper    = float(_pf.get("change_upper", 7))
    _change_lower    = float(_pf.get("change_lower", -10))
    _rsi_max         = float(_pf.get("rsi_max", 70))
    _ma_ratio        = float(_pf.get("ma_ratio", 0.97))
    _vol_ratio_max   = float(_pf.get("volume_ratio_max", 15))
    _consec          = int(_pf.get("consecutive_candles", 5))

    _bull_thr  = float(_pf.get("market_bull_threshold", 2.0))
    _bear_thr  = float(_pf.get("market_bear_threshold", -2.0))
    _market_state = "normal"
    try:
        def _rate(d):
            try: return float(str(d.get("flu_rt") or d.get("prdy_ctrt") or "0").replace(",", ""))
            except: return 0.0
        _kospi_rate  = _rate(kiwoom.get_market_index("kospi"))
        time.sleep(0.5)
        _kosdaq_rate = _rate(kiwoom.get_market_index("kosdaq"))
        _market_rate = max(_kospi_rate, _kosdaq_rate)  # 둘 중 높은 쪽 기준
        _skip_thr = float(_pf.get("skip_bull_threshold", 5.0))
        if _market_rate >= _skip_thr:
            logger.info(
                f"[프리필터] 시장 급등일 (KOSPI {_kospi_rate:+.1f}% / KOSDAQ {_kosdaq_rate:+.1f}%) "
                f"— 스크리닝 생략 (기준 +{_skip_thr}%)"
            )
            return []
        if _market_rate >= _bull_thr:
            _market_state = "bull"
            _change_upper += float(_pf.get("bull_change_upper_add", 4.0))
            _consec        = max(1, _consec - int(_pf.get("bull_consecutive_sub", 2)))
        elif _market_rate <= _bear_thr:
            _market_state = "bear"
            _change_lower -= float(_pf.get("bear_change_lower_sub", 3.0))
            _rsi_max      += float(_pf.get("bear_rsi_max_add", 5.0))
        logger.info(
            f"[프리필터] 시장 상태: {_market_state} "
            f"(KOSPI {_kospi_rate:+.1f}% / KOSDAQ {_kosdaq_rate:+.1f}%) → "
            f"등락률 상단 {_change_upper:.0f}% / 연속양봉 {_consec}일 / RSI {_rsi_max:.0f}"
        )
    except Exception as _e:
        logger.warning(f"[프리필터] 시장 지수 조회 실패, 기본값 사용: {_e}")

    passed = []
    reject_counts = {"시총": 0, "등락률_상단": 0, "등락률_하단": 0, "RSI": 0, "MA역배열": 0, "거래량과열": 0, "연속양봉": 0}
    time.sleep(1)  # 후보 수집 후 대기
    for cand in candidates:
        code = cand["stock_code"]
        try:
            # 현재가 조회
            price_data = kiwoom.get_current_price(code)
            cur_prc = abs(int(str(
                price_data.get("cur_prc") or price_data.get("stk_prpr") or "0"
            ).replace(",", "")))
            if not cur_prc:
                continue

            # 시가총액 (억 원)
            mkt_cap_raw = str(price_data.get("stk_amt") or price_data.get("hts_avls") or "0").replace(",", "")
            try:
                mkt_cap = abs(int(float(mkt_cap_raw)))
            except Exception:
                mkt_cap = 0
            _mkt_cap_min = int(_PREFILTER_CONFIG.get("market_cap_min", 500))
            if mkt_cap > 0 and mkt_cap < _mkt_cap_min:
                logger.debug(f"[프리필터] {cand['stock_name']}: 시총 {mkt_cap}억 < {_mkt_cap_min}억 → 제외")
                reject_counts["시총"] += 1
                continue

            # 등락률 제외 (시장 상황 반영 동적 기준)
            change_str = str(price_data.get("flu_rt") or price_data.get("prdy_ctrt") or "0").replace(",", "")
            try:
                change_pct = float(change_str)
            except Exception:
                change_pct = 0
            if change_pct > _change_upper:
                logger.debug(f"[프리필터] {cand['stock_name']}: 등락률 {change_pct:+.1f}% > +{_change_upper}% → 제외")
                reject_counts["등락률_상단"] += 1
                continue
            if change_pct < _change_lower:
                logger.debug(f"[프리필터] {cand['stock_name']}: 등락률 {change_pct:+.1f}% < {_change_lower}% → 제외")
                reject_counts["등락률_하단"] += 1
                continue

            # 일봉 데이터로 RSI / MA / 거래량비율 / 연속양봉 체크 (HTS 포함 전체)
            time.sleep(1)
            daily = kiwoom.get_daily_ohlcv(code, period=25)
            closes, volumes, opens = [], [], []
            for d in daily:
                cp = abs(int(str(d.get("cur_prc", "0")).replace(",", "") or "0"))
                op = abs(int(str(d.get("opn_prc", "0")).replace(",", "") or "0"))
                vol = abs(int(str(d.get("acc_trd_vol", "0")).replace(",", "") or "0"))
                if cp:
                    closes.append(cp)
                    volumes.append(vol)
                    opens.append(op)

            # RSI 상단 초과 제외 (시장 상황 반영)
            if len(closes) >= 15:
                rsi = calculate_rsi(closes)
                if rsi and rsi > _rsi_max:
                    logger.debug(f"[프리필터] {cand['stock_name']}: RSI {rsi:.1f} > {_rsi_max} → 제외")
                    reject_counts["RSI"] += 1
                    continue

            # MA 역배열 제외
            if len(closes) >= 20:
                ma5 = sum(closes[:5]) / 5
                ma20 = sum(closes[:20]) / 20
                if ma5 < ma20 * _ma_ratio:
                    logger.debug(f"[프리필터] {cand['stock_name']}: MA5 < MA20×{_ma_ratio} 역배열 → 제외")
                    reject_counts["MA역배열"] += 1
                    continue

            # 거래량 비율 상단 초과 제외
            if len(volumes) >= 21:
                avg_vol = sum(volumes[1:21]) / 20
                if avg_vol > 0 and volumes[0] > avg_vol * _vol_ratio_max:
                    logger.debug(f"[프리필터] {cand['stock_name']}: 거래량 {volumes[0]/avg_vol:.1f}배 > {_vol_ratio_max}배 → 제외")
                    reject_counts["거래량과열"] += 1
                    continue

            # 연속 양봉 제외 (시장 상황 반영)
            if len(closes) >= _consec and len(opens) >= _consec:
                if all(closes[i] > opens[i] for i in range(_consec)):
                    logger.debug(f"[프리필터] {cand['stock_name']}: {_consec}일 연속 양봉 → 제외")
                    reject_counts["연속양봉"] += 1
                    continue

            passed.append(cand)
            time.sleep(1)

        except Exception as e:
            logger.debug(f"[프리필터] {cand['stock_name']}: 조회 실패 ({e}) → 유지")
            passed.append(cand)
            time.sleep(3)  # 429 회복 대기

    active = {k: v for k, v in reject_counts.items() if v > 0}
    logger.info(f"[프리필터] {len(candidates)}개 → {len(passed)}개 통과 | 탈락 사유: {active}")
    return passed


# ═══════════════════════════ 장중 스캔 ═══════════════════════════

def run_intraday_scan():
    """장중 스캔: 거래량 급증 + HTS 조건검색 → AI 분석 → 모드에 따라 등록/승인.

    후보 수집: ka10023(거래량 급증) + HTS 조건검색(퀀트_ 전체)
    AI 분석 후 자동 모드면 watchlist 등록, 수동이면 승인 버튼
    """
    from worker.clients.kiwoom_client import KiwoomClient
    from data.db import get_watchlist, get_cooldown, set_cooldown, save_screening_log
    from notifications.telegram import send_message, send_message_with_inline_buttons

    kiwoom = KiwoomClient()
    existing_codes = {s["code"] for s in get_watchlist()}
    auto_mode = _is_auto_mode()
    market_text = _build_market_text(kiwoom)

    candidates = []
    seen_codes = set()

    # 1. 거래량 급증 (ka10023)
    try:
        for item in kiwoom.get_volume_surge()[:15]:
            code = str(item.get("stk_cd") or item.get("shtn_iscd") or "").strip()
            name = str(item.get("hts_kor_isnm") or item.get("stk_nm") or "").strip()
            if not code or code in existing_codes or len(code) != 6 or code in seen_codes:
                continue
            seen_codes.add(code)
            candidates.append({"stock_code": code, "stock_name": name, "source": "거래량 급증"})
    except Exception as e:
        logger.warning(f"장중 스캔 거래량 조회 실패: {e}")

    time.sleep(1)

    # 2. HTS 조건검색 (퀀트_ 전체)
    try:
        logger.info("[장중스캔][HTS] 조건검색 호출 시작 (ka10171 -> ka10172)")
        cond_stocks = kiwoom.run_all_quant_conditions()
        logger.info(f"[장중스캔][HTS] 조건검색 반환 종목 수: {len(cond_stocks)}")
        if cond_stocks:
            source_count = {}
            for item in cond_stocks:
                source = str(item.get("source", ""))
                source_name = source.replace("조건검색:", "").strip() if source else ""
                if source_name:
                    source_count[source_name] = source_count.get(source_name, 0) + 1
            logger.info(f"[HTS-TRACE] ka10172_by_condition={source_count}")
            source_names = sorted(
                {
                    str(item.get("source", "")).replace("조건검색:", "").strip()
                    for item in cond_stocks
                    if item.get("source")
                }
            )
            logger.info(
                f"[장중스캔][HTS] 사용된 조건식 수: {len(source_names)} "
                f"(샘플: {', '.join(source_names[:5])})"
            )
        else:
            logger.warning("[장중스캔][HTS] 조건검색 결과가 0건입니다.")
        hts_added = 0
        for item in cond_stocks:
            if hts_added >= 20:
                break
            code = item["stock_code"]
            if code not in existing_codes and code not in seen_codes:
                seen_codes.add(code)
                candidates.append(item)
                hts_added += 1
    except Exception as e:
        logger.warning(f"장중 스캔 HTS 조건검색 실패: {e}")

    if not candidates:
        logger.info("[장중 스캔] 후보 없음")
        return

    # 쿨다운 필터 (같은 종목 하루 1번)
    filtered = []
    for cand in candidates:
        key = f"intraday_scan:{cand['stock_code']}"
        next_allowed_at = get_cooldown(key)
        if next_allowed_at and now_kst() < next_allowed_at:  # 12시간
            continue
        set_cooldown(key, cooldown_minutes=12 * 60)
        filtered.append(cand)

    if not filtered:
        logger.info("[장중 스캔] 후보 전부 쿨다운 중")
        return

    # 프리필터: 풀 모드 (시총+등락률+RSI+MA)
    filtered = _prefilter_candidates(filtered, kiwoom)
    if not filtered:
        logger.info("[장중 스캔] 프리필터 후 후보 없음")
        return

    ai_target = filtered
    logger.info(f"[장중 스캔] 프리필터 통과 {len(ai_target)}개 → AI 분석 시작")

    added = []
    pending = []
    result_counts = {}  # 판정별 집계
    for cand in ai_target:
        try:
            analysis = _analyze_candidate(
                cand["stock_code"], cand["stock_name"],
                kiwoom=kiwoom, market_text=market_text,
            )
            rec = analysis.get("recommendation", "분석 실패")
            rr = analysis.get("rr_ratio", "N/A")
            reason = str(analysis.get("reason", "") or "").replace("\n", " ").strip()
            result_counts[rec] = result_counts.get(rec, 0) + 1
            logger.info(
                f"[장중 스캔] {cand['stock_name']} ({cand['stock_code']}) "
                f"→ {rec} (R/R={rr})"
            )

            # screening_log 저장
            try:
                log_id = save_screening_log(
                    stock_code=cand["stock_code"],
                    stock_name=cand["stock_name"],
                    source=cand["source"],
                    recommendation=rec,
                    reason=reason,
                    met_conditions=analysis.get("met_conditions"),
                    rr_ratio=float(rr) if rr and rr != "N/A" else None,
                    current_price=analysis.get("_current_price"),
                    dart_summary=analysis.get("_dart_summary"),
                    news_summary=analysis.get("_news_summary"),
                    market_snapshot=market_text,
                    ai_response=analysis.get("_ai_response"),
                )
            except Exception:
                log_id = None

            if rec != "관심종목 등록":
                time.sleep(2)
                continue

            alert_text = _format_screening_alert(
                cand["stock_name"], cand["stock_code"], cand["source"], analysis,
            )
            alert_text = f"📡 *[장중 스캔]*\n{alert_text}"

            if auto_mode:
                add_to_watchlist(cand["stock_code"], cand["stock_name"], analysis)
                ok = send_message(f"{alert_text}\n\n✅ *자동 관심종목 등록 완료*")
                if not ok:
                    logger.warning(f"[장중 스캔] {cand['stock_name']} 텔레그램 알림 발송 실패")
                added.append(cand["stock_name"])
                if log_id:
                    try:
                        from data.db import update_screening_action
                        update_screening_action(log_id, "auto_accepted")
                    except Exception:
                        pass
            else:
                buttons = [[
                    {"text": "✅ 관심종목 등록", "callback_data": f"screen_add:{cand['stock_code']}"},
                    {"text": "❌ 패스", "callback_data": f"screen_pass:{cand['stock_code']}"},
                ]]
                _pending_screenings[cand["stock_code"]] = {
                    "stock_name": cand["stock_name"],
                    "analysis": analysis,
                    "log_id": log_id,
                }
                msg_id = send_message_with_inline_buttons(alert_text, buttons)
                if msg_id is None:
                    logger.warning(f"[장중 스캔] {cand['stock_name']} 텔레그램 버튼 알림 발송 실패")
                pending.append(cand["stock_name"])

            time.sleep(2)
        except Exception as e:
            logger.error(f"[장중 스캔] {cand['stock_name']} 분석 실패: {e}")

    # AI 분석 집계
    counts_str = ", ".join(f"{k}={v}" for k, v in sorted(result_counts.items()))
    logger.info(f"[장중 스캔] AI 분석 완료 ({len(ai_target)}개): {counts_str}")
    if added:
        logger.info(f"[장중 스캔] {len(added)}개 자동 등록: {', '.join(added)}")
    if pending:
        logger.info(f"[장중 스캔] {len(pending)}개 승인 대기")


# ═══════════════════════════ 스크리닝 AI 시스템 프롬프트 (캐싱) ═══════════════════════════

_SCREENING_KNOWLEDGE = """## 트레이딩 분석 지식 — 종목 스크리닝용

### 1. 캔들 분석
[캔들 구조] 양봉=매수세 우위, 음봉=매도세 우위. 긴 아랫꼬리=매도 후 매수 반등. 긴 윗꼬리=매수 후 매도 압력.
[주요 패턴]
- 망치형: 하락 후 긴 아랫꼬리 양봉 → 반등 신호
- 역망치형: 상승 후 긴 윗꼬리 음봉 → 조정 신호
- 도지: 시가≈종가 → 추세 전환 초기 신호
- 불리시 엔걸핑: 음봉 후 감싸는 양봉 → 강한 상승 전환
- 베어리시 엔걸핑: 양봉 후 감싸는 음봉 → 강한 하락 전환

### 2. 거래량 분석
가격↑ + 거래량↑ = 강한 매수세 (추세 강화)
가격↑ + 거래량↓ = 매수세 약화 (신뢰도 낮음)
가격↓ + 거래량↑ = 강한 매도세 (추가 하락 가능)
가격↓ + 거래량↓ = 매도압력 약화 (반등 가능)
[볼륨 스프레드] 큰 몸통+높은 거래량=강한 추세 / 작은 몸통+높은 거래량=전환 가능 / 큰 몸통+낮은 거래량=속임수 가능
[OBV] 상승=매수세 우위, 하락=매도세 우위. 가격과 불일치=다이버전스

### 3. 추세·지지·저항
상승 추세: 고점·저점 점진 상승. 하락 추세: 고점·저점 점진 하락.
과거 고점=저항, 과거 저점=지지. 거래량 집중 가격대=강한 지지/저항.
저항 돌파 후 되돌림 성공 → 지지 전환. 지지 이탈 후 반등 실패 → 저항 전환.
돌파 시 거래량 급증 동반 = 신뢰도 높음.

### 4. 이동평균선(MA)
현재가 > MA5 > MA20: 강한 상승 (매수 유리)
MA5 > MA20 + 현재가 < MA5: 단기 눌림목 (지지 확인 후 entry 유리)
현재가 < MA5 < MA20: 강한 하락 (매수 신중)
골든크로스: 단기MA > 장기MA 전환 → 상승 추세 시작
데드크로스: 단기MA < 장기MA 전환 → 하락 추세 시작

### 5. RSI
RSI ≤ 30: 극과매도, 강한 반등 가능
RSI 31~43: 과매도, entry 신뢰도 높음
RSI 44~55: 중립
RSI 56~65: 과매수 접근
RSI ≥ 66: 과매수
[다이버전스] 강세: 가격 신저점 + RSI 저점 상승 → 반등 임박. 약세: 가격 신고점 + RSI 고점 하락 → 조정 임박.

### 6. 스토캐스틱·CCI·일목균형표
스토캐스틱: %K/%D 교차. 과매수≥80, 과매도≤20. 과매도에서 골든크로스=강한 매수 신호.
CCI: <-100 반등=매수 신호. >+100 하락=매도 신호.
일목: 구름대 위=상승, 아래=하락, 내부=중립. 전환선>기준선=매수 신호. 두꺼운 구름대=강한 지지/저항.

### 7. 차트 패턴
[반전] 이중 바닥→상승 전환 / 이중 천장→하락 전환 / 헤드앤숄더→하락 / 역 헤드앤숄더→상승
[지속] 상승 삼각형→돌파 시 강한 상승 / 깃발형→추세 연장 / 컵 위드 핸들→돌파 시 상승
[패턴+거래량] 돌파 시 거래량 급증 동반 = 신뢰도 높음

### 8. 피보나치
되돌림: 38.2~61.8% = 정상 되돌림, 매수 구간. 78.6% 초과 = 추세 전환 가능.
확장: 127.2%, 161.8%, 200% — 목표가 설정에 활용.
피보나치 지지 + MA 지지 + 거래량 증가 동시 = 높은 신뢰도 매수 포인트.

### 9. 공시 분석 (DART)
수주/계약=매출 성장 기대 / 실적 발표=서프라이즈/쇼크 판단 / 유증/CB/BW=주식 희석 단기 악재
호재 공시 + RSI 과매도 + 거래량 급증 = 강한 entry 신호.
악재 공시 + MA 하향 이탈 = 부적합 종목."""


_SCREENING_SYSTEM_PROMPT = f"""당신은 개인 투자자의 퀀트 트레이딩 시스템에서 신규 종목 편입 적합성을 판단하는 스크리닝 AI입니다.
후보 종목의 기술적 지표, 재무, 공시, 뉴스를 분석하여 편입 여부와 초기 전략을 제안합니다.

{_SCREENING_KNOWLEDGE}

## 편입 조건 5가지
- 2가지 이상 충족: 강한 편입 후보
- 1가지 강한 충족 + 다른 조건 부분 충족(근접하거나 일부 지표 해당): 편입 가능
- 1가지만 약하게 충족: 보류
1. **눌림목**: 상승 추세 중 조정 구간에 진입한 종목
   - MA20 지지선 근처 (현재가가 MA20 ± 3% 이내)
   - RSI 38~50 구간 (과매도 진입 또는 진입 직전)
   - 피보나치 38.2~61.8% 되돌림 구간
   - 거래량 감소 중 (매도세 약화 = 반등 가능)
   → 3개 이상 동시 충족 시 강한 눌림목

2. **저평가**: 펀더멘털 대비 주가가 낮은 종목
   - PER이 동종업계 평균 대비 낮음 (또는 절대 PER < 10)
   - PBR < 1 (자산가치 대비 저평가)
   - 영업이익률 양호한데 주가 하락 중
   → 재무제표 데이터로 판단. 데이터 없으면 이 조건 평가 불가로 처리

3. **테마 미반영**: 호재가 있으나 주가에 반영되지 않은 종목
   - 최근 공시에 수주/계약/신사업/정책 수혜 내용이 있으나 주가 횡보/하락
   - 뉴스에 긍정적 이슈가 있으나 거래량 미동반
   → 공시/뉴스 데이터 없으면 이 조건 평가 불가로 처리

4. **실적 개선**: 매출/영업이익이 증가 추세인 종목
   - 최근 2~3분기 매출 또는 영업이익 연속 증가
   - 적자→흑자 전환 또는 흑자 폭 확대
   → 재무제표 데이터로 판단. 데이터 없으면 이 조건 평가 불가로 처리

5. **잠재 성장**: 아직 주가에 반영되지 않았으나 상승 잠재력이 있는 종목
   - 외인/기관 순매수 지속 중 (스마트머니 유입 징후)
   - 거래량 서서히 증가하나 주가는 아직 횡보 (축적 단계)
   - 실적 개선 추세인데 주가 반응 없음 (시장 무관심)
   - 구름대 내부에서 전환선 골든크로스 직전 또는 MA 정배열 전환 초기
   - RSI 45~55 중립 구간에서 바닥 다지는 중
   → 급등 종목 제외 (당일 등락률 +5% 초과 시 이 조건으로 평가 금지)
   → 이 조건은 "향후 1~2주 내 움직임 가능성"을 판단하는 것. 이미 오른 종목의 추격 매수와 구별할 것.

## 부적합 필터 (1개라도 해당 시 즉시 부적합)
- 데드크로스 발생 중 (MA5 < MA20 + 하향 진행)
- RSI > 70 (이미 과매수)
- 거래량 없이 급등 (속임수 가능)
- 악재 공시 발견 (유증, CB, 관리종목 등)
- 시가총액 500억 미만 (유동성 리스크)

## 목표가·손절가 설정 기준
- 목표가: 피보나치 확장 127.2~161.8% 또는 직전 고점 저항선 기준
- 손절가: 평단 대비 -5~-10%. 최소한 직전 지지선 아래로 설정
- R/R 1.5:1 이상 확보 필수 (목표 수익폭 ≥ 손절 손실폭 × 1.5)
- R/R 1.5:1 미만이면 편입 보류 권고
- R/R 2:1 이상이면 우선 편입 대상

## RSI 임계값 설정 기준
- rsi_oversold: 일봉 RSI14 기준. 종목 변동성에 따라 38~43 범위.
  변동성 높은 종목=38~40 / 안정적 종목=41~43
- rsi_overbought: 60~75 범위.
  스윙 매매(중기)=65~70 / 단기 매매=60~65

## horizon 설정 기준
- 단기: 급등 후 조정 종목, 거래량 급증 동반, 뉴스/이벤트 기반
- 중기: 추세 전환 초기, 실적 개선 초기, 눌림목 진입
- 장기: 저평가 가치주, 성장주 초기 진입

## 활성화 가능한 모니터링 조건 (29개)
아래에서 종목 특성에 맞는 조건만 활성화하고, 각 조건에 대해 왜 활성화/비활성화했는지 근거를 제시하세요.

[가격 조건]
- target_price: 목표가 도달 알림 (exit) — 목표가 설정 시 필수 활성화
- stop_loss_price: 손절가 도달 알림 (exit) — 손절가 설정 시 필수 활성화

[RSI 조건]
- rsi_oversold: RSI 과매도 (entry) — 값: 38~43
- rsi_overbought: RSI 과매수 (exit) — 값: 60~75
- rsi_oversold_intraday: RSI 5분봉 과매도 (entry, 단기만) — 값: 30~35
- rsi_critical: RSI 극단적 과매도 경고 (both) — 값: 25~30
- rsi_oversold_add: RSI 과매도 물타기 (add)

[이동평균 조건]
- golden_cross: MA 골든크로스 (entry)
- death_cross: MA 데드크로스 (exit)
- ma20_support_break: MA20 하향 이탈 (exit)
- ma5_support_break: MA5 하향 이탈 (both)
- ma5_recovery: MA5 상향 돌파 회복 (entry)
- ma5_recovery_add: MA5 회복 추가매수 (add)
- new_high_20d: 20일 신고가 돌파 (both)

[MACD 조건]
- macd_golden_cross: MACD 골든크로스 (entry)
- macd_death_cross: MACD 데드크로스 (exit)

[볼린저 조건]
- bollinger_upper_break: 볼린저 상단 돌파 (both)
- bollinger_lower_break: 볼린저 하단 이탈 (entry)
- bollinger_lower_break_add: 볼린저 하단 물타기 (add)
- bollinger_critical_below: 볼린저 하단 3% 이탈 경고 (both)

[스토캐스틱 조건]
- stochastic_golden_cross: 스토캐스틱 골든크로스 (entry)
- stochastic_death_cross: 스토캐스틱 데드크로스 (exit)

[CCI 조건]
- cci_oversold: CCI 과매도 (entry) — 값: -100
- cci_overbought: CCI 과매수 (exit) — 값: 100

[일목균형표 조건]
- ichimoku_golden_cross: 일목 전환선 골든크로스 (entry)
- ichimoku_death_cross: 일목 전환선 데드크로스 (exit)
- ichimoku_cloud_breakout: 일목 구름대 돌파 (entry)
- ichimoku_cloud_breakdown: 일목 구름대 이탈 (exit)

[거래량 조건]
- volume_surge_ratio: 거래량 급증 (both) — 값: 1.5~3.0배

## 출력 형식 (반드시 JSON만 출력, 다른 텍스트 금지)
```json
{{
    "recommendation": "관심종목 등록" 또는 "보류" 또는 "부적합",
    "recommendation_type": "기술형" 또는 "이벤트형" 또는 "혼합형" 또는 "해당없음",
    "reason": "판단 근거 1~3문장",
    "met_conditions": ["충족된 편입조건명"],
    "disqualifiers": ["부적합 사유 (있을 때만)"],
    "target_price": 목표가(정수),
    "target_price_reason": "목표가 설정 근거 한 줄",
    "stop_loss_price": 손절가(정수),
    "stop_loss_price_reason": "손절가 설정 근거 한 줄",
    "horizon": "단기" 또는 "중기" 또는 "장기",
    "horizon_reason": "매매 기간 설정 근거 한 줄",
    "rsi_oversold": RSI 과매도 기준값(정수, 38~43),
    "rsi_oversold_reason": "과매도 기준 설정 근거 한 줄",
    "rsi_overbought": RSI 과매수 기준값(정수, 60~75),
    "rsi_overbought_reason": "과매수 기준 설정 근거 한 줄",
    "rr_ratio": 손익비(소수점 1자리),
    "enabled_conditions": {{
        "조건id": {{"enabled": true/false, "reason": "활성화/비활성화 근거 한 줄", "value": 값(해당시)}},
        ...전체 29개 조건에 대해 명시
    }}
}}
```

판단 원칙:
- 수치 기반 판단만 허용. "느낌", "분위기"로 판단 금지.
- 관심종목 추천은 "지금 당장 매수"가 아니라 "모니터링 가치" 판단이다.
- 기술형: 차트 구조, 추세, 거래량, RSI, 지지/저항이 좋아서 추적할 가치가 있는 종목.
- 이벤트형: 뉴스/공시/재료가 강해서 추적할 가치가 있는 종목. 단, 이미 과열되었으면 보류 가능.
- 혼합형: 기술적 매력과 이벤트 재료가 동시에 의미 있는 종목.
- recommendation_type에 맞춰 어떤 조건을 중점 모니터링할지 enabled_conditions와 임계값에 반영할 것.
- 기술형/혼합형에서는 차트·수급·리스크를 우선하고, 이벤트형에서만 뉴스/공시 비중을 높일 것.
- 뉴스/공시는 실적 쇼크, 유상증자, 수주/계약, 거래정지, 규제, 소송 등 가격 영향이 큰 특수 상황일 때만 상위 근거로 반영.
- 일반 기사, 테마성 기사, 반복 기사, 시장 해설은 보조 참고사항으로만 보고 과대평가하지 말 것.
- 데이터 부족 시 해당 조건은 "평가 불가"로 처리하고, 나머지 조건으로 판단.
- 균형 잡힌 시각으로 판단. 명확한 부적합 사유가 없고 1개 이상 강한 편입 조건이 있으면 편입 고려.
- 데이터 부족만으로 보류하지 말 것. 확인 가능한 조건들로 판단.
- 관심종목 등록 시 반드시 목표가·손절가·R/R을 수치로 제시.
- target_price, stop_loss_price는 반드시 원(KRW) 단위 정수로 출력. 예: 51000 (O), 51 (X), 5.1만 (X), "51,000" (X)
- 각 임계값과 조건 활성화의 설정 근거를 반드시 한 줄로 명시.
- target_price, stop_loss_price는 관심종목 등록 시 반드시 활성화.
- 단기 종목이 아니면 rsi_oversold_intraday 비활성화.
- 불필요한 add 조건은 초기 등록 시 비활성화 (보유 전이므로)."""


def _build_screening_insights() -> str:
    """과거 스크리닝 성과 + 최근 daily review 중 스크리닝 관련 인사이트 (3줄 이내)."""
    try:
        from data.db import get_screening_accuracy, get_recent_daily_reviews
        parts = []

        acc = get_screening_accuracy(days=30)
        if acc and acc.get("total", 0) >= 3:
            parts.append(
                f"30일 추천 {acc['total']}건: "
                f"7일적중 {acc.get('hit_7d', 'N/A')}% (평균{acc.get('avg_7d', 'N/A')}%) / "
                f"30일적중 {acc.get('hit_30d', 'N/A')}% (평균{acc.get('avg_30d', 'N/A')}%)"
            )

        reviews = get_recent_daily_reviews(limit=1)
        if reviews:
            detail = reviews[0].get("detail", "")
            for line in detail.splitlines():
                stripped = line.strip()
                if stripped.startswith("[스크리닝평가]") or stripped.startswith("[개선제안]"):
                    parts.append(stripped)

        return "\n".join(parts) if parts else "데이터 부족"
    except Exception:
        return "조회 실패"


# ═══════════════════════════ AI 편입 분석 ═══════════════════════════

def _analyze_candidate(stock_code: str, stock_name: str, kiwoom=None, market_text: str | None = None) -> dict:
    """후보 종목 1개를 차트+공시+뉴스로 분석하여 편입 적합성 판단.

    Returns:
        {recommendation, reason, target_price, stop_loss_price, horizon, rsi_oversold, rsi_overbought}
    """
    from worker.clients.kiwoom_client import KiwoomClient
    from worker.indicators import calculate_rsi, calculate_volume_ratio, calculate_chart_summary

    if kiwoom is None:
        kiwoom = KiwoomClient()

    # 1. 현재가 + 차트 데이터
    price_data = kiwoom.get_current_price(stock_code)
    current_price = abs(int(str(
        price_data.get("cur_prc") or price_data.get("stk_prpr") or price_data.get("prpr") or "0"
    ).replace(",", "")))

    if not stock_name:
        stock_name = price_data.get("hts_kor_isnm") or stock_code

    daily_data = kiwoom.get_daily_ohlcv(stock_code, period=90)
    close_prices, high_prices, low_prices, open_prices, volumes = [], [], [], [], []
    for d in daily_data:
        cp = abs(int(str(d.get("cur_prc", "0")).replace(",", "") or "0"))
        hp = abs(int(str(d.get("high_pric", "0")).replace(",", "") or "0"))
        lp = abs(int(str(d.get("lwst_pric", "0") or d.get("low_pric", "0")).replace(",", "") or "0"))
        op = abs(int(str(d.get("strt_pric", "0") or d.get("opn_pric", "0")).replace(",", "") or "0"))
        vol = abs(int(str(d.get("trde_qty", "0")).replace(",", "") or "0"))
        if cp: close_prices.append(cp)
        if hp: high_prices.append(hp)
        if lp: low_prices.append(lp)
        if op: open_prices.append(op)
        if vol: volumes.append(vol)

    rsi = calculate_rsi(close_prices) if len(close_prices) >= 15 else None
    volume_ratio = calculate_volume_ratio(volumes) if len(volumes) >= 21 else None
    chart = calculate_chart_summary(
        close_prices, high_prices, current_price,
        low_prices=low_prices, open_prices=open_prices, volumes=volumes,
    ) if len(close_prices) >= 5 else None

    if not chart or not _ai_client:
        return {"recommendation": "분석 불가"}

    # 2. DART 공시 + 재무
    dart_text = ""
    try:
        from worker.clients.dart_client import format_full_context_for_ai, DART_API_KEY
        if DART_API_KEY:
            dart_text = format_full_context_for_ai(stock_code)
    except Exception:
        pass

    # 3. 뉴스
    news_text = ""
    try:
        from worker.clients.news_client import format_news_for_ai, NAVER_CLIENT_ID
        if NAVER_CLIENT_ID:
            news_text = format_news_for_ai(stock_name, max_items=5)
    except Exception:
        pass

    # 4. 포트폴리오 맥락 (현재 보유 종목 수, 현금 비중 등)
    portfolio_context = ""
    try:
        from data.db import get_conn
        with get_conn() as conn:
            watchlist_count = conn.execute("SELECT COUNT(*) FROM watchlist WHERE enabled = 1").fetchone()[0]
            portfolio_rows = conn.execute("SELECT stock_name, profit_rate FROM portfolio").fetchall()
        holding_count = len(portfolio_rows)
        if portfolio_rows:
            holdings_summary = ", ".join(
                f"{r['stock_name']}({r['profit_rate']:+.1f}%)" for r in portfolio_rows[:10]
            )
            portfolio_context = (
                f"현재 보유: {holding_count}종목 ({holdings_summary})\n"
                f"관심종목: {watchlist_count}개"
            )
        else:
            portfolio_context = f"현재 보유 없음 / 관심종목 {watchlist_count}개"
    except Exception:
        portfolio_context = "포트폴리오 조회 실패"

    # 5. 시장 환경
    if not market_text:
        market_text = _build_market_text(kiwoom)

    # 6. 차트 분석 텍스트 (claude_judge.py 수준)
    stoch_text = ""
    if chart.stochastic_k is not None:
        stoch_level = ""
        if chart.stochastic_k >= 80: stoch_level = " (과매수)"
        elif chart.stochastic_k <= 20: stoch_level = " (과매도)"
        cross = ""
        if getattr(chart, 'stochastic_golden_cross', False): cross = " ★골든크로스"
        elif getattr(chart, 'stochastic_death_cross', False): cross = " ★데드크로스"
        d_str = f"/ %D {chart.stochastic_d:.0f}" if chart.stochastic_d is not None else ""
        stoch_text = f"%K {chart.stochastic_k:.0f} {d_str}{stoch_level}{cross}"

    cci_text = ""
    if chart.cci is not None:
        cci_level = ""
        if chart.cci > 100: cci_level = " (과매수)"
        elif chart.cci < -100: cci_level = " (과매도)"
        cci_text = f"{chart.cci:.0f}{cci_level}"

    ichimoku_parts = []
    if chart.ichimoku_above_cloud is True:
        ichimoku_parts.append("구름대 위 (상승)")
    elif chart.ichimoku_above_cloud is False:
        ichimoku_parts.append("구름대 아래 (하락)")
    else:
        ichimoku_parts.append("구름대 내부 (중립)")
    if getattr(chart, 'ichimoku_cloud_thickness', None) is not None:
        label = "두꺼움" if chart.ichimoku_cloud_thickness > 3 else "얇음" if chart.ichimoku_cloud_thickness < 1 else "보통"
        ichimoku_parts.append(f"두께 {chart.ichimoku_cloud_thickness:.1f}% [{label}]")

    fib_text = ""
    if chart.fibonacci:
        f = chart.fibonacci
        levels = [
            ("23.6%", _fmt_int(f.get("fib_236"), 0)),
            ("38.2%", _fmt_int(f.get("fib_382"), 0)),
            ("50%", _fmt_int(f.get("fib_500"), 0)),
            ("61.8%", _fmt_int(f.get("fib_618"), 0)),
        ]
        nearest = min(levels, key=lambda x: abs(x[1] - current_price))
        swing_high = _fmt_int(f.get("swing_high"), 0)
        swing_low = _fmt_int(f.get("swing_low"), 0)
        ext_1272 = _fmt_int(f.get("ext_1272"), 0)
        ext_1618 = _fmt_int(f.get("ext_1618"), 0)
        fib_text = (f"고점 {swing_high:,} / 저점 {swing_low:,}"
                    f" — 근접 레벨: {nearest[0]}({nearest[1]:,})"
                    f" | 확장: 127.2%={ext_1272:,} / 161.8%={ext_1618:,}")

    # MA20 대비 거리 (눌림목 판단용)
    ma20_dist = ""
    if chart.ma20:
        dist_pct = (current_price - chart.ma20) / chart.ma20 * 100
        ma20_dist = f" (MA20 대비 {dist_pct:+.1f}%)"

    # 연속 하락일
    consecutive_down = 0
    prices_with_cur = (list(getattr(chart, 'recent_10d_prices', []) or []) + [current_price])
    if len(prices_with_cur) >= 2:
        for i in range(len(prices_with_cur) - 1, 0, -1):
            if prices_with_cur[i] < prices_with_cur[i - 1]:
                consecutive_down += 1
            else:
                break

    # 7. 유저 프롬프트 조립
    ma5_val = _fmt_int(getattr(chart, "ma5", None), 0)
    ma20_val = _fmt_int(getattr(chart, "ma20", None), 0)
    support_val = _fmt_int(getattr(chart, "support_level", None), 0)
    resist_val = _fmt_int(getattr(chart, "resistance_level", None), 0)
    macd_line = getattr(chart, "macd_line", None)
    macd_signal = getattr(chart, "macd_signal", None)
    macd_line_text = f"{macd_line:.0f}" if isinstance(macd_line, (int, float)) else "N/A"
    macd_signal_text = f"{macd_signal:.0f}" if isinstance(macd_signal, (int, float)) else "N/A"

    user_prompt = f"""## 종목 정보
- 종목: {stock_name} ({stock_code})
- 현재가: {current_price:,}원

## 기술적 지표
- MA5: {ma5_val:,}원 / MA20: {ma20_val:,}원{ma20_dist}
- 추세: {chart.trend} (현재가 MA5 {'위' if chart.above_ma5 else '아래'} / MA20 {'위' if chart.above_ma20 else '아래'})
- RSI(14): {rsi if rsi else 'N/A'}
- 거래량 배율: {f'{volume_ratio:.1f}배' if volume_ratio else 'N/A'}
- 스토캐스틱: {stoch_text or 'N/A'}
- CCI: {cci_text or 'N/A'}
- 일목균형표: {' / '.join(ichimoku_parts)}
- MACD: {macd_line_text} / Signal: {macd_signal_text}
- 볼린저: 상단 {int(chart.bollinger_upper or 0):,} / 하단 {int(chart.bollinger_lower or 0):,}
- OBV: {chart.obv_trend or 'N/A'}
- 지지: {support_val:,}원 / 저항: {resist_val:,}원
- 피보나치: {fib_text or 'N/A'}
- RSI 다이버전스: {chart.rsi_divergence or '없음'}
- MACD 다이버전스: {chart.macd_divergence or '없음'}
- 캔들 패턴: {', '.join(chart.candle_patterns) if chart.candle_patterns else '없음'}
- 차트 패턴: {', '.join(chart.chart_patterns) if chart.chart_patterns else '없음'}
- 볼륨 스프레드: {getattr(chart, 'volume_spread', '없음') or '없음'}
- 거래량 추세: {getattr(chart, 'volume_price_trend', '없음') or '없음'}
- 연속 하락: {consecutive_down}일
- 5일 전 대비: {f'{chart.price_change_5d:+.2f}%' if chart.price_change_5d is not None else 'N/A'}

## DART 공시/재무
{dart_text or '데이터 없음'}

## 최근 뉴스
{news_text or '데이터 없음'}

## 포트폴리오 현황
{portfolio_context}

## 시장 환경
{market_text}

## 과거 스크리닝 성과 (자기 보정용)
{_build_screening_insights()}"""

    try:
        if _AI_BACKEND == "anthropic":
            response = _ai_client.messages.create(
                model=_AI_MODEL, max_tokens=1000,
                system=[
                    {
                        "type": "text",
                        "text": _SCREENING_SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_prompt}],
            )
            ai_text = response.content[0].text
        else:
            response = _ai_client.chat.completions.create(
                model=_AI_MODEL, max_tokens=1000,
                messages=[
                    {"role": "system", "content": _SCREENING_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
            )
            ai_text = response.choices[0].message.content

        # 중첩 JSON(enabled_conditions) 포함 응답 파싱
        json_text = _extract_json_block(ai_text)
        if json_text:
            try:
                result = json.loads(json_text)
            except Exception:
                # 1차 파싱 실패 시 JSON 정규화 재요청 후 재시도
                repaired = _repair_screening_json(ai_text)
                repaired_block = _extract_json_block(repaired) or repaired.strip()
                try:
                    result = json.loads(repaired_block)
                except Exception:
                    logger.warning(f"JSON 파싱 최종 실패: {repaired_block[:200]}")
                    raise

            # R/R 1.5:1 미만이면 관심종목 등록 → 보류로 강제 변환
            rr = result.get("rr_ratio", 0)
            if result.get("recommendation") == "관심종목 등록" and rr and float(rr) < 1.5:
                logger.info(f"[스크리닝] {stock_name}: R/R {rr} < 1.5 → 보류로 변환")
                result["recommendation"] = "보류"
                result["reason"] = f"R/R {rr}:1 미달 (1.5:1 이상 필요). " + result.get("reason", "")
            # RAG용 컨텍스트 첨부
            result["_current_price"] = current_price
            result["_dart_summary"] = dart_text or None
            result["_news_summary"] = news_text or None
            result["_ai_response"] = ai_text
            return result

        return {"recommendation": "분석 실패"}

    except Exception as e:
        logger.error(f"스크리닝 AI 분석 오류 ({stock_name}): {e}")
        return {"recommendation": "분석 실패"}


# ═══════════════════════════ watchlist 추가 ═══════════════════════════

def add_to_watchlist(stock_code: str, stock_name: str, analysis: dict) -> bool:
    """분석 결과로 watchlist에 추가. 자동/수동 모드 공용."""
    from data.db import get_conn

    # AI가 제안한 조건별 활성화 설정 사용
    ai_conditions = analysis.get("enabled_conditions", {})
    conditions = {}

    # 포지션 전용 필드 — watchlist에 넣지 않음 (매수 후 positions 테이블에서 관리)
    _position_only_fields = {
        "target_price", "stop_loss_price",
        "rsi_oversold_add", "bollinger_lower_break_add", "ma5_recovery_add",
    }

    # 값이 있는 조건 (임계값 설정)
    value_fields = {
        "rsi_oversold", "rsi_overbought",
        "rsi_oversold_intraday", "rsi_critical", "volume_surge_ratio",
        "cci_oversold", "cci_overbought",
    }
    # 불리언 조건 (활성화/비활성화만)
    flag_fields = {
        "golden_cross", "death_cross", "ma20_support_break", "ma5_support_break",
        "ma5_recovery", "new_high_20d",
        "macd_golden_cross", "macd_death_cross",
        "bollinger_upper_break", "bollinger_lower_break",
        "bollinger_critical_below",
        "stochastic_golden_cross", "stochastic_death_cross",
        "ichimoku_golden_cross", "ichimoku_death_cross",
        "ichimoku_cloud_breakout", "ichimoku_cloud_breakdown",
    }

    for cond_id, cond_info in ai_conditions.items():
        if not isinstance(cond_info, dict) or not cond_info.get("enabled"):
            continue
        if cond_id in _position_only_fields:
            continue  # 매수 후 positions에서 관리
        if cond_id in value_fields:
            val = cond_info.get("value")
            if val is None:
                val = analysis.get(cond_id, 0)
            # 콤마 제거 + 숫자 변환 (AI가 "51,000" 형태로 출력할 수 있음)
            _int_fields = {"cci_oversold", "cci_overbought"}
            try:
                val = float(str(val).replace(",", "").strip())
                if cond_id in _int_fields:
                    val = int(val)
            except (ValueError, TypeError):
                logger.warning(f"[스크리닝] {stock_name} {cond_id}={val} 숫자 변환 실패 → 무시")
                continue
            conditions[cond_id] = val
        elif cond_id in flag_fields:
            conditions[cond_id] = True

    # AI가 enabled_conditions를 안 줬을 때 fallback (포지션 필드 제외)
    if not conditions:
        conditions = {
            "rsi_oversold": analysis.get("rsi_oversold", 40),
            "rsi_overbought": analysis.get("rsi_overbought", 65),
            "golden_cross": True,
            "death_cross": True,
            "volume_surge_ratio": 2.0,
            "bollinger_lower_break": True,
            "ma20_support_break": True,
        }

    horizon = analysis.get("horizon", "중기")
    conditions["horizon"] = horizon

    from data.db import upsert_stock
    upsert_stock(stock_code, stock_name, True, conditions)

    logger.info(f"[관심종목 등록] {stock_name}({stock_code}) horizon={horizon} "
                f"조건수={len(conditions)} (목표가/손절가는 매수 후 positions에서 설정)")
    return True


def _format_screening_alert(stock_name: str, stock_code: str, source: str, analysis: dict) -> str:
    """스크리닝 결과를 텔레그램 알림 텍스트로 포맷."""
    reason = analysis.get("reason", "")
    recommendation_type = analysis.get("recommendation_type", "해당없음")
    met_conditions = analysis.get("met_conditions", [])
    if not isinstance(met_conditions, list):
        met_conditions = []
    met = ", ".join(met_conditions)
    rr = analysis.get("rr_ratio", "N/A")
    target_price = _fmt_int(analysis.get("target_price"), 0)
    stop_loss_price = _fmt_int(analysis.get("stop_loss_price"), 0)
    rsi_oversold = _fmt_int(analysis.get("rsi_oversold"), 40)
    rsi_overbought = _fmt_int(analysis.get("rsi_overbought"), 65)

    lines = [
        f"🔍 *{stock_name}* ({stock_code}) — {source}",
        f"",
        f"*[AI 분석]* {met} 충족. R/R {rr}:1 / 유형: {recommendation_type}",
        f"{reason}",
        f"",
        f"📌 *제안 전략*",
        f"• 목표가 {f'{target_price:,}원' if target_price else '미설정'} _(매수 후 확정)_ — {analysis.get('target_price_reason', '')}",
        f"• 손절가 {f'{stop_loss_price:,}원' if stop_loss_price else '미설정'} _(매수 후 확정)_ — {analysis.get('stop_loss_price_reason', '')}",
        f"• RSI 과매도 {rsi_oversold} — {analysis.get('rsi_oversold_reason', '')}",
        f"• RSI 과매수 {rsi_overbought} — {analysis.get('rsi_overbought_reason', '')}",
        f"• {analysis.get('horizon', '중기')} — {analysis.get('horizon_reason', '')}",
    ]

    # 활성화된 조건 근거 표시
    enabled_conditions = analysis.get("enabled_conditions", {})
    if enabled_conditions:
        enabled = [(k, v) for k, v in enabled_conditions.items()
                   if isinstance(v, dict) and v.get("enabled")
                   and k not in ("target_price", "stop_loss_price")]
        disabled_important = [(k, v) for k, v in enabled_conditions.items()
                              if isinstance(v, dict) and not v.get("enabled")
                              and v.get("reason")]

        if enabled:
            lines.append("")
            lines.append(f"📋 *활성 조건* ({len(enabled)}개)")
            for cond_id, info in enabled:
                val_str = f" ({info['value']})" if info.get("value") else ""
                lines.append(f"  ✅ {cond_id}{val_str} — {info.get('reason', '')}")

        # 주요 비활성 조건 (근거가 있는 것만, 최대 5개)
        if disabled_important:
            notable = [d for d in disabled_important
                       if d[0] in ("rsi_oversold_intraday", "rsi_critical",
                                   "ichimoku_golden_cross", "ichimoku_death_cross",
                                   "stochastic_golden_cross", "stochastic_death_cross")][:5]
            if notable:
                lines.append("")
                lines.append("🚫 *주요 비활성 조건*")
                for cond_id, info in notable:
                    lines.append(f"  ❌ {cond_id} — {info.get('reason', '')}")

    return "\n".join(lines)


def _is_auto_mode() -> bool:
    """AUTO_TRADE 환경변수로 자동/수동 모드 판별."""
    return os.getenv("AUTO_TRADE", "false").strip().lower() == "true"


# ═══════════════════════════ 메인: 일일 자동 스크리닝 ═══════════════════════════

def run_daily_screening():
    """일일 자동 스크리닝: 후보 발굴 → AI 분석 → 모드에 따라 자동 편입 or 사용자 승인 요청."""
    from notifications.telegram import send_message, send_message_with_inline_buttons
    from data.db import save_strategy_note
    from worker.clients.kiwoom_client import KiwoomClient

    auto_mode = _is_auto_mode()
    kiwoom = KiwoomClient()
    market_text = _build_market_text(kiwoom)

    candidates = _screen_candidates()
    if not candidates:
        logger.info("[스크리닝] 후보 없음")
        return

    # 프리필터: AI 분석 전 명백히 부적합 종목 제거
    candidates = _prefilter_candidates(candidates, kiwoom)
    if not candidates:
        logger.info("[스크리닝] 프리필터 후 후보 없음")
        return

    logger.info(f"[스크리닝] 후보 {len(candidates)}개 → AI 분석 시작")

    added = []
    pending = []
    result_counts = {}  # 판정별 집계
    for cand in candidates:
        try:
            analysis = _analyze_candidate(
                cand["stock_code"],
                cand["stock_name"],
                kiwoom=kiwoom,
                market_text=market_text,
            )
            rec = analysis.get("recommendation", "분석 실패")
            rr = analysis.get("rr_ratio", "N/A")
            reason = str(analysis.get("reason", "") or "").replace("\n", " ").strip()
            result_counts[rec] = result_counts.get(rec, 0) + 1
            logger.info(
                f"[스크리닝] {cand['stock_name']} ({cand['stock_code']}) "
                f"→ {rec} (R/R={rr})"
            )

            # RAG용: 모든 스크리닝 결과 저장 (관심종목 등록/보류/부적합 모두)
            try:
                from data.db import save_screening_log
                log_id = save_screening_log(
                    stock_code=cand["stock_code"],
                    stock_name=cand["stock_name"],
                    source=cand["source"],
                    recommendation=rec,
                    reason=reason,
                    met_conditions=analysis.get("met_conditions"),
                    rr_ratio=float(rr) if rr and rr != "N/A" else None,
                    current_price=analysis.get("_current_price"),
                    indicator_snapshot=json.dumps(
                        {k: v for k, v in analysis.get("enabled_conditions", {}).items()
                         if isinstance(v, dict) and v.get("enabled")},
                        ensure_ascii=False,
                    ) if analysis.get("enabled_conditions") else None,
                    dart_summary=analysis.get("_dart_summary"),
                    news_summary=analysis.get("_news_summary"),
                    market_snapshot=market_text,
                    ai_response=analysis.get("_ai_response"),
                )
            except Exception as e:
                logger.warning(f"[스크리닝] screening_log 저장 실패: {e}")
                log_id = None

            if analysis.get("recommendation") != "관심종목 등록":
                time.sleep(2)
                continue

            alert_text = _format_screening_alert(
                cand["stock_name"], cand["stock_code"], cand["source"], analysis,
            )

            if auto_mode:
                # 자동 모드: 즉시 등록 + 근거 포함 알림
                add_to_watchlist(cand["stock_code"], cand["stock_name"], analysis)
                ok = send_message(f"{alert_text}\n\n✅ *자동 관심종목 등록 완료*")
                if not ok:
                    logger.warning(f"[스크리닝] {cand['stock_name']} 텔레그램 알림 발송 실패")
                added.append(cand["stock_name"])
                if log_id:
                    try:
                        from data.db import update_screening_action
                        update_screening_action(log_id, "auto_accepted")
                    except Exception:
                        pass
            else:
                # 수동 모드: 근거 포함 알림 + 승인 버튼
                buttons = [
                    [
                        {"text": "✅ 관심종목 등록", "callback_data": f"screen_add:{cand['stock_code']}"},
                        {"text": "❌ 패스", "callback_data": f"screen_pass:{cand['stock_code']}"},
                    ]
                ]
                # analysis를 임시 저장 (텔레그램 봇에서 콜백 시 사용)
                _pending_screenings[cand["stock_code"]] = {
                    "stock_name": cand["stock_name"],
                    "analysis": analysis,
                    "log_id": log_id,
                }
                msg_id = send_message_with_inline_buttons(alert_text, buttons)
                if msg_id is None:
                    logger.warning(f"[스크리닝] {cand['stock_name']} 텔레그램 버튼 알림 발송 실패")
                pending.append(cand["stock_name"])

            time.sleep(2)  # API rate limit
        except Exception as e:
            logger.error(f"[스크리닝] {cand['stock_name']} 분석 실패: {e}")

    # AI 분석 집계
    counts_str = ", ".join(f"{k}={v}" for k, v in sorted(result_counts.items()))
    logger.info(f"[스크리닝] AI 분석 완료 ({len(candidates)}개): {counts_str}")
    if added:
        summary = f"자동 스크리닝: {', '.join(added)} 관심종목 등록"
        save_strategy_note("watchlist", summary, summary)
        logger.info(f"[스크리닝] {len(added)}개 종목 자동 등록 완료")
    if pending:
        logger.info(f"[스크리닝] {len(pending)}개 종목 사용자 승인 대기 중")


# 수동 모드에서 사용자 승인 대기 중인 스크리닝 결과
_pending_screenings: dict[str, dict] = {}


def handle_screening_callback(stock_code: str, action: str) -> str:
    """텔레그램 봇에서 스크리닝 콜백 처리.

    Returns:
        응답 메시지 텍스트
    """
    pending = _pending_screenings.pop(stock_code, None)
    if not pending:
        return "⚠️ 만료된 요청입니다."

    log_id = pending.get("log_id")

    if action == "add":
        add_to_watchlist(stock_code, pending["stock_name"], pending["analysis"])
        from data.db import save_strategy_note
        save_strategy_note(
            "watchlist",
            f"수동 스크리닝: {pending['stock_name']} 관심종목 등록",
            f"사용자 승인으로 등록. {pending['analysis'].get('reason', '')}",
        )
        if log_id:
            try:
                from data.db import update_screening_action
                update_screening_action(log_id, "accepted")
            except Exception:
                pass
        return f"✅ {pending['stock_name']} 관심종목 등록 완료"
    else:
        if log_id:
            try:
                from data.db import update_screening_action
                update_screening_action(log_id, "rejected")
            except Exception:
                pass
        return f"❌ {pending['stock_name']} 패스"


# ═══════════════════════════ 일일 자동 복기 ═══════════════════════════


def run_daily_review():
    """장 마감 후 AI 자동 복기: 오늘 신호·매매·포트폴리오 분석 → 전략노트 저장 + 텔레그램."""
    from data.db import (
        get_today_signals, get_portfolio, get_trades,
        get_verdict_accuracy, save_strategy_note, get_screening_accuracy,
    )
    from notifications.telegram import send_message

    if not _ai_client:
        logger.warning("[일일복기] AI 클라이언트 미설정 — 건너뜀")
        return

    today_str = now_kst().strftime("%Y-%m-%d")
    logger.info(f"[일일복기] {today_str} 복기 시작")

    # ── 데이터 수집 ──
    signals = get_today_signals()
    trades = get_trades(limit=30)
    today_trades = [t for t in trades if str(t.get("executed_at", "")).startswith(today_str)]
    portfolio = get_portfolio()
    accuracy_14d = get_verdict_accuracy(days=14)
    screening_acc = get_screening_accuracy(days=30)

    # 신호 요약 (최대 15건, 핵심만)
    signal_lines = []
    for s in signals[:15]:
        verdict = s.get("verdict") or "?"
        name = s.get("stock_name", "")
        price = s.get("current_price", 0)
        conds = s.get("triggered_conditions", "")
        r1d = s.get("result_1d")
        result_str = f" | 1일후:{r1d:+.1f}%" if r1d is not None else ""
        signal_lines.append(f"  {name} [{verdict}] {price:,}원 | {conds}{result_str}")

    # 매매 요약
    trade_lines = []
    for t in today_trades[:10]:
        trade_lines.append(
            f"  {t.get('stock_name','')} {t.get('side','')} {t.get('quantity',0)}주 @ {t.get('price',0):,}원"
        )

    # 포트폴리오 요약
    total_eval = sum(p.get("eval_amount", 0) for p in portfolio)
    total_pl = sum(p.get("profit_loss", 0) for p in portfolio)
    port_lines = []
    for p in portfolio[:10]:
        rate = p.get("profit_rate", 0)
        port_lines.append(f"  {p.get('stock_name','')} {rate:+.1f}%")

    # 적중률 요약
    acc_lines = []
    for v, a in accuracy_14d.items():
        hit = a.get("hit_rate_3d")
        avg3 = a.get("avg_3d")
        acc_lines.append(f"  {v}: {a['count']}건, 적중률 {hit}%, 평균3일 {avg3:+.1f}%")

    # 스크리닝 성과
    scr_text = ""
    if screening_acc:
        scr_text = (
            f"추천 {screening_acc['total']}건"
            f" | 7일적중 {screening_acc.get('hit_7d', 'N/A')}%"
            f" | 30일적중 {screening_acc.get('hit_30d', 'N/A')}%"
        )

    # ── AI 프롬프트 구성 (간결하게) ──
    user_prompt = f"""## {today_str} 장 마감 복기 데이터

### 오늘 신호 ({len(signals)}건)
{chr(10).join(signal_lines) if signal_lines else "없음"}

### 오늘 매매 ({len(today_trades)}건)
{chr(10).join(trade_lines) if trade_lines else "없음"}

### 포트폴리오 (평가액 {total_eval:,}원, 손익 {total_pl:+,}원)
{chr(10).join(port_lines) if port_lines else "보유 없음"}

### 최근 14일 AI 판정 적중률
{chr(10).join(acc_lines) if acc_lines else "데이터 부족"}

### 최근 30일 스크리닝 성과
{scr_text or "데이터 부족"}

위 데이터를 분석하여 아래 형식으로 하루 복기를 작성하세요.

## 출력 형식 (엄격히 준수)
[시장총평] 1~2문장
[신호분석] 오늘 주요 신호와 AI 판정 평가 (2~3문장)
[매매평가] 오늘 매매 실행 평가, 없으면 "매매 없음" (1~2문장)
[적중률분석] 최근 판정별 적중률 분석, 오판 패턴이 있으면 지적 (2~3문장)
[스크리닝평가] 종목 추천 성과 분석, 없으면 생략 (1~2문장)
[내일주의] 내일 주의사항/확인할 포인트 (2~3개 bullet)
[개선제안] AI 판단 개선을 위한 구체적 제안 (1~2개, 없으면 생략)

총 300자 이내. 마크다운 헤더(#) 금지."""

    system_prompt = (
        "당신은 퀀트 트레이딩 시스템의 일일 복기 분석가입니다. "
        "데이터 기반으로 냉정하게 평가하고, 구체적 개선점을 제시합니다. "
        "감정적 표현 없이 수치 중심으로 작성하세요."
    )

    try:
        if _AI_BACKEND == "anthropic":
            response = _ai_client.messages.create(
                model=_AI_MODEL_MINI,
                max_tokens=600,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            review_text = response.content[0].text
        else:
            response = _ai_client.chat.completions.create(
                model=_AI_MODEL_MINI,
                max_tokens=600,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
            review_text = response.choices[0].message.content
    except Exception as e:
        logger.error(f"[일일복기] AI 호출 실패: {e}")
        return

    # ── 전략노트 저장 ──
    summary = f"{today_str} 자동 복기"
    save_strategy_note("daily_review", summary, review_text)
    logger.info(f"[일일복기] 전략노트 저장 완료")

    # ── 텔레그램 발송 (4000자 제한) ──
    tg_text = f"📊 *{today_str} 일일 복기*\n\n{review_text}"
    if len(tg_text) > 3900:
        tg_text = tg_text[:3900] + "\n\n_(이하 생략)_"
    send_message(tg_text)
    logger.info(f"[일일복기] 텔레그램 발송 완료")


# ═══════════════════════════ watchlist 조건 재평가 ═══════════════════════════

def _detect_regime(chart, rsi: float | None, current_price: int) -> dict:
    """종합 지표 기반 레짐 감지.

    Returns:
        {
            "trend": "uptrend" | "downtrend" | "sideways",
            "volatility": "high" | "normal" | "low",
            "momentum": "bullish" | "bearish" | "neutral",
            "score": int,  # -100 ~ +100
            "bandwidth": float | None,  # 볼린저 밴드폭 (%)
        }
    """
    if chart is None:
        return {"trend": "sideways", "volatility": "normal", "momentum": "neutral",
                "score": 0, "bandwidth": None}

    score = 0

    # ── MA 정배열/역배열 (가중치 높음: 추세 핵심) ──
    if chart.ma5 and chart.ma20:
        ratio = chart.ma5 / chart.ma20
        if ratio > 1.02:
            score += 20  # 강한 정배열
        elif ratio > 1.0:
            score += 10  # 약한 정배열
        elif ratio < 0.98:
            score -= 20  # 강한 역배열
        else:
            score -= 10  # 약한 역배열

    # ── RSI ──
    if rsi is not None:
        if rsi > 65:
            score += 15
        elif rsi > 50:
            score += 5
        elif rsi < 35:
            score -= 15
        elif rsi < 50:
            score -= 5

    # ── MACD ──
    if chart.macd_line is not None and chart.macd_signal is not None:
        if chart.macd_line > chart.macd_signal:
            score += 10
        else:
            score -= 10

    # ── 스토캐스틱 ──
    if chart.stochastic_k is not None:
        if chart.stochastic_k > 80:
            score += 5
        elif chart.stochastic_k < 20:
            score -= 5

    # ── CCI ──
    if chart.cci is not None:
        if chart.cci > 100:
            score += 5
        elif chart.cci < -100:
            score -= 5

    # ── 일목균형표 구름대 (가중치 높음: 중기 추세) ──
    if chart.ichimoku_above_cloud:
        score += 15
    elif chart.ichimoku_below_cloud:
        score -= 15

    # ── OBV ──
    if chart.obv_trend:
        if "매수" in chart.obv_trend:
            score += 5
        elif "매도" in chart.obv_trend:
            score -= 5

    # ── 다이버전스 (추세 전환 신호) ──
    if chart.rsi_divergence:
        if "강세" in chart.rsi_divergence:
            score += 10
        elif "약세" in chart.rsi_divergence:
            score -= 10
    if chart.macd_divergence:
        if "강세" in chart.macd_divergence:
            score += 8
        elif "약세" in chart.macd_divergence:
            score -= 8

    score = max(-100, min(100, score))

    # ── 추세 판정 ──
    if score >= 20:
        trend = "uptrend"
    elif score <= -20:
        trend = "downtrend"
    else:
        trend = "sideways"

    # ── 변동성 (볼린저 밴드폭) ──
    bandwidth = None
    volatility = "normal"
    if chart.bollinger_upper and chart.bollinger_lower and chart.ma20 and chart.ma20 > 0:
        bandwidth = (chart.bollinger_upper - chart.bollinger_lower) / chart.ma20 * 100
        if bandwidth > 15:
            volatility = "high"
        elif bandwidth < 5:
            volatility = "low"

    # ── 모멘텀 ──
    momentum = "neutral"
    if chart.macd_line is not None and chart.macd_signal is not None and rsi is not None:
        macd_diff = chart.macd_line - chart.macd_signal
        if macd_diff > 0 and rsi > 50:
            momentum = "bullish"
        elif macd_diff < 0 and rsi < 50:
            momentum = "bearish"

    return {
        "trend": trend,
        "volatility": volatility,
        "momentum": momentum,
        "score": score,
        "bandwidth": bandwidth,
    }


def _calculate_adjustments(regime: dict, stock: dict) -> dict:
    """레짐 기반 조건 조정값 계산. 변경이 필요한 필드만 반환."""
    trend = regime["trend"]
    volatility = regime["volatility"]
    horizon = stock.get("horizon", "중기")
    adjustments = {}

    # ── RSI 과매도 ──
    # 하락장: 기준 올림 → 덜 빠져도 신호 → 더 일찍 잡힘
    # 상승장: 기준 내림 → 더 깊이 빠져야 신호 → 노이즈 감소
    # 횡보: 변경 없음 (신호 빈도 조절 목적 변경 금지 원칙 준수)
    cur = stock.get("rsi_oversold")
    if cur is not None and trend != "sideways":
        base_floor = {"단기": 38, "장기": 42}.get(horizon, 40)  # 종목 유형별 하한
        if trend == "downtrend":
            ideal = min(45, cur + 3)  # 상한 45
        else:  # uptrend
            ideal = max(base_floor, cur - 3)  # 하한: horizon 기본값
        if abs(ideal - cur) >= 3:
            adjustments["rsi_oversold"] = ideal

    # ── RSI 과매수 ──
    cur = stock.get("rsi_overbought")
    if cur is not None:
        base = {"단기": 65, "장기": 70}.get(horizon, 67)
        adj = 0
        if trend == "uptrend":
            adj += 3  # 상승 추세: 더 올라갈 수 있음
        elif trend == "downtrend":
            adj -= 3  # 하락 추세: 일찍 빠져야
        if volatility == "high":
            adj += 2
        elif volatility == "low":
            adj -= 2
        ideal = max(55, min(80, base + adj))
        if abs(ideal - cur) >= 3:
            adjustments["rsi_overbought"] = ideal

    # ── CCI 과매도 ──
    cur = stock.get("cci_oversold")
    if cur is not None:
        base = -100
        adj = 0
        if trend == "downtrend":
            adj -= 20
        elif trend == "uptrend":
            adj += 15
        if volatility == "high":
            adj -= 15
        elif volatility == "low":
            adj += 15
        ideal = max(-200, min(-50, base + adj))
        if abs(ideal - cur) >= 20:
            adjustments["cci_oversold"] = ideal

    # ── CCI 과매수 ──
    cur = stock.get("cci_overbought")
    if cur is not None:
        base = 100
        adj = 0
        if trend == "uptrend":
            adj += 20
        elif trend == "downtrend":
            adj -= 15
        if volatility == "high":
            adj += 15
        elif volatility == "low":
            adj -= 15
        ideal = max(50, min(200, base + adj))
        if abs(ideal - cur) >= 20:
            adjustments["cci_overbought"] = ideal

    # ── 거래량 급증 비율 ──
    cur = stock.get("volume_surge_ratio")
    if cur is not None:
        if volatility == "high" and cur < 2.5:
            adjustments["volume_surge_ratio"] = round(min(3.0, cur + 0.5), 1)
        elif volatility == "low" and cur > 2.0:
            adjustments["volume_surge_ratio"] = round(max(1.5, cur - 0.5), 1)

    return adjustments


def _parse_price_int(val) -> int:
    """현재가/가격 문자열을 int로 파싱."""
    try:
        return abs(int(str(val or "0").replace(",", "").strip()))
    except (ValueError, TypeError):
        return 0


def reassess_watchlist(kiwoom) -> None:
    """장 시작 전 watchlist 미보유 종목 조건 재평가.

    전일 대비 레짐(추세·변동성·모멘텀)이 변했으면 임계값 자동 조정.
    RSI, MA, MACD, 볼린저, 스토캐스틱, CCI, 일목균형표, OBV, 다이버전스 종합 판단.
    """
    from data.db import get_watchlist, get_portfolio, update_stock_field, save_strategy_note
    from worker.indicators import calculate_rsi, calculate_chart_summary
    from notifications.telegram import send_message

    stocks = [s for s in get_watchlist() if s.get("enabled")]
    held_codes = {str(h.get("stock_code", "")) for h in get_portfolio()}
    targets = [s for s in stocks if s["code"] not in held_codes]

    if not targets:
        logger.info("[재평가] 미보유 watchlist 종목 없음 — 스킵")
        return

    logger.info(f"[재평가] 미보유 {len(targets)}개 종목 조건 재평가 시작")
    changes = []

    for stock in targets:
        code = stock["code"]
        name = stock["name"]
        try:
            price_data = kiwoom.get_current_price(code)
            current_price = _parse_price_int(
                price_data.get("cur_prc") or price_data.get("stk_prpr"))
            if not current_price:
                continue

            time.sleep(1)
            daily = kiwoom.get_daily_ohlcv(code, period=90)
            closes, highs, lows, opens, vols = [], [], [], [], []
            for d in daily:
                c = _parse_price_int(d.get("cur_prc"))
                h = _parse_price_int(d.get("high_pric"))
                lo = _parse_price_int(d.get("lwst_pric") or d.get("low_pric"))
                o = _parse_price_int(d.get("strt_pric") or d.get("opn_pric"))
                v = _parse_price_int(d.get("trde_qty"))
                if c:
                    closes.append(c)
                if h:
                    highs.append(h)
                if lo:
                    lows.append(lo)
                if o:
                    opens.append(o)
                if v:
                    vols.append(v)

            if len(closes) < 20:
                continue

            horizon = stock.get("horizon", "중기")
            rsi_period = {"단기": 7, "장기": 21}.get(horizon, 14)
            rsi = calculate_rsi(closes, period=rsi_period) if len(closes) >= rsi_period + 1 else None
            chart = calculate_chart_summary(
                closes, highs, current_price,
                low_prices=lows, open_prices=opens, volumes=vols,
            ) if len(closes) >= 5 else None

            regime = _detect_regime(chart, rsi, current_price)
            adjs = _calculate_adjustments(regime, stock)

            if adjs:
                for field, value in adjs.items():
                    update_stock_field(code, field, value)
                changes.append({
                    "name": name, "code": code,
                    "regime": regime, "adjustments": adjs,
                })
                adj_str = ", ".join(f"{k}: {stock.get(k)}→{v}" for k, v in adjs.items())
                logger.info(f"[재평가] {name}: {regime['trend']}/{regime['volatility']} "
                            f"(score={regime['score']}) → {adj_str}")

            time.sleep(1)

        except Exception as e:
            logger.warning(f"[재평가] {name}: 실패 ({e})")
            time.sleep(2)

    # ── 결과 리포트 ──
    if changes:
        lines = [f"🔄 *watchlist 조건 재평가* ({len(changes)}개 조정)\n"]
        for c in changes:
            r = c["regime"]
            trend_emoji = {"uptrend": "📈", "downtrend": "📉", "sideways": "➡️"}[r["trend"]]
            adj_parts = []
            for field, val in c["adjustments"].items():
                adj_parts.append(f"`{field}` → {val}")
            lines.append(f"{trend_emoji} *{c['name']}* ({r['trend']}, {r['volatility']})")
            lines.append(f"  {', '.join(adj_parts)}")
        msg = "\n".join(lines)
        send_message(msg)

        # 전략 로그 자동 기록 (A안: 시장 국면 자동 조정은 원칙 예외, 단 ±5 이내 + 로그 필수)
        adj_summary = ", ".join(
            f"{c['name']} {k}: {c['adjustments'][k]}"
            for c in changes for k in c["adjustments"]
        )
        save_strategy_note(
            category="watchlist",
            summary=f"09:15 레짐 재평가 자동 조정 ({len(changes)}개 종목)",
            detail=adj_summary,
        )
        logger.info(f"[재평가] {len(changes)}개 종목 조정 완료")
    else:
        logger.info("[재평가] 조정 필요 종목 없음")
