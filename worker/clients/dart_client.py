"""
DART 공시 API 클라이언트
- 종목별 최근 공시 조회
- corp_code 매핑 (종목코드 → DART 고유번호)
"""

import io
import json
import logging
import os
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

logger = logging.getLogger(__name__)

DART_API_KEY = os.getenv("DART_API_KEY", "").strip()
DART_BASE_URL = "https://opendart.fss.or.kr/api"

# corp_code 매핑 캐시 (종목코드 → DART 고유번호)
_corp_code_map: dict[str, str] = {}
_corp_code_loaded = False
_CACHE_DIR = os.path.join(os.path.dirname(__file__), ".dart_cache")


def _ensure_cache_dir():
    os.makedirs(_CACHE_DIR, exist_ok=True)


def _load_corp_code_map() -> dict[str, str]:
    """종목코드 → DART 고유번호 매핑 로드. 하루 1회 갱신."""
    global _corp_code_map, _corp_code_loaded

    if _corp_code_loaded and _corp_code_map:
        return _corp_code_map

    _ensure_cache_dir()
    cache_file = os.path.join(_CACHE_DIR, "corp_code_map.json")
    cache_date_file = os.path.join(_CACHE_DIR, "corp_code_date.txt")

    # 캐시가 오늘 날짜면 재사용
    today = datetime.now().strftime("%Y-%m-%d")
    if os.path.exists(cache_file) and os.path.exists(cache_date_file):
        with open(cache_date_file, "r") as f:
            cached_date = f.read().strip()
        if cached_date == today:
            with open(cache_file, "r", encoding="utf-8") as f:
                _corp_code_map = json.load(f)
            _corp_code_loaded = True
            logger.info(f"DART corp_code 캐시 로드: {len(_corp_code_map)}개")
            return _corp_code_map

    if not DART_API_KEY:
        logger.warning("DART_API_KEY 미설정")
        return {}

    # DART에서 고유번호 파일 다운로드
    try:
        resp = requests.get(
            f"{DART_BASE_URL}/corpCode.xml",
            params={"crtfc_key": DART_API_KEY},
            timeout=30,
        )
        resp.raise_for_status()

        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            xml_name = zf.namelist()[0]
            xml_data = zf.read(xml_name)

        root = ET.fromstring(xml_data)
        result = {}
        for corp in root.findall("list"):
            stock_code = (corp.findtext("stock_code") or "").strip()
            corp_code = (corp.findtext("corp_code") or "").strip()
            if stock_code and corp_code:
                result[stock_code] = corp_code

        # 캐시 저장
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(result, f)
        with open(cache_date_file, "w") as f:
            f.write(today)

        _corp_code_map = result
        _corp_code_loaded = True
        logger.info(f"DART corp_code 다운로드 완료: {len(result)}개")
        return result

    except Exception as e:
        logger.error(f"DART corp_code 다운로드 실패: {e}")
        # 이전 캐시가 있으면 사용
        if os.path.exists(cache_file):
            with open(cache_file, "r", encoding="utf-8") as f:
                _corp_code_map = json.load(f)
            _corp_code_loaded = True
            return _corp_code_map
        return {}


def get_corp_code(stock_code: str) -> str | None:
    """종목코드(6자리) → DART 고유번호(8자리) 변환."""
    mapping = _load_corp_code_map()
    return mapping.get(stock_code)


def get_disclosures(
    stock_code: str,
    days: int = 30,
    pblntf_ty: str = "",
) -> list[dict]:
    """특정 종목의 최근 공시 목록 조회.

    Args:
        stock_code: 종목코드 (6자리)
        days: 조회 기간 (일)
        pblntf_ty: 공시유형 필터 (빈값=전체, A=정기, B=주요사항, C=발행, D=지분, E=기타, I=거래소)

    Returns:
        list[dict]: 공시 목록 [{report_nm, rcept_dt, flr_nm, rcept_no, rm}, ...]
    """
    if not DART_API_KEY:
        return []

    corp_code = get_corp_code(stock_code)
    if not corp_code:
        logger.debug(f"DART corp_code 없음: {stock_code}")
        return []

    end_date = datetime.now().strftime("%Y%m%d")
    bgn_date = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")

    params = {
        "crtfc_key": DART_API_KEY,
        "corp_code": corp_code,
        "bgn_de": bgn_date,
        "end_de": end_date,
        "last_reprt_at": "N",
        "page_count": 100,
    }
    if pblntf_ty:
        params["pblntf_ty"] = pblntf_ty

    try:
        resp = requests.get(
            f"{DART_BASE_URL}/list.json",
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("status") != "000":
            if data.get("status") == "013":  # 조회 결과 없음
                return []
            logger.warning(f"DART API 오류: {data.get('message')} (status={data.get('status')})")
            return []

        return data.get("list", [])

    except Exception as e:
        logger.error(f"DART 공시 조회 실패 ({stock_code}): {e}")
        return []


# ─────────────────────────── 공시 유형별 조회 ───────────────────────────

# 공시유형별 조회 기간
_DISCLOSURE_PERIODS = {
    "B": 30,   # 주요사항 (수주/계약)
    "A": 90,   # 정기공시 (실적)
    "C": 90,   # 발행공시 (유상증자/CB/BW)
    "D": 30,   # 지분공시 (내부자 매매)
    "I": 7,    # 거래소공시 (단기 이벤트)
    "": 14,    # 기타 전체
}


def get_all_disclosures(stock_code: str) -> list[dict]:
    """공시유형별 적절한 기간으로 전체 공시 조회 (중복 제거)."""
    seen_rcept = set()
    all_disclosures = []

    for pblntf_ty, days in _DISCLOSURE_PERIODS.items():
        disclosures = get_disclosures(stock_code, days=days, pblntf_ty=pblntf_ty)
        for d in disclosures:
            rcept_no = d.get("rcept_no", "")
            if rcept_no not in seen_rcept:
                seen_rcept.add(rcept_no)
                all_disclosures.append(d)
        # DART API rate limit (1초 1회)
        time.sleep(0.5)

    # 최신순 정렬
    all_disclosures.sort(key=lambda x: x.get("rcept_dt", ""), reverse=True)
    return all_disclosures


def format_disclosures_for_ai(stock_code: str, max_items: int = 10) -> str:
    """AI 판단 프롬프트용 공시 요약 텍스트 생성."""
    disclosures = get_all_disclosures(stock_code)

    if not disclosures:
        return "최근 공시 없음"

    lines = []
    for d in disclosures[:max_items]:
        dt = d.get("rcept_dt", "")
        if len(dt) == 8:
            dt = f"{dt[:4]}-{dt[4:6]}-{dt[6:]}"
        name = d.get("report_nm", "")
        filer = d.get("flr_nm", "")
        remark = d.get("rm", "")

        line = f"  - {dt} {name}"
        if filer and filer != d.get("corp_name", ""):
            line += f" ({filer})"
        if remark:
            line += f" [{remark}]"
        lines.append(line)

    return "\n".join(lines)


def get_financial_summary_for_ai(stock_code: str) -> str:
    """AI 판단용 재무지표 요약 (매출, 영업이익, PER, PBR, ROE 등)."""
    if not DART_API_KEY:
        return ""

    corp_code = get_corp_code(stock_code)
    if not corp_code:
        return ""

    from datetime import datetime
    year = str(datetime.now().year - 1)

    # 주요계정 조회
    try:
        resp = requests.get(
            f"{DART_BASE_URL}/fnlttSinglAcnt.json",
            params={"crtfc_key": DART_API_KEY, "corp_code": corp_code,
                    "bsns_year": year, "reprt_code": "11011"},
            timeout=15,
        )
        data = resp.json()
        if data.get("status") != "000":
            return ""

        lines = []
        for item in data.get("list", []):
            acnt = item.get("account_nm", "")
            amount = item.get("thstrm_amount", "")
            if acnt and amount and item.get("fs_div") == "CFS":  # 연결재무제표
                lines.append(f"  - {acnt}: {amount}")

        if not lines:
            return ""
        return f"  [{year}년 사업보고서 주요계정]\n" + "\n".join(lines[:8])

    except Exception:
        return ""


def format_full_context_for_ai(stock_code: str) -> str:
    """AI 판단용 DART 전체 컨텍스트 (공시 + 재무지표)."""
    logger.info(f"[DART] 호출 시작: stock_code={stock_code}")
    parts = []

    disclosures = format_disclosures_for_ai(stock_code, max_items=8)
    if disclosures and disclosures != "최근 공시 없음":
        parts.append(disclosures)

    financial = get_financial_summary_for_ai(stock_code)
    if financial:
        parts.append(financial)

    result = "\n".join(parts) if parts else "최근 공시/재무 데이터 없음"
    logger.info(f"[DART] 호출 완료: stock_code={stock_code}, has_context={'예' if bool(parts) else '아니오'}")
    return result
