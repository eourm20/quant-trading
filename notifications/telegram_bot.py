"""
텔레그램 봇 — 양방향 매매 명령 수신 + 실행
- /buy 종목코드or이름 수량  : 시장가 매수 요청
- /sell 종목코드or이름 수량 : 시장가 매도 요청
- /confirm                  : 대기 중인 주문 실행
- /cancel                   : 대기 중인 주문 취소
- /price 종목코드or이름     : 현재가 조회
- /balance                  : 잔고/보유 종목 조회
- /help                     : 도움말

보안: TELEGRAM_CHAT_ID 에서 온 메시지만 처리
안전장치: /buy·/sell 는 /confirm 으로 2단계 확인 후 실행
종목명 입력 시 watchlist에서 자동으로 코드 변환 (관심종목만 가능)
"""

import logging
import os
import threading
import time
from datetime import datetime, timedelta, time as dtime, timezone

_KST = timezone(timedelta(hours=9))


def _now_kst() -> datetime:
    return datetime.now(_KST).replace(tzinfo=None)

import httpx
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = str(os.getenv("TELEGRAM_CHAT_ID", "")).strip()
ALLOW_TRADE = True  # manual Telegram orders are always allowed (with confirm)

CONFIRM_TIMEOUT_SEC = 60  # 확인 대기 시간

# KRX 세션 정의 (kiwoom_client._get_trde_tp 와 동일 기준)
_SESSION_RANGES = {
    "premarket":   (dtime(8, 30),  dtime(9, 0)),
    "main":        (dtime(9, 0),   dtime(15, 30)),
    "aftermarket": (dtime(15, 40), dtime(16, 0)),
    "offhours":    (dtime(16, 0),  dtime(18, 0)),
}
_SESSION_LABELS = {
    "premarket":   "프리장 (전일 종가)",
    "main":        "정규장",
    "aftermarket": "애프터장 (당일 종가)",
    "offhours":    "시간외단일가",
}


def _get_order_session() -> str:
    t = _now_kst().time()
    for session, (start, end) in _SESSION_RANGES.items():
        if start <= t <= end:
            return session
    return "main"  # 장외 시간 기본값


def _resolve_stock(name_or_code: str, kiwoom=None) -> tuple[str, str]:
    """종목명 또는 종목코드를 (code, name) 으로 반환.

    조회 순서:
    1. watchlist DB (관심종목)
    2. portfolio DB (보유 종목 캐시)
    3. Kiwoom API 실시간 보유 종목 (kiwoom 클라이언트 있을 때)
    4. 숫자 6자리면 코드로 간주, 아니면 못 찾은 것으로 에러
    """
    query = name_or_code.strip()

    # ① watchlist
    try:
        from data.db import get_watchlist
        stocks = get_watchlist()
        for s in stocks:
            if s["code"] == query or s["name"] == query:
                return s["code"], s["name"]
        matched = [s for s in stocks if query in s["name"]]
        if len(matched) == 1:
            return matched[0]["code"], matched[0]["name"]
        if len(matched) > 1:
            names = ", ".join(s["name"] for s in matched)
            raise ValueError(f"종목명 여러 개 일치: {names}\n종목코드로 입력해 주세요.")
    except ValueError:
        raise
    except Exception:
        pass

    # ② portfolio DB 캐시
    try:
        from data.db import get_portfolio
        for h in get_portfolio():
            code = str(h.get("stock_code", ""))
            name = str(h.get("stock_name", ""))
            if code == query or name == query or (query in name and query):
                return code, name
    except Exception:
        pass

    # ③ Kiwoom API 실시간 보유 종목
    if kiwoom:
        try:
            holdings = kiwoom.get_holdings()
            candidates = []
            for h in holdings:
                code = str(h.get("stk_cd") or h.get("stock_cd") or "").replace("A", "", 1)
                name = str(h.get("stk_nm") or h.get("hts_kor_isnm") or "")
                if code == query or name == query:
                    return code, name
                if query in name:
                    candidates.append((code, name))
            if len(candidates) == 1:
                return candidates[0]
            if len(candidates) > 1:
                names = ", ".join(n for _, n in candidates)
                raise ValueError(f"종목명 여러 개 일치: {names}\n종목코드로 입력해 주세요.")
        except ValueError:
            raise
        except Exception:
            pass

    # ④ 숫자 6자리면 코드로 간주
    if query.isdigit() and len(query) == 6:
        return query, query

    # ⑤ 키움 ka10099 전종목 캐시 검색
    if kiwoom:
        try:
            results = kiwoom.search_stock_by_name(query)
            if len(results) == 1:
                return results[0]
            if len(results) > 1:
                names = ", ".join(f"{n}({c})" for c, n in results[:5])
                if len(results) > 5:
                    names += f" 외 {len(results) - 5}개"
                raise ValueError(f"'{query}' 검색 결과 여러 개:\n{names}\n더 구체적인 이름으로 입력해 주세요.")
        except ValueError:
            raise
        except Exception as e:
            logger.warning(f"종목 검색 실패: {e}")

    raise ValueError(f"'{query}' 종목을 찾을 수 없습니다.\n종목코드(6자리)로 입력해 주세요.")


def _resolve_name_by_code(code: str, kiwoom=None) -> str:
    """코드로 종목명을 최대한 안전하게 조회 (실패 시 code 반환)."""
    try:
        if code:
            _, name = _resolve_stock(code, kiwoom=kiwoom)
            return name or code
    except Exception:
        pass
    return code


class _PendingOrder:
    def __init__(
        self,
        stock_code: str,
        stock_name: str,
        order_type: str,
        qty: int,
        price_type: str = "market",
        limit_price: int = 0,
        signal_id: int | None = None,
        order_market: str | None = None,
    ):
        self.stock_code = stock_code
        self.stock_name = stock_name
        self.order_type = order_type   # "1"=매수, "2"=매도
        self.qty = qty
        self.price_type = price_type   # "market" or "limit"
        self.limit_price = limit_price # 지정가 주문 시 사용자가 입력한 가격
        self.signal_id = signal_id     # 원본 신호 ID (행동 기록용)
        self.order_market = (order_market or "").strip().upper() or None  # "KRX" | "NXT" | "SOR"
        self.created_at = datetime.now()

    @property
    def expired(self) -> bool:
        return datetime.now() > self.created_at + timedelta(seconds=CONFIRM_TIMEOUT_SEC)

    @property
    def side_label(self) -> str:
        return "매수" if self.order_type == "1" else "매도"


# 임계값 변경 제안 상태: key=stock_code, value={msg_id, stock_name, condition_text, pending}
_threshold_proposals: dict[str, dict] = {}


def store_threshold_proposal(
    stock_code: str, msg_id: int, stock_name: str,
    condition_text: str, changes: list[dict],
) -> None:
    """main.py에서 제안 발송 후 상태 저장."""
    _threshold_proposals[stock_code] = {
        "msg_id": msg_id,
        "stock_name": stock_name,
        "condition_text": condition_text,
        "pending": [dict(c) for c in changes],
    }


class TelegramBot:
    def __init__(self, kiwoom_client=None):
        self._client = httpx.Client(timeout=35.0)
        self._offset = 0
        self._pending: _PendingOrder | None = None
        self._lock = threading.Lock()
        self._kiwoom = kiwoom_client
        # 버튼 클릭 후 수량 입력 대기 큐: [{"action": "buy"/"sell", "code": "...", "name": "..."}, ...]
        self._waiting_qty_queue: list[dict] = []
        # 지정가 입력 대기 상태: {"order_type": "1"/"2", "code": "...", "name": "...", "qty": N}
        self._waiting_price: dict | None = None
        # 명령어 인자 없이 입력 시 종목명 대기 상태: {"cmd": "buy"/"sell"/"price"}
        self._waiting_stock_input: dict | None = None
        # 임계값 수정 대기: {"code": ..., "name": ..., "field": ..., "old": int}
        self._waiting_threshold_edit: dict | None = None

    def _available_order_markets(self) -> list[str]:
        if self._kiwoom and getattr(self._kiwoom, "_is_mock", False):
            return ["KRX"]
        return ["SOR", "KRX", "NXT"]

    def _prompt_market_selection(
        self,
        order_action: str,   # "buy" | "sell"
        price_type: str,     # "market" | "limit"
        code: str,
        name: str,
        signal_id: int | None = None,
        rec_qty: int | None = None,
    ) -> None:
        sid = signal_id if signal_id is not None else 0
        qty = rec_qty if rec_qty is not None else 0
        markets = self._available_order_markets()
        labels = {"SOR": "⚡ SOR", "KRX": "🏛 KRX", "NXT": "🧭 NXT"}
        row = []
        for market in markets:
            row.append({
                "text": labels.get(market, market),
                "callback_data": f"pick_market:{order_action}:{price_type}:{code}:{sid}:{market}:{qty}",
            })
        row.append({"text": "❌ 취소", "callback_data": "cancel_order"})

        side = "매수" if order_action == "buy" else "매도"
        ptxt = "시장가" if price_type == "market" else "지정가"
        self._send(
            f"*{name}* (`{code}`) {side} ({ptxt})\n주문시장을 선택해 주세요.",
            reply_markup={"inline_keyboard": [row]},
        )

    # ── Telegram API ───────────────────────────────────────────────────────

    def _handle_inline_query(self, query_id: str, from_id: str, query_text: str) -> None:
        """인라인 쿼리 처리 — @봇이름 삼 입력 시 종목 후보 반환."""
        # 보안: 설정된 CHAT_ID 사용자만
        if from_id != CHAT_ID:
            self._client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/answerInlineQuery",
                json={"inline_query_id": query_id, "results": []},
                timeout=5,
            )
            return

        results = []
        query = query_text.strip()
        if query and self._kiwoom:
            try:
                matches = self._kiwoom.search_stock_by_name(query)[:5]
                for code, name in matches:
                    results.append({
                        "type": "article",
                        "id": code,
                        "title": f"{name}  ({code})",
                        "description": f"종목코드 {code} — 탭하면 코드가 입력됩니다",
                        "input_message_content": {
                            "message_text": code,
                        },
                    })
            except Exception as e:
                logger.debug(f"[bot] 인라인 검색 실패: {e}")

        try:
            self._client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/answerInlineQuery",
                json={"inline_query_id": query_id, "results": results, "cache_time": 30},
                timeout=10,
            )
        except Exception as e:
            logger.warning(f"[bot] answerInlineQuery 실패: {e}")

    def _send(self, text: str, parse_mode: str = "Markdown", reply_markup: dict | None = None) -> None:
        if not BOT_TOKEN or not CHAT_ID:
            return
        payload: dict = {"chat_id": CHAT_ID, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            resp = self._client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json=payload,
                timeout=10,
            )
            if resp.status_code == 200:
                return
            body_preview = (resp.text or "")[:200]
            if parse_mode and ("can't parse entities" in body_preview.lower() or "parse entities" in body_preview.lower()):
                retry_payload: dict = {"chat_id": CHAT_ID, "text": text}
                if reply_markup:
                    retry_payload["reply_markup"] = reply_markup
                self._client.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json=retry_payload,
                    timeout=10,
                )
        except Exception as e:
            logger.warning(f"[bot] 메시지 발송 실패: {e}")

    def _get_updates(self) -> list[dict]:
        try:
            resp = self._client.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
                params={
                    "offset": self._offset,
                    "timeout": 30,
                    "allowed_updates": ["message", "callback_query", "inline_query"],
                },
                timeout=35,
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
            return data.get("result", [])
        except Exception as e:
            logger.debug(f"[bot] getUpdates 오류: {e}")
            return []

    def _answer_callback(self, callback_id: str, text: str = "") -> None:
        """버튼 클릭 응답 (로딩 스피너 제거용)."""
        try:
            self._client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery",
                json={"callback_query_id": callback_id, "text": text},
                timeout=5,
            )
        except Exception:
            pass

    def _remove_inline_keyboard(self, message_id: int) -> None:
        """원본 메시지의 인라인 키보드 제거 — 중복 클릭 방지."""
        try:
            self._client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageReplyMarkup",
                json={
                    "chat_id": CHAT_ID,
                    "message_id": message_id,
                    "reply_markup": {"inline_keyboard": []},
                },
                timeout=5,
            )
        except Exception:
            pass

    def _update_threshold_keyboard(self, stock_code: str, done_field: str) -> None:
        """처리된 필드를 제거하고 남은 필드로 키보드 업데이트."""
        proposal = _threshold_proposals.get(stock_code)
        if not proposal:
            return
        proposal["pending"] = [c for c in proposal["pending"] if c["field"] != done_field]
        msg_id = proposal["msg_id"]
        if not proposal["pending"]:
            _threshold_proposals.pop(stock_code, None)
            self._remove_inline_keyboard(msg_id)
            return
        from notifications.telegram import _build_threshold_keyboard
        keyboard = _build_threshold_keyboard(stock_code, proposal["pending"])
        try:
            self._client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageReplyMarkup",
                json={"chat_id": CHAT_ID, "message_id": msg_id,
                      "reply_markup": {"inline_keyboard": keyboard}},
                timeout=5,
            )
        except Exception:
            pass

    def _handle_callback(self, callback_id: str, data: str, message_id: int | None = None) -> None:
        """인라인 버튼 클릭 처리."""
        # th_* 콜백은 필드별 키보드 업데이트로 처리 (전체 제거 X)
        is_threshold = data.startswith(("th_apply:", "th_reject:", "th_edit:", "th_reject_all:"))
        if message_id and not is_threshold:
            self._remove_inline_keyboard(message_id)

        # 주문 실행/취소 콜백
        if data == "exec_market":
            self._answer_callback(callback_id, "시장가 주문 실행.")
            self._cmd_confirm(price_type="market")
            return
        if data == "exec_limit":
            self._answer_callback(callback_id, "지정가 주문 실행.")
            self._cmd_confirm(price_type="limit")
            return
        if data == "confirm_order":
            self._answer_callback(callback_id, "주문을 실행합니다.")
            self._cmd_confirm()
            return
        if data == "cancel_order":
            self._answer_callback(callback_id, "주문을 취소했습니다.")
            self._cmd_cancel()
            return

        # 추천수량 버튼: rec_buy_market / rec_sell_market / rec_buy_limit / rec_sell_limit
        # 신규 포맷: rec_*:{qty}:{code}:{signal_id}
        # 하위호환: rec_*:{qty}:{code}:{name[:signal_id]}
        if data.startswith(("rec_buy_market:", "rec_sell_market:", "rec_buy_limit:", "rec_sell_limit:")):
            rec_parts = data.split(":")
            if len(rec_parts) < 4:
                self._answer_callback(callback_id)
                return
            action_token = rec_parts[0]
            rec_qty_str = rec_parts[1]
            rec_code = rec_parts[2]
            try:
                rec_qty = int(rec_qty_str)
            except ValueError:
                self._answer_callback(callback_id)
                return

            rec_signal_id = None
            rec_name = _resolve_name_by_code(rec_code, kiwoom=self._kiwoom)
            # 신규 포맷
            if len(rec_parts) == 4 and rec_parts[3].isdigit():
                rec_signal_id = int(rec_parts[3]) if rec_parts[3] != "0" else None
            # 레거시 포맷
            elif len(rec_parts) >= 4:
                if rec_parts[-1].isdigit():
                    rec_signal_id = int(rec_parts[-1]) if rec_parts[-1] != "0" else None
                    rec_name = ":".join(rec_parts[3:-1]) or rec_name
                else:
                    rec_name = ":".join(rec_parts[3:]) or rec_name

            if not ALLOW_TRADE:
                self._answer_callback(callback_id, "매매 비활성화 상태입니다.")
                self._send("🔒 매매 실행이 비활성화되어 있습니다.")
                return
            order_action = "buy" if "buy" in action_token else "sell"
            price_type = "limit" if "limit" in action_token else "market"
            side = "매수" if order_action == "buy" else "매도"
            self._answer_callback(callback_id, f"추천 {rec_qty:,}주 {side} — 주문시장 선택.")
            self._prompt_market_selection(
                order_action=order_action,
                price_type=price_type,
                code=rec_code,
                name=rec_name,
                signal_id=rec_signal_id,
                rec_qty=rec_qty,
            )
            return

        # 주문시장 선택 콜백
        if data.startswith("pick_market:"):
            parts_m = data.split(":")
            if len(parts_m) != 7:
                self._answer_callback(callback_id)
                return
            _, order_action, price_type, code, sid_str, order_market, qty_str = parts_m
            signal_id = int(sid_str) if sid_str.isdigit() and sid_str != "0" else None
            pre_qty = int(qty_str) if qty_str.isdigit() and qty_str != "0" else None

            side = "매수" if order_action == "buy" else "매도"
            order_type = "1" if order_action == "buy" else "2"
            self._answer_callback(callback_id, f"주문시장 {order_market} 선택됨")

            if price_type == "limit":
                # 지정가: 먼저 가격 입력, 추천수량이면 qty pre-stored
                cur_prc = 0
                if self._kiwoom:
                    try:
                        pd = self._kiwoom.get_current_price(code)
                        cur_prc = abs(int(str(
                            pd.get("cur_prc") or pd.get("stk_prpr") or pd.get("prpr") or "0"
                        ).replace(",", "")))
                    except Exception:
                        pass
                with self._lock:
                    self._waiting_price = {
                        "order_type": order_type,
                        "code": code,
                        "name": code,
                        "qty": pre_qty,
                        "signal_id": signal_id,
                        "order_market": order_market,
                    }
                qty_hint = f", *{pre_qty:,}주*" if pre_qty else ""
                price_hint = f" (현재가: {cur_prc:,}원)" if cur_prc else ""
                self._send(
                    f"`{code}` {side} (지정가{qty_hint}, {order_market}) — 주문 가격을 입력해 주세요.{price_hint}\n"
                    f"(숫자만, 예: `{cur_prc or 50000}`)",
                    reply_markup={"inline_keyboard": [[
                        {"text": "❌ 취소", "callback_data": "cancel_order"},
                    ]]},
                )
                return

            # 시장가
            if pre_qty:
                self._cmd_order(
                    order_type,
                    code,
                    str(pre_qty),
                    price_type="market",
                    signal_id=signal_id,
                    order_market=order_market,
                )
            else:
                with self._lock:
                    self._waiting_qty_queue.append({
                        "action": order_action,
                        "price_type": "market",
                        "code": code,
                        "name": code,
                        "signal_id": signal_id,
                        "order_market": order_market,
                    })
                    queue_len = len(self._waiting_qty_queue)
                if queue_len == 1:
                    self._send(
                        f"`{code}` {side} (시장가, {order_market}) — 수량을 입력해 주세요.\n(숫자만, 예: `100`)",
                        reply_markup={"inline_keyboard": [[
                            {"text": "❌ 취소", "callback_data": "cancel_order"},
                        ]]}
                    )
                else:
                    self._send(
                        f"`{code}` {side}(시장가, {order_market}) 대기열 추가 ({queue_len}번째)\n"
                        f"현재 처리 중인 주문 수량 먼저 입력해 주세요."
                    )
            return

        # 임계값 변경 제안 콜백
        if data.startswith("th_apply:"):
            _, code, field, new_val_str = data.split(":", 3)
            try:
                new_val = float(new_val_str) if "." in new_val_str else int(new_val_str)
            except ValueError:
                self._answer_callback(callback_id, "값 파싱 오류")
                return
            self._apply_threshold_change(callback_id, code, field, new_val)
            self._update_threshold_keyboard(code, field)
            return

        if data.startswith("th_edit:"):
            parts_th = data.split(":", 3)
            if len(parts_th) != 4:
                self._answer_callback(callback_id)
                return
            _, code, field, old_val_str = parts_th
            try:
                old_val = int(old_val_str)
            except ValueError:
                old_val = 0
            from data.db import get_watchlist
            stock_name = next((s["name"] for s in get_watchlist() if s["code"] == code), code)
            from notifications.telegram import _FIELD_LABELS
            label = _FIELD_LABELS.get(field, field)
            with self._lock:
                self._waiting_threshold_edit = {"code": code, "name": stock_name, "field": field, "old": old_val}
            self._answer_callback(callback_id, f"{label} 새 값을 입력하세요.")
            self._send(
                f"*{stock_name}* `{label}` 새 값을 입력해 주세요. (현재 제안: `{old_val:,}`)\n숫자만 입력:",
                reply_markup={"inline_keyboard": [[
                    {"text": "❌ 취소", "callback_data": f"th_reject:{code}:{field}"},
                ]]}
            )
            return

        if data.startswith("th_reject:"):
            parts_th = data.split(":", 2)
            if len(parts_th) == 3:
                _, code, field = parts_th
                from notifications.telegram import _FIELD_LABELS
                label = _FIELD_LABELS.get(field, field)
                self._answer_callback(callback_id, f"{label} 변경 제외됨")
                self._update_threshold_keyboard(code, field)
            else:
                self._answer_callback(callback_id)
            with self._lock:
                self._waiting_threshold_edit = None
            return

        if data.startswith("th_reject_all:"):
            code = data.split(":", 1)[1]
            self._answer_callback(callback_id, "임계값 변경 전체 취소됨")
            proposal = _threshold_proposals.pop(code, None)
            if proposal:
                self._remove_inline_keyboard(proposal["msg_id"])
            with self._lock:
                self._waiting_threshold_edit = None
            self._send("❌ 임계값 변경 제안을 모두 취소했습니다.")
            return

        # ── 스크리닝 콜백 (관심종목 등록/패스) ──
        if data.startswith("screen_add:"):
            code = data.split(":", 1)[1]
            from worker.stock_analyzer import handle_screening_callback
            result = handle_screening_callback(code, "add")
            self._answer_callback(callback_id, result)
            self._send(result)
            return

        if data.startswith("screen_pass:"):
            code = data.split(":", 1)[1]
            from worker.stock_analyzer import handle_screening_callback
            result = handle_screening_callback(code, "pass")
            self._answer_callback(callback_id, result)
            self._send(result)
            return

        parts = data.split(":")
        if len(parts) < 2:
            self._answer_callback(callback_id)
            return
        action, code = parts[0], parts[1]
        _signal_id = None
        name = _resolve_name_by_code(code, kiwoom=self._kiwoom)

        # 신규 포맷: action:code:signal_id
        if len(parts) == 3 and parts[2].isdigit():
            _signal_id = int(parts[2]) if parts[2] != "0" else None
        # 레거시 포맷: action:code:name[:signal_id]
        elif len(parts) >= 3:
            if parts[-1].isdigit():
                _signal_id = int(parts[-1]) if parts[-1] != "0" else None
                legacy_name = ":".join(parts[2:-1]).strip()
            else:
                legacy_name = ":".join(parts[2:]).strip()
            if legacy_name:
                name = legacy_name

        if action == "hold":
            self._answer_callback(callback_id, "홀드 유지.")
            try:
                from data.db import shorten_cooldowns_for_stock, update_signal_action
                cnt = shorten_cooldowns_for_stock(code)
                logger.info(f"[bot] 홀드({name}) — 쿨다운 {cnt}건 단축 (원래의 25%)")
                if _signal_id is not None:
                    update_signal_action(_signal_id, "홀드")
            except Exception as e:
                logger.warning(f"[bot] 홀드 처리 실패: {e}")
            self._send(f"⏸ *{name}* 홀드 유지.\n_(신호 유지 시 조건별 쿨다운의 25% 후 재알림)_")
        elif action in ("buy_market", "buy_limit", "sell_market", "sell_limit"):
            if not ALLOW_TRADE:
                self._answer_callback(callback_id, "매매 비활성화 상태입니다.")
                self._send("🔒 매매 실행이 비활성화되어 있습니다.")
                return
            order_action = "buy" if action.startswith("buy") else "sell"
            price_type = "limit" if action.endswith("limit") else "market"
            side = "매수" if order_action == "buy" else "매도"
            self._answer_callback(callback_id, f"{side} 주문시장 선택.")
            self._prompt_market_selection(
                order_action=order_action,
                price_type=price_type,
                code=code,
                name=name,
                signal_id=_signal_id,
            )
        else:
            self._answer_callback(callback_id)

    def _apply_threshold_change(self, callback_id: str, code: str, field: str, new_val: int) -> None:
        """임계값 변경 적용 + 전략 노트 저장 (전환조건 텍스트를 detail로 사용)."""
        from data.db import update_stock_field, get_watchlist, save_strategy_note
        from notifications.telegram import _FIELD_LABELS
        stock_name = next((s["name"] for s in get_watchlist() if s["code"] == code), code)
        label = _FIELD_LABELS.get(field, field)
        ok = update_stock_field(code, field, new_val)
        if ok:
            # 전환조건 텍스트를 detail로 — 없으면 기본 메시지
            condition_text = (_threshold_proposals.get(code) or {}).get("condition_text", "")
            save_strategy_note(
                category="watchlist",
                summary=f"{stock_name} {label} → {new_val:,} (AI 전환조건 적용)",
                detail=condition_text or f"AI 홀드 전환조건에 따라 {field}={new_val} 적용",
            )
            val_str = str(new_val) if isinstance(new_val, float) else f"{new_val:,}"
            self._answer_callback(callback_id, f"{label} {val_str} 적용 완료")
            self._send(f"✅ *{stock_name}* `{label}` → `{val_str}` 으로 변경 완료\n_(전략 노트 기록됨)_")
            logger.info(f"[bot] 임계값 변경: {code} {field}={new_val}")
        else:
            self._answer_callback(callback_id, "변경 실패")
            self._send(f"❌ `{label}` 변경 실패 — 종목 코드를 확인해 주세요.")

    # ── 명령 처리 ──────────────────────────────────────────────────────────

    def _handle(self, text: str) -> None:
        text = text.strip()

        # ── "취소" 텍스트 입력 → 전체 흐름 종료 ─────────────────────────
        if text in ("취소", "취소", "/cancel"):
            self._cmd_cancel()
            return

        # ── 임계값 수정 입력 대기 처리 ──────────────────────────────────
        with self._lock:
            wth = self._waiting_threshold_edit
        if wth and text.isdigit():
            new_val = int(text)
            if new_val <= 0:
                self._send("❌ 값은 양의 정수여야 합니다. 다시 입력해 주세요.")
                return
            with self._lock:
                self._waiting_threshold_edit = None
            self._apply_threshold_change("", wth["code"], wth["field"], new_val)
            self._update_threshold_keyboard(wth["code"], wth["field"])
            return

        # ── 지정가 입력 대기 처리 (수량 큐보다 먼저 확인) ──────────────
        with self._lock:
            wp = self._waiting_price
        if wp and text.isdigit():
            limit_price = int(text)
            if limit_price <= 0:
                self._send("❌ 가격은 양의 정수여야 합니다. 다시 입력해 주세요.")
                return
            with self._lock:
                self._waiting_price = None
                pre_qty = wp.get("qty")  # rec 버튼으로 온 경우 수량 pre-stored
            side = "매수" if wp["order_type"] == "1" else "매도"
            order_market = wp.get("order_market")
            if pre_qty:
                # 추천수량 pre-stored → 수량 입력 생략, 바로 주문 확인으로
                self._cmd_order(
                    wp["order_type"], wp["code"], str(pre_qty),
                    price_type="limit", limit_price=limit_price,
                    signal_id=wp.get("signal_id"), order_market=order_market,
                )
            else:
                # 일반 지정가 → 수량 입력 단계로
                with self._lock:
                    self._waiting_qty_queue.append({
                        "action": "buy" if wp["order_type"] == "1" else "sell",
                        "price_type": "limit",
                        "limit_price": limit_price,
                        "code": wp["code"],
                        "name": wp["name"],
                        "signal_id": wp.get("signal_id"),
                        "order_market": order_market,
                    })
                self._send(
                    f"*{wp['name']}* {side} (지정가 {limit_price:,}원, {order_market or '기본'}) — 수량을 입력해 주세요.\n"
                    f"(숫자만, 예: `100`)",
                    reply_markup={"inline_keyboard": [[
                        {"text": "❌ 취소", "callback_data": "cancel_order"},
                    ]]}
                )
            return

        # ── 수량 대기 큐 처리 ────────────────────────────────────────────
        with self._lock:
            wq = self._waiting_qty_queue[0] if self._waiting_qty_queue else None
        if wq and text.isdigit():
            with self._lock:
                self._waiting_qty_queue.pop(0)
                remaining = list(self._waiting_qty_queue)
            qty = int(text)
            order_type = "1" if wq["action"] == "buy" else "2"
            price_type = wq.get("price_type", "market")

            limit_price = wq.get("limit_price", 0)
            self._cmd_order(
                order_type, wq["code"], str(qty),
                price_type=price_type, limit_price=limit_price,
                signal_id=wq.get("signal_id"), order_market=wq.get("order_market"),
            )
            if remaining:
                next_wq = remaining[0]
                next_side = "매수" if next_wq["action"] == "buy" else "매도"
                next_price = "지정가" if next_wq.get("price_type") == "limit" else "시장가"
                next_market = next_wq.get("order_market", "기본")
                self._send(
                    f"📋 대기 중인 주문 {len(remaining)}건\n"
                    f"다음: *{next_wq['name']}* {next_side}({next_price}, {next_market}) — 수량을 입력해 주세요."
                )
            return

        # ── 종목명 대기 상태 처리 ─────────────────────────────────────────
        with self._lock:
            ws = self._waiting_stock_input
        if ws and not text.startswith("/"):
            # price 모드는 sticky (연속 조회 가능) — buy/sell은 1회 소비
            if ws["cmd"] != "price":
                with self._lock:
                    self._waiting_stock_input = None
            cmd_type = ws["cmd"]
            if cmd_type == "price":
                self._cmd_price(text)
            elif cmd_type in ("buy", "sell"):
                try:
                    code, name = _resolve_stock(text, kiwoom=self._kiwoom)
                except ValueError as e:
                    self._send(f"❌ {e}")
                    return
                order_type_str = "buy" if cmd_type == "buy" else "sell"
                side = "매수" if cmd_type == "buy" else "매도"
                session = _get_order_session()
                session_label = _SESSION_LABELS.get(session, "")
                if session == "main":
                    buttons = [
                        {"text": "📊 시장가", "callback_data": f"{order_type_str}_market:{code}:{name}"},
                        {"text": "💰 지정가", "callback_data": f"{order_type_str}_limit:{code}:{name}"},
                        {"text": "❌ 취소",   "callback_data": "cancel_order"},
                    ]
                elif session in ("premarket", "aftermarket"):
                    buttons = [
                        {"text": f"📋 종가 주문 ({session_label})", "callback_data": f"{order_type_str}_market:{code}:{name}"},
                        {"text": "❌ 취소", "callback_data": "cancel_order"},
                    ]
                else:  # offhours — 지정가 필수
                    buttons = [
                        {"text": "💰 지정가 (시간외단일가)", "callback_data": f"{order_type_str}_limit:{code}:{name}"},
                        {"text": "❌ 취소", "callback_data": "cancel_order"},
                    ]
                self._send(
                    f"*{name}* (`{code}`) {side}\n주문 방식을 선택해 주세요.\n⏰ 현재: *{session_label}*",
                    reply_markup={"inline_keyboard": [buttons]}
                )
            return

        # ── 명령어 처리 ───────────────────────────────────────────────────
        parts = text.split()
        if not parts:
            return
        cmd = parts[0].lower().split("@")[0]  # /buy@botname → /buy

        if cmd == "/help":
            self._cmd_help()
        elif cmd == "/buy":
            if len(parts) == 3:
                self._cmd_order("1", parts[1], parts[2])
            else:
                with self._lock:
                    self._waiting_stock_input = {"cmd": "buy"}
                self._send(
                    "📈 *매수* — 종목명 또는 코드를 입력하세요.",
                    reply_markup={"inline_keyboard": [[
                        {"text": "🔍 종목 검색", "switch_inline_query_current_chat": ""},
                        {"text": "❌ 취소", "callback_data": "cancel_order"},
                    ]]}
                )
        elif cmd == "/sell":
            if len(parts) == 3:
                self._cmd_order("2", parts[1], parts[2])
            else:
                with self._lock:
                    self._waiting_stock_input = {"cmd": "sell"}
                self._send(
                    "📉 *매도* — 종목명 또는 코드를 입력하세요.",
                    reply_markup={"inline_keyboard": [[
                        {"text": "🔍 종목 검색", "switch_inline_query_current_chat": ""},
                        {"text": "❌ 취소", "callback_data": "cancel_order"},
                    ]]}
                )
        elif cmd == "/confirm":
            self._cmd_confirm()
        elif cmd == "/cancel":
            self._cmd_cancel()
        elif cmd == "/status":
            self._cmd_status()
        elif cmd == "/price":
            if len(parts) == 2:
                self._cmd_price(parts[1])
            else:
                with self._lock:
                    self._waiting_stock_input = {"cmd": "price"}
                self._send(
                    "💰 *현재가 조회* — 종목명 또는 코드를 입력하세요.",
                    reply_markup={"inline_keyboard": [[
                        {"text": "🔍 종목 검색", "switch_inline_query_current_chat": ""},
                    ]]}
                )
        elif cmd in ("/balance", "/잔고"):
            self._cmd_balance()
        elif cmd == "/issues":
            self._cmd_issues(parts[1:] if len(parts) > 1 else [])
        elif cmd == "/issue_done":
            if len(parts) >= 2:
                self._cmd_issue_done(parts[1], " ".join(parts[2:]) if len(parts) > 2 else "")
            else:
                self._send("사용법: `/issue_done 3` 또는 `/issue_done 3 완료메모`")
        elif text.startswith("/"):
            self._send("❓ 알 수 없는 명령어입니다.\n/help 로 사용법을 확인하세요.")
        else:
            # 비명령어 텍스트 — 종목명/코드로 간주해 현재가 조회 시도
            self._cmd_price(text)

    def _cmd_help(self) -> None:
        self._send(
            "*📋 Quant Bot 명령어*\n\n"
            "`/buy 종목명or코드 수량` — 시장가 매수 요청\n"
            "`/sell 종목명or코드 수량` — 시장가 매도 요청\n"
            "`/status` — 대기 중인 주문 확인\n"
            "`/confirm` — 대기 중인 주문 실행 (60초 유효)\n"
            "`/cancel` — 대기 중인 주문 취소\n"
            "`/price 종목명or코드` — 현재가 조회\n"
            "`/balance` — 잔고 및 보유 종목 조회\n\n"
            "`/issues` — 운영 이슈(open) 조회\n"
            "`/issues all` — 전체 이슈 조회\n"
            "`/issue_done ID [메모]` — 이슈 완료 처리\n\n"
            "💡 종목명 전체 검색 가능 (부분 일치 지원)\n"
            "🔒 매매 실행: " + ("*활성화*" if ALLOW_TRADE else "*비활성화* (KIWOOM\\_ALLOW\\_TRADE\\_EXECUTION=true 필요)")
        )

    def _cmd_issues(self, args: list[str]) -> None:
        try:
            from data.db import get_improvement_issues
            mode = (args[0].strip().lower() if args else "open")
            status = "all" if mode in ("all", "전체") else "open"
            rows = get_improvement_issues(status=status, limit=15)
            if not rows:
                self._send("📭 조회된 이슈가 없습니다.")
                return
            lines = [f"🛠 *Improvement Issues* ({status})", ""]
            for r in rows:
                iid = r.get("id")
                pri = r.get("priority", "P2")
                st = r.get("status", "open")
                title = str(r.get("title", "")).strip()
                lines.append(f"`#{iid}` [{pri}/{st}] {title}")
            self._send("\n".join(lines))
        except Exception as e:
            self._send(f"❌ 이슈 조회 실패: `{e}`")

    def _cmd_issue_done(self, issue_id_text: str, note: str = "") -> None:
        try:
            issue_id = int(str(issue_id_text).strip())
        except Exception:
            self._send("❌ 이슈 ID는 숫자로 입력해 주세요. 예: `/issue_done 3`")
            return
        try:
            from data.db import update_improvement_issue_status
            ok = update_improvement_issue_status(issue_id, "done", resolved_note=(note or "telegram_done"))
            if not ok:
                self._send(f"⚠️ 이슈 `#{issue_id}` 상태 변경 실패 (ID 확인 필요)")
                return
            self._send(f"✅ 이슈 `#{issue_id}` 완료 처리했습니다.")
        except Exception as e:
            self._send(f"❌ 이슈 완료 처리 실패: `{e}`")

    def _cmd_status(self) -> None:
        with self._lock:
            order = self._pending

        if order is None:
            self._send("📭 대기 중인 주문이 없습니다.")
            return

        if order.expired:
            with self._lock:
                self._pending = None
            self._send("⏰ 대기 주문이 만료되었습니다. 다시 입력해 주세요.")
            return

        remaining = CONFIRM_TIMEOUT_SEC - int((datetime.now() - order.created_at).total_seconds())
        self._send(
            f"⏳ *대기 중인 주문*\n\n"
            f"종목: *{order.stock_name}* (`{order.stock_code}`)\n"
            f"수량: *{order.qty:,}주* ({order.side_label} {order.price_type})\n"
            f"주문시장: *{order.order_market or '기본'}*\n"
            f"남은 시간: *{remaining}초*\n\n"
            f"✅ `/confirm` — 실행 | ❌ `/cancel` — 취소"
        )

    def _cmd_order(
        self,
        order_type: str,
        name_or_code: str,
        qty_str: str,
        price_type: str = "market",
        limit_price: int = 0,
        signal_id: int | None = None,
        order_market: str | None = None,
    ) -> None:
        if not ALLOW_TRADE:
            self._send(
                "🔒 매매 실행이 비활성화되어 있습니다.\n"
                "`.env` 에 `AUTO_TRADE=true` 설정 후 워커를 재시작하세요."
            )
            return

        try:
            qty = int(qty_str)
            if qty <= 0:
                raise ValueError
        except ValueError:
            self._send("❌ 수량은 양의 정수여야 합니다.\n예: `/buy 삼성전자 100`")
            return

        try:
            code, stock_name = _resolve_stock(name_or_code, kiwoom=self._kiwoom)
        except ValueError as e:
            self._send(f"❌ {e}")
            return

        side = "매수" if order_type == "1" else "매도"
        market_label = (order_market or "기본").upper()

        # 현재가 조회 → 예상 총액 (종목명 보정용으로도 사용)
        cur_prc = 0
        if self._kiwoom:
            try:
                pd = self._kiwoom.get_current_price(code)
                cur_prc = abs(int(str(
                    pd.get("cur_prc") or pd.get("stk_prpr") or pd.get("prpr") or "0"
                ).replace(",", "")))
                api_name = str(pd.get("stk_nm") or pd.get("hts_kor_isnm") or "")
                if api_name:
                    stock_name = api_name
            except Exception:
                pass

        # 세션별 가격 레이블 결정
        session = _get_order_session()
        if price_type == "limit" and limit_price > 0:
            exec_prc = limit_price
            price_label = f"지정가 {exec_prc:,}원"
        elif session == "premarket":
            exec_prc = 0
            price_label = "종가 주문 (전일 종가)"
        elif session == "aftermarket":
            exec_prc = 0
            price_label = "종가 주문 (당일 종가)"
        elif session == "offhours":
            exec_prc = 0
            price_label = "시간외단일가 (현재가 자동)"
        else:
            exec_prc = 0
            price_label = "시장가"

        ref_prc = exec_prc if exec_prc else cur_prc
        total_amt = ref_prc * qty if ref_prc else 0
        total_line = ""
        if cur_prc:
            if price_type == "limit" and exec_prc and exec_prc != cur_prc:
                total_line = f"\n💰 현재가: *{cur_prc:,}원* | 지정가: *{exec_prc:,}원* | 예상 총액: *{total_amt:,}원*"
            else:
                total_line = f"\n💰 현재가: *{cur_prc:,}원* | 예상 총액: *{total_amt:,}원*"

        # 예수금 조회
        deposit_line = ""
        if self._kiwoom:
            try:
                dep = self._kiwoom.get_deposit()
                avail = dep.get("order_available", 0)
                if avail:
                    after = avail - total_amt if order_type == "1" else avail + total_amt
                    deposit_line = (
                        f"\n🏦 주문가능: *{avail:,}원*"
                        f" → 주문 후: *{after:,}원*"
                    )
            except Exception:
                pass

        with self._lock:
            self._pending = _PendingOrder(
                code, stock_name, order_type, qty,
                price_type=price_type, limit_price=exec_prc,
                signal_id=signal_id, order_market=order_market,
            )

        confirm_markup = {
            "inline_keyboard": [[
                {"text": "✅ 최종 확인", "callback_data": "confirm_order"},
                {"text": "❌ 취소", "callback_data": "cancel_order"},
            ]]
        }
        self._send(
            f"⚠️ *{side} 주문 최종 확인*\n\n"
            f"종목: *{stock_name}* (`{code}`)\n"
            f"수량: *{qty:,}주* ({price_label})\n"
            f"주문시장: *{market_label}*"
            f"{total_line}"
            f"{deposit_line}\n\n"
            f"⏱ {CONFIRM_TIMEOUT_SEC}초 내에 확인하세요.",
            reply_markup=confirm_markup,
        )

    def _cmd_confirm(self, price_type: str | None = None) -> None:
        with self._lock:
            order = self._pending
            self._pending = None

        if order is None:
            self._send("📭 대기 중인 주문이 없습니다.")
            return

        if order.expired:
            self._send(f"⏰ 주문이 만료되었습니다. ({CONFIRM_TIMEOUT_SEC}초 초과)\n다시 입력해 주세요.")
            return

        if not self._kiwoom:
            self._send("❌ 키움 클라이언트가 연결되어 있지 않습니다.")
            return

        # price_type: 인자 우선, 없으면 pending order의 값 사용
        effective_price_type = price_type if price_type is not None else order.price_type

        # 지정가: 사용자가 입력한 가격 사용 (limit_price)
        exec_price = 0
        price_label = "시장가"
        if effective_price_type == "limit":
            exec_price = order.limit_price
            if exec_price:
                price_label = f"지정가 {exec_price:,}원"
            else:
                # 가격 정보 없으면 시장가로 폴백
                logger.warning("[bot] 지정가 주문인데 limit_price=0 — 시장가로 대신 실행")
                self._send("⚠️ 지정가 정보 없음 — 시장가로 대신 실행합니다.")

        self._send(
            f"⏳ *{order.stock_name}* {order.qty:,}주 {order.side_label} "
            f"({price_label}, {order.order_market or '기본'}) 주문 전송 중..."
        )

        try:
            result = self._kiwoom.place_order(
                order.stock_code, order.order_type, order.qty,
                price=exec_price, order_market=order.order_market,
            )
            ord_no = result.get("ord_no") or result.get("order_no") or "-"
            self._send(
                f"✅ *{order.side_label} 주문 접수 완료*\n\n"
                f"종목: *{order.stock_name}* (`{order.stock_code}`)\n"
                f"수량: *{order.qty:,}주* ({price_label})\n"
                f"주문시장: *{order.order_market or '기본'}*\n"
                f"주문번호: `{ord_no}`"
            )
            logger.info(f"[bot] {order.side_label} 주문 완료: {order.stock_name} {order.qty}주 → 주문번호 {ord_no}")

            # 모의투자: 체결내역 API 미지원 → trades 테이블에 직접 기록
            if getattr(self._kiwoom, "_is_mock", False):
                try:
                    from data.db import insert_trade_direct
                    insert_trade_direct(
                        trade_id=ord_no,
                        stock_code=order.stock_code,
                        stock_name=order.stock_name,
                        side=order.side_label,
                        quantity=order.qty,
                        price=exec_price,
                    )
                    logger.info(f"[bot] 모의투자 체결 DB 저장: {order.stock_name} {order.qty}주 {order.side_label} (ord_no={ord_no})")
                except Exception as e:
                    logger.warning(f"[bot] 모의투자 체결 DB 저장 실패: {e}")

            # 체결 후 해당 종목 쿨다운 리셋 — 새 포지션 기준으로 신호 재시작
            try:
                from data.db import reset_cooldowns_for_stock, update_signal_action, set_add_cooldown_after_trade
                cnt = reset_cooldowns_for_stock(order.stock_code)
                logger.info(f"[bot] 체결 후 쿨다운 리셋: {order.stock_name} {cnt}건")
                # 매수 후 60분간 add/both 신호 억제 (직후 물타기 신호 노이즈 방지)
                if order.order_type == "1":
                    add_cnt = set_add_cooldown_after_trade(order.stock_code, suppress_minutes=60)
                    logger.info(f"[bot] 매수 후 add 신호 억제 설정: {order.stock_name} {add_cnt}건 (60분)")
                if order.signal_id is not None:
                    action_label = "매수" if order.order_type == "1" else "매도"
                    update_signal_action(order.signal_id, action_label)
            except Exception:
                pass

            # 매수 후 포지션 자동 생성 + AI 판단
            if order.order_type == "1":
                try:
                    from data.db import create_position_from_trade
                    created = create_position_from_trade(order.stock_code, order.stock_name, 0, order.qty)
                    if created:
                        logger.info(f"[bot] 매수 후 포지션 생성: {order.stock_name}")
                    # AI 포지션 판단 (별도 스레드로 비동기 실행)
                    import threading
                    def _ai_position():
                        try:
                            from worker.claude_judge import judge_position_values
                            from data.db import update_position_field, save_strategy_note, get_position
                            import time
                            time.sleep(5)  # portfolio_sync 후 평단가 갱신 대기
                            pos = get_position(order.stock_code)
                            avg_price = pos["avg_price"] if pos and pos.get("avg_price") else 0
                            result = judge_position_values(order.stock_code, order.stock_name, avg_price, order.qty)
                            if result:
                                for k in ("target_price", "stop_loss_price", "add_buy_price"):
                                    if result.get(k):
                                        update_position_field(order.stock_code, k, result[k])
                                detail = "\n".join(
                                    f"{k}: {result[k]:,}원 — {result.get(k.replace('_price','_reason'), result.get(k.replace('price','reason'), ''))}"
                                    for k in ("target_price", "stop_loss_price", "add_buy_price") if result.get(k)
                                )
                                save_strategy_note("watchlist", f"{order.stock_name} 포지션 AI 설정", detail)
                                tp = result.get("target_price", 0)
                                sl = result.get("stop_loss_price", 0)
                                ab = result.get("add_buy_price", 0)
                                msg = f"📌 *{order.stock_name}* 포지션 AI 설정\n"
                                if tp: msg += f"• 목표가 *{tp:,}원* — {result.get('target_reason', '')}\n"
                                if sl: msg += f"• 손절가 *{sl:,}원* — {result.get('stop_loss_reason', '')}\n"
                                if ab: msg += f"• 추가매수 *{ab:,}원* — {result.get('add_buy_reason', '')}"
                                self._send(msg)
                                logger.info(f"[bot] AI 포지션 설정: {order.stock_name} 목표={tp:,} 손절={sl:,}")
                        except Exception as e:
                            logger.error(f"[bot] AI 포지션 설정 실패: {e}")
                    threading.Thread(target=_ai_position, daemon=True).start()
                except Exception:
                    pass

            # 전략 노트 기록
            try:
                from data.db import save_strategy_note
                save_strategy_note(
                    "trade",
                    f"{order.stock_name} {order.qty}주 {order.side_label} (텔레그램 봇)",
                    f"주문번호: {ord_no}",
                )
            except Exception:
                pass

        except Exception as e:
            logger.error(f"[bot] 주문 실패: {e}")
            self._send(f"❌ 주문 실패\n\n`{e}`")

    def _cmd_cancel(self) -> None:
        with self._lock:
            order = self._pending
            self._pending = None
            ws = self._waiting_stock_input
            self._waiting_stock_input = None
            self._waiting_qty_queue.clear()
            self._waiting_price = None
            self._waiting_threshold_edit = None

        if ws and ws["cmd"] == "price":
            self._send("💰 현재가 조회 모드 종료.")
        elif order is None:
            self._send("📭 대기 중인 주문이 없습니다.")
        else:
            self._send(f"❌ *{order.stock_name}* {order.qty:,}주 {order.side_label} 주문을 취소했습니다.")

    def _cmd_price(self, name_or_code: str) -> None:
        if not self._kiwoom:
            self._send("❌ 키움 클라이언트가 연결되어 있지 않습니다.")
            return
        try:
            try:
                code, _ = _resolve_stock(name_or_code, kiwoom=self._kiwoom)
            except ValueError as e:
                self._send(f"❌ {e}")
                return
            data = self._kiwoom.get_current_price(code)
            cur_prc = abs(int(str(data.get("cur_prc") or data.get("stk_prpr") or data.get("prpr") or "0").replace(",", "")))
            name = str(data.get("stk_nm") or data.get("hts_kor_isnm") or code)
            chg = str(data.get("fluc_rt") or data.get("prdy_ctrt") or "")
            chg_str = f" ({chg}%)" if chg else ""
            self._send(f"💰 *{name}* (`{code}`)\n현재가: *{cur_prc:,}원*{chg_str}")
        except Exception as e:
            self._send(f"❌ 현재가 조회 실패: `{e}`")

    def _cmd_balance(self) -> None:
        if not self._kiwoom:
            self._send("❌ 키움 클라이언트가 연결되어 있지 않습니다.")
            return
        try:
            holdings = self._kiwoom.get_holdings()
            if not holdings:
                self._send("📭 보유 종목이 없습니다.")
                return

            lines = ["*📊 보유 종목*\n"]
            for h in holdings:
                name = str(h.get("stk_nm") or h.get("hts_kor_isnm") or "")
                raw_code = str(h.get("stk_cd") or "")
                # 종목코드 앞 'A' 제거 (키움 응답 특성)
                code = raw_code.lstrip("A")
                qty_raw = str(h.get("rmnd_qty") or h.get("hldg_qty") or "0")
                avg = str(h.get("avg_prc") or h.get("pchs_avg_pric") or "")
                cur = str(h.get("cur_prc") or h.get("prpr") or "")

                qty_i = int(qty_raw.replace(",", "").lstrip("0") or "0")
                try:
                    avg_i = abs(int(str(avg).replace(",", "") or "0"))
                    cur_i = abs(int(str(cur).replace(",", "") or "0"))
                    pl_pct = ((cur_i - avg_i) / avg_i * 100) if avg_i else 0
                    lines.append(
                        f"• *{name}* (`{code}`)\n"
                        f"  {qty_i:,}주 | 평균 {avg_i:,}원 → {cur_i:,}원 ({pl_pct:+.1f}%)"
                    )
                except Exception:
                    lines.append(f"• *{name}* (`{code}`) {qty_i:,}주")

            self._send("\n".join(lines))
        except Exception as e:
            self._send(f"❌ 잔고 조회 실패: `{e}`")

    # ── 폴링 루프 ──────────────────────────────────────────────────────────

    def run(self) -> None:
        """블로킹 폴링 루프 — 별도 스레드에서 실행"""
        if not BOT_TOKEN or not CHAT_ID:
            logger.warning("[bot] TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID 미설정 — 봇 비활성화")
            return

        logger.info("[bot] 텔레그램 봇 시작 (long-polling)")

        self._send("🤖 *Quant Bot 시작*\n`/help` 로 명령어를 확인하세요.")

        while True:
            updates = self._get_updates()
            for update in updates:
                self._offset = update["update_id"] + 1

                # ── inline_query (종목 검색 자동완성) ──────────────────────
                iq = update.get("inline_query")
                if iq:
                    from_id = str(iq.get("from", {}).get("id", ""))
                    try:
                        self._handle_inline_query(iq["id"], from_id, iq.get("query", ""))
                    except Exception as e:
                        logger.error(f"[bot] 인라인 쿼리 처리 오류: {e}")
                    continue

                # ── callback_query (인라인 버튼 클릭) ──────────────────────
                cb = update.get("callback_query")
                if cb:
                    sender = str(cb.get("message", {}).get("chat", {}).get("id", ""))
                    if sender != CHAT_ID:
                        logger.warning(f"[bot] 허가되지 않은 callback chat_id={sender} 무시")
                        continue
                    logger.info(f"[bot] 콜백: {cb.get('data')!r}")
                    try:
                        msg_id = cb.get("message", {}).get("message_id")
                        self._handle_callback(cb["id"], cb.get("data", ""), message_id=msg_id)
                    except Exception as e:
                        logger.error(f"[bot] 콜백 처리 오류: {e}", exc_info=True)
                    continue

                # ── 일반 메시지 ─────────────────────────────────────────────
                msg = update.get("message", {})
                sender = str(msg.get("chat", {}).get("id", ""))
                if sender != CHAT_ID:
                    logger.warning(f"[bot] 허가되지 않은 chat_id={sender} 메시지 무시")
                    continue
                text = msg.get("text", "")
                if text:
                    logger.info(f"[bot] 수신: {text!r}")
                    try:
                        self._handle(text)
                    except Exception as e:
                        logger.error(f"[bot] 명령 처리 오류: {e}", exc_info=True)

            if not updates:
                time.sleep(1)


def start_bot_thread(kiwoom_client=None) -> threading.Thread:
    """워커 main.py 에서 호출 — 데몬 스레드로 봇 실행"""
    bot = TelegramBot(kiwoom_client=kiwoom_client)
    t = threading.Thread(target=bot.run, name="telegram-bot", daemon=True)
    t.start()
    return t
