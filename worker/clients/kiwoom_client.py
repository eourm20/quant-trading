"""
키움 REST API 직접 호출 클라이언트 (워커 전용)
MCP 코드 구조 기반으로 작성 (POST + api-id 헤더 방식)
"""

import os
import logging
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

BASE_URL = os.getenv("KIWOOM_BASE_URL", "https://api.kiwoom.com").rstrip("/")
APP_KEY = os.getenv("KIWOOM_APP_KEY")
APP_SECRET = os.getenv("KIWOOM_APP_SECRET")
ACCOUNT_NO = os.getenv("KIWOOM_ACCOUNT_NO")

KST = ZoneInfo("Asia/Seoul")
logger = logging.getLogger(__name__)


class KiwoomClient:
    def __init__(self):
        self._client = httpx.Client(timeout=10.0)
        self._token: str | None = None
        self._token_expires_at: datetime | None = None
        # 종목명→코드 캐시 (당일 유지)
        self._stock_map: dict[str, str] = {}   # name → code
        self._stock_map_date: str = ""
        # 모의/실거래 여부에 따라 거래소 구분 자동 설정
        self._is_mock = "mockapi" in BASE_URL
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
        for attempt in range(2):  # 401 토큰 만료 시 1회 재시도
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

    def get_trade_history(self, days: int = 30) -> list[dict]:
        """매매 내역 조회 (kt00015, tp=3:매수 + tp=4:매도)"""
        end_dt = datetime.now(tz=KST).strftime("%Y%m%d")
        start_dt = (datetime.now(tz=KST) - timedelta(days=days)).strftime("%Y%m%d")
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
        from datetime import time as dtime
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
        trde_tp = self._get_trde_tp(price)
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
