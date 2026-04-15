"""
키움 REST API 직접 호출 클라이언트 (워커 전용)
MCP 코드 구조 기반으로 작성 (POST + api-id 헤더 방식)
"""

import os
import logging
import time
from datetime import datetime, timedelta, timezone
from datetime import time as dtime
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

BASE_URL = os.getenv("KIWOOM_BASE_URL", "https://api.kiwoom.com").rstrip("/")
APP_KEY = os.getenv("KIWOOM_APP_KEY")
APP_SECRET = os.getenv("KIWOOM_APP_SECRET")
ACCOUNT_NO = os.getenv("KIWOOM_ACCOUNT_NO")
KIWOOM_IS_MOCK = os.getenv("KIWOOM_IS_MOCK", "").strip().lower()
KIWOOM_BLOCK_AFTER_HOURS_IN_MOCK = os.getenv("KIWOOM_BLOCK_AFTER_HOURS_IN_MOCK", "true").strip().lower()

KST = ZoneInfo("Asia/Seoul")
logger = logging.getLogger(__name__)


class KiwoomClient:
    @staticmethod
    def _env_bool(value: str) -> bool | None:
        if value in {"1", "true", "yes", "y", "on"}:
            return True
        if value in {"0", "false", "no", "n", "off"}:
            return False
        return None

    def __init__(self):
        self._client = httpx.Client(timeout=10.0)
        self._token: str | None = None
        self._token_expires_at: datetime | None = None
        # 종목명→코드 캐시 (당일 유지)
        self._stock_map: dict[str, str] = {}   # name → code
        self._stock_map_date: str = ""
        # 모의/실거래 여부에 따라 거래소 구분 자동 설정
        _mock_override = self._env_bool(KIWOOM_IS_MOCK)
        self._is_mock = _mock_override if _mock_override is not None else ("mockapi" in BASE_URL)
        _block_override = self._env_bool(KIWOOM_BLOCK_AFTER_HOURS_IN_MOCK)
        self._block_mock_after_hours = True if _block_override is None else _block_override
        self._holdings_markets = ["KRX"] if self._is_mock else ["KRX", "NXT"]
        self._trade_history_markets = ["KRX"] if self._is_mock else ["%"]
        self._order_market = "KRX" if self._is_mock else "SOR"

        # 기존 코드 호환용 (사용처가 남아있을 수 있어 유지)
        self._dmst_stex_tp = self._trade_history_markets[0]
        self._stex_tp = "1" if self._is_mock else "3"

    def _post_with_retry(
        self,
        url: str,
        *,
        json_body: dict,
        headers: dict,
        api_name: str,
        max_retries: int = 4,
    ) -> httpx.Response:
        last_resp: httpx.Response | None = None
        for attempt in range(max_retries):
            resp = self._client.post(url, json=json_body, headers=headers)
            last_resp = resp
            if resp.status_code == 429:
                wait = min(8, 2 ** attempt)
                logger.warning(f"[429] {api_name} 재시도 {attempt + 1}/{max_retries} ({wait}s 대기)")
                time.sleep(wait)
                continue
            return resp
        if last_resp is not None:
            return last_resp
        raise RuntimeError(f"{api_name} 요청 실패 (응답 없음)")

    @staticmethod
    def _is_regular_session_now() -> bool:
        """KRX 정규장(09:00~15:30) 여부."""
        now_t = datetime.now(tz=KST).time()
        return dtime(9, 0) <= now_t <= dtime(15, 30)

    def _get_token(self) -> str:
        now = datetime.now(tz=timezone.utc)
        if self._token and self._token_expires_at and now < self._token_expires_at - timedelta(seconds=60):
            return self._token

        resp = self._post_with_retry(
            f"{BASE_URL}/oauth2/token",
            json_body={
                "grant_type": "client_credentials",
                "appkey": APP_KEY,
                "secretkey": APP_SECRET,
            },
            headers={"Content-Type": "application/json;charset=UTF-8"},
            api_name="oauth2/token",
        )
        resp.raise_for_status()
        payload = resp.json()

        token = str(payload.get("token", "")).strip()
        if not token:
            raise ValueError(f"토큰 발급 실패: {payload}")

        self._token = token
        expires_dt = str(payload.get("expires_dt", "")).strip()
        self._token_expires_at = self._parse_expires_dt(expires_dt)
        return token

    @staticmethod
    def _parse_expires_dt(value: str) -> datetime:
        if value:
            try:
                dt = datetime.strptime(value, "%Y%m%d%H%M%S")
                return dt.replace(tzinfo=KST).astimezone(timezone.utc)
            except ValueError:
                pass
        return datetime.now(tz=timezone.utc) + timedelta(hours=1)

    def _headers(self, api_id: str) -> dict:
        return {
            "Content-Type": "application/json;charset=UTF-8",
            "authorization": f"Bearer {self._get_token()}",
            "api-id": api_id,
        }

    def _post(self, path: str, api_id: str, body: dict) -> dict:
        url = f"{BASE_URL}/{path.lstrip('/')}"
        for attempt in range(2):  # 토큰 만료 시 1회 재시도
            resp = self._post_with_retry(
                url,
                json_body=body,
                headers=self._headers(api_id),
                api_name=api_id,
            )
            if resp.status_code == 401 and attempt == 0:
                self._token = None
                self._token_expires_at = None
                logger.warning(f"[{api_id}] 401 응답으로 토큰 갱신 후 재시도")
                continue
            resp.raise_for_status()
            payload = resp.json()
            code = payload.get("return_code")
            # return_code=3: 토큰 유효하지 않음 (8005) — 토큰 갱신 후 1회 재시도
            if code in (3, "3") and attempt == 0:
                msg = payload.get("return_msg", "")
                self._token = None
                self._token_expires_at = None
                logger.warning(f"[{api_id}] return_code=3 ({msg}) — 토큰 갱신 후 재시도")
                continue
            if code not in (None, 0, "0"):
                raise RuntimeError(f"API 오류 [{api_id}] code={code} msg={payload.get('return_msg')}")
            return payload
        raise RuntimeError(f"API 요청 실패 [{api_id}]")

    def get_current_price(self, stock_code: str) -> dict:
        """현재가 조회 (ka10001 주식기본정보요청)"""
        return self._post(
            "/api/dostk/stkinfo",
            "ka10001",
            {"stk_cd": stock_code},
        )

    def get_daily_ohlcv(self, stock_code: str, period: int = 21) -> list[dict]:
        """일봉 데이터 조회 (ka10081 주식일봉차트)"""
        today = datetime.now(tz=KST).strftime("%Y%m%d")
        payload = self._post(
            "/api/dostk/chart",
            "ka10081",
            {
                "stk_cd": stock_code,
                "base_dt": today,
                "upd_stkpc_tp": "1",
            },
        )
        # 응답 필드명 확인 후 필요시 수정
        rows = payload.get("stk_dt_pole_chart_qry", [])
        return rows[:period]

    def get_intraday_ohlcv(self, stock_code: str, tic_scope: str = "5", period: int = 30) -> list[dict]:
        """분봉 데이터 조회 (ka10080 주식분봉차트)
        tic_scope: 분 단위 ("1", "3", "5", "10", "15", "30", "60")
        period: 최대 조회 봉 수
        반환: 최신순 정렬된 분봉 리스트 (cur_prc 필드 사용)
        """
        today = datetime.now(tz=KST).strftime("%Y%m%d")
        payload = self._post(
            "/api/dostk/chart",
            "ka10080",
            {
                "stk_cd": stock_code,
                "tic_scope": tic_scope,
                "base_dt": today,
                "upd_stkpc_tp": "0",
            },
        )
        rows = payload.get("stk_mnt_pole_chart_qry", [])
        return rows[:period]

    def get_holdings(self) -> list[dict]:
        """보유 종목 조회 (kt00018 계좌평가잔고내역요청)"""
        results = []
        for market in self._holdings_markets:
            payload = self._post(
                "/api/dostk/acnt",
                "kt00018",
                {"qry_tp": "1", "dmst_stex_tp": market},
            )
            rows = payload.get("acnt_evlt_remn_indv_tot", [])
            results.extend(rows if isinstance(rows, list) else [])
        return results

    def _parse_executions_rows(self, rows: list, stock_code: str = "") -> list[dict]:
        """kt00007 raw rows 파싱."""
        if not isinstance(rows, list):
            return []
        result = []

        def _norm_date(raw: str) -> str:
            s = str(raw or "").strip().replace("-", "")
            return s if len(s) == 8 and s.isdigit() else ""

        for r in rows:
            code = str(r.get("stk_cd") or "").strip().lstrip("A")
            if stock_code and code != stock_code:
                continue
            io_nm = str(r.get("io_tp_nm") or "")
            side = "매도" if "매도" in io_nm else "매수"

            def _i(key):
                return abs(int(str(r.get(key) or "0").replace(",", "").lstrip("0") or "0"))

            executed_date = (
                _norm_date(r.get("cntr_dt"))
                or _norm_date(r.get("trde_dt"))
                or _norm_date(r.get("ord_dt"))
                or ""
            )

            result.append({
                "stock_code": code,
                "stock_name": str(r.get("stk_nm") or "").strip(),
                "side": side,
                "quantity": _i("cntr_qty"),    # 체결수량
                "price": _i("cntr_uv"),        # 체결가격
                "order_qty": _i("ord_qty"),    # 주문수량
                "remain_qty": _i("ord_remnq"), # 잔량(미체결)
                "time": str(r.get("cnfm_tm") or r.get("ord_tm") or ""),
                "executed_date": executed_date,  # YYYYMMDD(있으면)
                "order_no": str(r.get("ord_no") or ""),
                "market": str(r.get("dmst_stex_tp") or ""),
            })
        return result

    @staticmethod
    def _normalize_yyyymmdd(trade_date: str) -> str:
        s = str(trade_date or "").strip().replace("-", "")
        if len(s) == 8 and s.isdigit():
            return s
        return ""

    def get_executions(
        self,
        stock_code: str = "",
        trade_date: str = "",
        fill_missing_date: bool = True,
    ) -> list[dict]:
        """체결 내역 조회 (kt00007 계좌별주문체결내역상세요청).

        문서 스펙 기준:
        - body: ord_dt, qry_tp, stk_bond_tp, sell_tp, stk_cd, fr_ord_no, dmst_stex_tp
        - header: cont-yn, next-key(연속조회)
        """
        ymd = self._normalize_yyyymmdd(trade_date) or datetime.now(tz=KST).strftime("%Y%m%d")
        dmst_stex_tp = "KRX" if self._is_mock else "%"
        body = {
            "ord_dt": ymd,
            "qry_tp": "1",       # 1: 주문
            "stk_bond_tp": "0",  # 0: 전체
            "sell_tp": "0",      # 0: 전체
            "stk_cd": stock_code or "",
            "fr_ord_no": "",
            "dmst_stex_tp": dmst_stex_tp,
        }

        merged: dict[tuple[str, str, str, str, int, int], dict] = {}
        cont_yn = ""
        next_key = ""

        while True:
            headers = self._headers("kt00007")
            if cont_yn:
                headers["cont-yn"] = cont_yn
                headers["next-key"] = next_key

            resp = self._post_with_retry(
                f"{BASE_URL}/api/dostk/acnt",
                json_body=body,
                headers=headers,
                api_name="kt00007",
            )
            resp.raise_for_status()
            payload = resp.json()
            code = payload.get("return_code")
            if code not in (None, 0, "0"):
                raise RuntimeError(f"API 오류 [kt00007] code={code} msg={payload.get('return_msg')}")

            rows = payload.get("acnt_ord_cntr_prps_dtl", [])
            for item in self._parse_executions_rows(rows, stock_code=stock_code):
                # 응답에 체결일이 없을 수 있어, 해당 API 요청일을 보조 메타로 채움
                if fill_missing_date and not str(item.get("executed_date") or "").strip():
                    item["executed_date"] = ymd
                k = (
                    str(item.get("order_no") or ""),
                    str(item.get("stock_code") or ""),
                    str(item.get("side") or ""),
                    str(item.get("time") or ""),
                    int(item.get("quantity") or 0),
                    int(item.get("price") or 0),
                )
                merged[k] = item

            cont_yn = resp.headers.get("cont-yn", "")
            next_key = resp.headers.get("next-key", "")
            if cont_yn != "Y":
                break
            # 문서상 fr_ord_no(시작주문번호)도 함께 전달
            if next_key:
                body["fr_ord_no"] = next_key

        return list(merged.values())

    def get_pending_orders(self, stock_code: str = "") -> list[dict]:
        """당일 미체결 주문 조회 (ka10075 미체결요청).
        stock_code 지정 시 해당 종목만 필터링하여 반환.
        반환: [{"stock_code", "stock_name", "order_no", "side", "order_qty", "exec_qty", "remain_qty", "order_price", "time"}, ...]
        """
        payload = self._post(
            "/api/dostk/acnt",
            "ka10075",
            {"all_stk_tp": "0", "stk_cd": stock_code, "trde_tp": "0", "stex_tp": "0"},
        )
        rows = payload.get("oso", [])
        if not isinstance(rows, list):
            return []
        result = []
        for r in rows:
            code = str(r.get("stk_cd") or "").strip().lstrip("A")
            if stock_code and code != stock_code:
                continue
            io_nm = str(r.get("io_tp_nm") or r.get("seln_byov_tp") or "")
            side = "매도" if ("매도" in io_nm or io_nm == "1") else "매수"

            def _i(key):
                return abs(int(str(r.get(key) or "0").replace(",", "").lstrip("0") or "0"))

            result.append({
                "stock_code": code,
                "stock_name": str(r.get("stk_nm") or "").strip(),
                "order_no": str(r.get("ord_no") or ""),
                "side": side,
                "order_qty": _i("ord_qty"),
                "exec_qty": _i("cntr_qty") or _i("exec_qty"),
                "remain_qty": _i("ord_remnq") or _i("rema_qty"),
                "order_price": _i("ord_uv"),
                "time": str(r.get("ord_tm") or r.get("ord_tmd") or ""),
            })
        return result

    def get_deposit(self) -> dict:
        """주문 가능 예수금 조회 (kt00001 예수금상세현황요청).
        반환: {"deposit": int, "order_available": int}
        """
        payload = self._post(
            "/api/dostk/acnt",
            "kt00001",
            {"qry_tp": "3"},  # 3=전체조회
        )

        def _int(key):
            v = str(payload.get(key, "0") or "0").replace(",", "").lstrip("0") or "0"
            try:
                return abs(int(float(v)))
            except Exception:
                return 0

        deposit = _int("entr")            # 예수금
        order_available = _int("ord_alow_amt")  # 주문가능금액
        if not order_available:
            order_available = deposit
        return {"deposit": deposit, "order_available": order_available}

    def get_market_index(self, market: str = "kospi") -> dict:
        """업종(지수) 현재가 조회 (ka20001)
        market: "kospi" 또는 "kosdaq"
        """
        if market == "kosdaq":
            body = {"mrkt_tp": "1", "inds_cd": "101"}
        else:
            body = {"mrkt_tp": "0", "inds_cd": "001"}
        try:
            return self._post("/api/dostk/sect", "ka20001", body)
        except Exception as e:
            logger.warning(f"지수 조회 실패 ({market}): {e}")
            return {}

    def get_trade_history_range(self, start_dt: str, end_dt: str) -> list[dict]:
        """매매 내역 조회 (kt00015, tp=3:매수 + tp=4:매도, 기간 지정)."""
        start_dt = str(start_dt or "").replace("-", "").strip()
        end_dt = str(end_dt or "").replace("-", "").strip()
        if not (len(start_dt) == 8 and start_dt.isdigit() and len(end_dt) == 8 and end_dt.isdigit()):
            raise ValueError(f"잘못된 조회 기간: start_dt={start_dt}, end_dt={end_dt}")

        results = []
        for market in self._trade_history_markets:
            for tp in ("3", "4"):
                try:
                    payload = self._post(
                        "/api/dostk/acnt",
                        "kt00015",
                        {
                            "strt_dt": start_dt,
                            "end_dt": end_dt,
                            "tp": tp,
                            "stk_cd": "",
                            "crnc_cd": "KRW",
                            "gds_tp": "1",
                            "dmst_stex_tp": market,
                            "frgn_stex_code": "",
                        },
                    )
                    rows = payload.get("trst_ovrl_trde_prps_array", [])
                    results.extend(rows if isinstance(rows, list) else [])
                except Exception as e:
                    logger.warning(f"매매 내역 조회 실패 (tp={tp}, market={market}): {e}")
        return results

    def get_trade_history(self, days: int = 30) -> list[dict]:
        """매매 내역 조회 (kt00015, tp=3:매수 + tp=4:매도)."""
        end_dt = datetime.now(tz=KST).strftime("%Y%m%d")
        start_dt = (datetime.now(tz=KST) - timedelta(days=days)).strftime("%Y%m%d")
        return self.get_trade_history_range(start_dt, end_dt)

    @staticmethod
    def _to_num(value) -> float | None:
        try:
            s = str(value or "").replace(",", "").strip()
            if not s:
                return None
            return float(s)
        except Exception:
            return None

    def _extract_realized_metrics(self, payload: dict) -> dict:
        """실현손익 관련 수치 후보를 payload에서 최대한 보수적으로 추출."""
        import re

        pnl_candidates: list[tuple[str, float]] = []
        fee_candidates: list[float] = []
        tax_candidates: list[float] = []

        def _walk(obj, prefix: str = ""):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    key = f"{prefix}.{k}" if prefix else str(k)
                    _walk(v, key)
                return
            if isinstance(obj, list):
                for i, item in enumerate(obj):
                    _walk(item, f"{prefix}[{i}]")
                return
            n = self._to_num(obj)
            if n is None:
                return
            lk = prefix.lower()
            # 수수료 / 세금
            if any(x in lk for x in ("fee", "수수료")):
                fee_candidates.append(n)
            if any(x in lk for x in ("tax", "세금")):
                tax_candidates.append(n)
            # 실현손익 계열 후보
            if any(x in lk for x in ("실현", "손익", "prft", "profit", "pnl", "손익금", "pl")):
                # 비율/퍼센트로 보이는 필드는 제외
                if re.search(r"(rate|rt|율|퍼센트|pct)", lk):
                    return
                pnl_candidates.append((prefix, n))

        _walk(payload)

        realized_pnl = None
        source_key = ""
        if pnl_candidates:
            # 절대값이 가장 큰 값을 대표치로 사용
            source_key, realized_pnl = max(pnl_candidates, key=lambda x: abs(x[1]))

        fee = sum(fee_candidates) if fee_candidates else None
        tax = sum(tax_candidates) if tax_candidates else None
        return {
            "realized_pnl": realized_pnl,
            "fee": fee,
            "tax": tax,
            "pnl_source_key": source_key,
            "pnl_candidates": len(pnl_candidates),
        }

    def get_realized_pnl_today(self) -> dict:
        """당일 실현손익 조회 (ka10077 우선, ka10074 폴백)."""
        api_candidates = [
            ("ka10077", [{"tp": "0"}, {"tp": "1"}, {"tp": "2"}]),
            ("ka10074", [{"qry_tp": "2"}, {"tp": "0"}]),
        ]
        errors: list[str] = []
        for api_id, bodies in api_candidates:
            for body in bodies:
                try:
                    payload = self._post("/api/dostk/acnt", api_id, body)
                    metrics = self._extract_realized_metrics(payload)
                    return {"ok": True, "scope": "today", "api_id": api_id, "body": body, **metrics, "raw": payload}
                except Exception as e:
                    errors.append(f"{api_id} body={body}: {e}")
                    logger.debug(f"[실현손익] {api_id} 실패 body={body}: {e}")
        return {"ok": False, "scope": "today", "error": "실현손익 API 호출 실패", "attempt_errors": errors}

    def get_realized_pnl_period(self, days: int = 30) -> dict:
        """기간 실현손익 조회 (ka10073 우선, ka10074 폴백)."""
        end_dt = datetime.now(tz=KST).strftime("%Y%m%d")
        start_dt = (datetime.now(tz=KST) - timedelta(days=max(1, int(days)))).strftime("%Y%m%d")
        stex = self._trade_history_markets[0]
        body_full = {
            "strt_dt": start_dt,
            "end_dt": end_dt,
            "stk_cd": "",
            "crnc_cd": "KRW",
            "gds_tp": "1",
            "dmst_stex_tp": stex,
            "frgn_stex_code": "",
        }
        api_candidates = [
            ("ka10073", [body_full, {"strt_dt": start_dt, "end_dt": end_dt}]),
            ("ka10074", [body_full, {"strt_dt": start_dt, "end_dt": end_dt}]),
        ]
        errors: list[str] = []
        for api_id, bodies in api_candidates:
            for body in bodies:
                try:
                    payload = self._post("/api/dostk/acnt", api_id, body)
                    metrics = self._extract_realized_metrics(payload)
                    return {
                        "ok": True,
                        "scope": "period",
                        "api_id": api_id,
                        "body": body,
                        "start_dt": start_dt,
                        "end_dt": end_dt,
                        **metrics,
                        "raw": payload,
                    }
                except Exception as e:
                    errors.append(f"{api_id} body_keys={list(body.keys())}: {e}")
                    logger.debug(f"[실현손익] {api_id} 실패 body_keys={list(body.keys())}: {e}")
        return {
            "ok": False,
            "scope": "period",
            "start_dt": start_dt,
            "end_dt": end_dt,
            "error": "실현손익 API 호출 실패",
            "attempt_errors": errors,
        }

    def _load_stock_map(self) -> None:
        """당일 캐시가 없으면 ka10099로 코스피/코스닥 전종목 로드."""
        today = datetime.now(tz=KST).strftime("%Y%m%d")
        if self._stock_map_date == today and self._stock_map:
            return
        new_map = {}
        for mrkt_tp in ("0", "10"):  # 0=코스피, 10=코스닥
            try:
                items = self._fetch_all_pages(
                    "/api/dostk/stkinfo", "ka10099", {"mrkt_tp": mrkt_tp}
                )
                for item in items:
                    code = str(item.get("code", "")).strip()
                    name = str(item.get("name", "")).strip()
                    if code and name:
                        new_map[name] = code
                import time as _time
                _time.sleep(1)  # 시장 간 요청 간격
            except Exception as e:
                logger.warning(f"ka10099 종목 로드 실패 (mrkt_tp={mrkt_tp}): {e}")
        if new_map:
            self._stock_map = new_map
            self._stock_map_date = today
            logger.info(f"[종목캐시] {len(self._stock_map)}개 로드 완료")
        else:
            logger.warning(f"[종목캐시] 로드 실패 — 기존 {len(self._stock_map)}개 유지")

    def _fetch_all_pages(self, path: str, api_id: str, body: dict,
                         max_retries: int = 3) -> list[dict]:
        """cont-yn / next-key 페이지네이션 처리하여 전체 결과 반환.
        429 응답 시 최대 max_retries 회 재시도 (지수 백오프).
        """
        import time as _time
        results = []
        cont_yn = ""
        next_key = ""
        while True:
            headers = self._headers(api_id)
            if cont_yn:
                headers["cont-yn"] = cont_yn
                headers["next-key"] = next_key
            for attempt in range(max_retries):
                resp = self._client.post(
                    f"{BASE_URL}/{path.lstrip('/')}",
                    json=body,
                    headers=headers,
                )
                if resp.status_code == 429:
                    wait = 2 ** attempt  # 1s, 2s, 4s
                    logger.warning(f"[429] {api_id} 레이트 리밋 — {wait}초 후 재시도 ({attempt+1}/{max_retries})")
                    _time.sleep(wait)
                    continue
                resp.raise_for_status()
                break
            else:
                raise RuntimeError(f"{api_id} 429 재시도 초과")
            payload = resp.json()
            items = payload.get("list", [])
            if isinstance(items, list):
                results.extend(items)
            cont_yn = resp.headers.get("cont-yn", "")
            next_key = resp.headers.get("next-key", "")
            if cont_yn != "Y":
                break
        return results

    def search_stock_by_name(self, name: str) -> list[tuple[str, str]]:
        """종목명(부분 일치)으로 (code, name) 목록 반환.
        키움 ka10099 기반 당일 캐시 사용 — 외부 연결 없음.
        예: '삼성전' → [('005930', '삼성전자'), ('009150', '삼성전기'), ...]
        """
        query = name.strip()
        if not query:
            return []
        self._load_stock_map()
        results = [
            (code, n)
            for n, code in self._stock_map.items()
            if query in n
        ]
        logger.info(f"[검색-ka10099] '{query}' 결과: {results[:3]} (전체 {len(results)}건)")
        return results[:10]

    def _get_trde_tp(self, price: int) -> str:
        """현재 시간 기준 거래구분 코드 자동 선택.
        프리장(08:30~09:00) → 61, 정규장(09:00~15:30) → 0/3,
        애프터장(15:40~16:00) → 81, 시간외단일가(16:00~18:00) → 62
        """
        t = datetime.now(tz=KST).time()
        if dtime(8, 30) <= t < dtime(9, 0):
            return "61"
        if dtime(9, 0) <= t <= dtime(15, 30):
            return "0" if price == 0 else "3"
        if dtime(15, 40) <= t < dtime(16, 0):
            return "81"
        if dtime(16, 0) <= t <= dtime(18, 0):
            return "62"
        return "0" if price == 0 else "3"  # 기본값 (정규장 기준)

    def place_order(
        self,
        stock_code: str,
        order_type: str,  # "1"=매수, "2"=매도
        qty: int,
        price: int = 0,  # 0=시장가
        order_market: str | None = None,  # "KRX" | "NXT" | "SOR"
    ) -> dict:
        """주식 주문
        kt10000: 매수주문, kt10001: 매도주문
        trde_tp: 현재 세션에 따라 자동 선택
          - 정규장: 0(시장가) / 3(지정가)
          - 프리장:  61
          - 애프터:  81
          - 시간외단일가: 62
        """
        api_id = "kt10000" if order_type == "1" else "kt10001"
        # 지정가 요청 시 항상 현재가로 강제 — 임의 금액 입력 방지
        if price != 0:
            try:
                pd = self.get_current_price(stock_code)
                real_price = abs(int(str(pd.get("cur_prc") or "0").replace(",", "")))
                if real_price > 0:
                    if real_price != price:
                        logger.info(f"[주문] 지정가 {price:,}원 → 현재가 {real_price:,}원으로 강제 적용")
                    price = real_price
            except Exception as e:
                logger.warning(f"[주문] 현재가 조회 실패, 요청 가격 {price:,}원 유지: {e}")
        trde_tp = self._get_trde_tp(price)
        # 모의투자: 정규장 시간(09:00~15:30) 외 주문 전면 차단
        if self._is_mock and self._block_mock_after_hours and not self._is_regular_session_now():
            raise RuntimeError(
                "모의투자에서는 정규장(09:00~15:30) 외 주문을 지원하지 않습니다."
            )
        # 모의투자는 정규장 주문(0/3)만 허용. 시간외(61/81/62)는 명시적으로 차단.
        if self._is_mock and self._block_mock_after_hours and trde_tp in {"61", "81", "62"}:
            session_map = {"61": "장전 시간외", "81": "장후 시간외", "62": "시간외 단일가"}
            session_name = session_map.get(trde_tp, "시간외")
            raise RuntimeError(
                f"모의투자에서는 {session_name} 주문을 지원하지 않습니다. 정규장(09:00~15:30)에서만 주문 가능합니다."
            )
        # 시간외단일가(62): 지정가 필수 — 시장가 요청 시 현재가로 자동 변환
        if trde_tp == "62" and price == 0:
            try:
                pd = self.get_current_price(stock_code)
                price = abs(int(str(pd.get("cur_prc") or "0").replace(",", "")))
                logger.info(f"[주문] 시간외단일가 자동지정가: {price:,}원")
            except Exception as e:
                logger.warning(f"[주문] 시간외단일가 현재가 조회 실패: {e}")
        # 프리장/애프터장 종가매매는 가격 지정 불필요 (종가 자동 적용)
        ord_uv = "" if trde_tp in ("61", "81") else (str(price) if price else "")
        market = str(order_market or self._order_market).strip().upper()
        if self._is_mock:
            # 모의투자는 KRX만 사용
            market = "KRX"
        elif market not in {"KRX", "NXT", "SOR"}:
            market = self._order_market

        logger.info(
            f"[주문] {stock_code} {'매수' if order_type=='1' else '매도'} {qty}주 "
            f"market={market} trde_tp={trde_tp} price={ord_uv or '종가'}"
        )
        try:
            return self._post(
                "/api/dostk/ordr",
                api_id,
                {
                    "dmst_stex_tp": market,
                    "stk_cd": stock_code,
                    "ord_qty": str(qty),
                    "ord_uv": ord_uv,
                    "trde_tp": trde_tp,
                    "cond_uv": "",
                },
            )
        except RuntimeError as e:
            if "RC4027" not in str(e) or trde_tp not in ("0",):
                raise
            # RC4027(모의투자 상/하한가): 시장가 → 현재가 지정가로 1회 재시도
            try:
                pd = self.get_current_price(stock_code)
                fallback_price = abs(int(str(pd.get("cur_prc") or "0").replace(",", "")))
            except Exception as fe:
                logger.warning(f"[주문] RC4027 폴백 현재가 조회 실패: {fe}")
                raise e
            if not fallback_price:
                raise
            logger.warning(f"[주문] RC4027 → 지정가 재시도: {fallback_price:,}원")
            return self._post(
                "/api/dostk/ordr",
                api_id,
                {
                    "dmst_stex_tp": market,
                    "stk_cd": stock_code,
                    "ord_qty": str(qty),
                    "ord_uv": str(fallback_price),
                    "trde_tp": "3",
                    "cond_uv": "",
                },
            )

    def get_sector_index(self, inds_cd: str) -> dict:
        """종목 업종코드로 섹터 지수 조회 (ka20001)
        업종코드는 ka10001 응답의 upjong_cd 값
        """
        # 업종코드 앞자리로 시장구분 추론 (1xx=코스닥, 0xx=코스피)
        mrkt_tp = "1" if str(inds_cd).startswith("1") else "0"
        try:
            return self._post(
                "/api/dostk/sect",
                "ka20001",
                {"mrkt_tp": mrkt_tp, "inds_cd": inds_cd.zfill(3)},
            )
        except Exception as e:
            logger.warning(f"섹터 조회 실패 ({inds_cd}): {e}")
            return {}

    # ── 랭킹/시세 조회 (스크리닝용) ──────────────────────────────────

    def get_volume_surge(self) -> list[dict]:
        """거래량 급증 종목 (ka10023)"""
        stex = self._stex_tp
        payload = self._post(
            "/api/dostk/rkinfo", "ka10023",
            {
                "mrkt_tp": "000",
                "sort_tp": "1",
                "tm_tp": "2",
                "trde_qty_tp": "5",
                "tm": "",
                "stk_cnd": "0",
                "pric_tp": "0",
                "stex_tp": stex,
            },
        )
        return payload.get("trde_qty_sdnin") or payload.get("output") or []

    def get_decline_rank(self) -> list[dict]:
        """등락률 하위 종목 — 하락 상위 (ka10027)"""
        stex = self._stex_tp
        payload = self._post(
            "/api/dostk/rkinfo", "ka10027",
            {
                "mrkt_tp": "000",
                "sort_tp": "1",
                "trde_qty_cnd": "0000",
                "stk_cnd": "0",
                "crd_cnd": "0",
                "updown_incls": "1",
                "pric_cnd": "0",
                "trde_prica_cnd": "0",
                "stex_tp": stex,
            },
        )
        return payload.get("pred_pre_flu_rt_upper") or payload.get("output") or []

    def get_foreign_net_buy(self) -> list[dict]:
        """외인 순매수 상위 (ka10035)"""
        stex = self._stex_tp
        payload = self._post(
            "/api/dostk/rkinfo", "ka10035",
            {
                "mrkt_tp": "000",
                "trde_tp": "2",
                "base_dt_tp": "1",
                "stex_tp": stex,
            },
        )
        return payload.get("for_cont_nettrde_upper") or payload.get("output") or []

    def get_quiet_accumulation(self, change_threshold: float = 3.0, max_results: int = 15) -> list[dict]:
        """조용한 축적 종목: 외인 순매수 상위 중 등락률 ±threshold% 이내.

        외인이 꾸준히 사고 있지만 주가는 아직 횡보하는 종목을 찾는다.
        """
        foreign_stocks = self.get_foreign_net_buy()
        result = []
        for item in foreign_stocks:
            code = str(item.get("stk_cd") or item.get("shtn_iscd") or "").strip()
            if not code or len(code) != 6:
                continue
            try:
                time.sleep(0.5)
                price_data = self.get_current_price(code)
                change_str = str(price_data.get("flu_rt") or price_data.get("prdy_ctrt") or "0")
                change_pct = abs(float(change_str.replace(",", "").replace("+", "").replace("-", "")))
                if change_pct <= change_threshold:
                    item["_change_pct"] = change_pct
                    result.append(item)
                    if len(result) >= max_results:
                        break
            except Exception:
                continue
        logger.info(f"[조용한축적] 외인순매수 {len(foreign_stocks)}개 중 등락률 ±{change_threshold}% 이내: {len(result)}개")
        return result

    # ── HTS 조건검색 (WebSocket) ──────────────────────────────────

    def _ws_url(self) -> str:
        """WebSocket URL 생성."""
        if "mockapi" in BASE_URL:
            return "wss://mockapi.kiwoom.com:10000/api/dostk/websocket"
        return "wss://api.kiwoom.com:10000/api/dostk/websocket"

    async def _ws_condition_search(self) -> list[dict]:
        """단일 WebSocket 연결에서 LOGIN → ka10171 → ka10172 순차 실행.

        키움 WebSocket은 같은 연결에서 ka10171(목록) 호출 후
        ka10172(검색)를 해야 정상 동작한다.
        """
        import websockets
        import json as _json
        import asyncio

        token = self._get_token()

        async with websockets.connect(self._ws_url()) as ws:
            # ── 1) LOGIN ──
            await ws.send(_json.dumps({"trnm": "LOGIN", "token": token}))
            login_resp = _json.loads(await ws.recv())
            logger.debug(f"[WS] LOGIN: code={login_resp.get('return_code')}")
            if login_resp.get("return_code") != 0:
                logger.warning(f"[WS] LOGIN 실패: {login_resp}")
                return []

            # ── 2) ka10171 — 조건 목록 조회 ──
            await ws.send(_json.dumps({"trnm": "CNSRLST"}))
            list_resp = _json.loads(await ws.recv())
            logger.debug(f"[조건검색] ka10171 return_code={list_resp.get('return_code')}")
            data = list_resp.get("data", [])

            conditions = []
            for item in data:
                if isinstance(item, list) and len(item) >= 2:
                    conditions.append({"seq": item[0], "name": item[1]})
                elif isinstance(item, dict):
                    conditions.append({"seq": item.get("seq", ""), "name": item.get("name", "")})

            quant_conditions = [c for c in conditions if c["name"].startswith("퀀트_")]
            logger.info(f"[조건검색] 목록 {len(conditions)}개, 퀀트_ {len(quant_conditions)}개: {[c['name'] for c in quant_conditions]}")

            if not quant_conditions:
                logger.info("[조건검색] '퀀트_' 조건식 없음 (전체: %s)", [c["name"] for c in conditions[:10]])
                return []

            # ── 3) ka10172 — 각 조건 실행 (같은 연결) ──
            all_stocks = []
            seen_codes: set[str] = set()

            for cond in quant_conditions:
                req = {
                    "trnm": "CNSRREQ",
                    "seq": cond["seq"],
                    "search_type": "0",
                    "stex_tp": "K",
                    "cont_yn": "N",
                    "next_key": "",
                }
                await ws.send(_json.dumps(req))

                # 비동기 응답: 첫 recv는 ACK일 수 있음. data가 올 때까지 최대 5회 수신
                resp_data = []
                for attempt in range(5):
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=10)
                        resp = _json.loads(raw)
                        logger.debug(
                            f"[조건검색] ka10172 '{cond['name']}' recv#{attempt}: "
                            f"keys={list(resp.keys())} trnm={resp.get('trnm')}"
                            f"{' data_len=' + str(len(resp['data'])) if 'data' in resp and isinstance(resp.get('data'), list) else ''}"
                        )
                        if "data" in resp and resp["data"]:
                            resp_data = resp["data"]
                            break
                        # return_code 에러면 중단
                        if resp.get("return_code") and resp["return_code"] != 0:
                            logger.warning(f"[조건검색] ka10172 에러: {resp.get('return_msg')}")
                            break
                    except asyncio.TimeoutError:
                        logger.debug(f"[조건검색] ka10172 '{cond['name']}' recv#{attempt}: timeout")
                        break

                logger.info(f"[조건검색] '{cond['name']}' → {len(resp_data)}건")
                for item in resp_data:
                    if not isinstance(item, dict):
                        continue
                    code = str(item.get("9001", "")).replace("A", "").strip()
                    name = str(item.get("302", "")).strip()
                    if code and len(code) == 6 and code not in seen_codes:
                        seen_codes.add(code)
                        all_stocks.append({
                            "stock_code": code,
                            "stock_name": name,
                            "source": f"조건검색:{cond['name']}",
                        })
                await asyncio.sleep(1)  # rate limit

            logger.info(f"[조건검색] 퀀트 조건 {len(quant_conditions)}개 → {len(all_stocks)}종목")
            return all_stocks

    def get_condition_list(self) -> list[dict]:
        """HTS 조건검색 목록 조회 (ka10171). 단독 호출용."""
        import asyncio
        import json as _json

        try:
            async def _query():
                import websockets
                token = self._get_token()
                async with websockets.connect(self._ws_url()) as ws:
                    await ws.send(_json.dumps({"trnm": "LOGIN", "token": token}))
                    await ws.recv()
                    await ws.send(_json.dumps({"trnm": "CNSRLST"}))
                    return _json.loads(await ws.recv())

            result = asyncio.run(_query())
            data = result.get("data", [])
            conditions = []
            for item in data:
                if isinstance(item, list) and len(item) >= 2:
                    conditions.append({"seq": item[0], "name": item[1]})
                elif isinstance(item, dict):
                    conditions.append({"seq": item.get("seq", ""), "name": item.get("name", "")})
            logger.info(f"[조건검색] 목록 {len(conditions)}개 조회: {[c['name'] for c in conditions[:10]]}")
            return conditions
        except Exception as e:
            logger.warning(f"조건검색 목록 조회 실패: {e}")
            return []

    def run_all_quant_conditions(self) -> list[dict]:
        """'퀀트_' 접두어가 붙은 모든 조건식을 단일 WebSocket 연결에서 실행.
        Returns: [{"stock_code", "stock_name", "source": "조건검색:조건명"}, ...]
        """
        import asyncio

        try:
            return asyncio.run(self._ws_condition_search())
        except Exception as e:
            logger.warning(f"HTS 조건검색 실패: {e}")
            return []
