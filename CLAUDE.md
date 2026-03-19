# Quant Trading System — Claude.ai 행동 지침

## 프로젝트 개요
AI 기반 개인용 퀀트 트레이딩 시스템.
키움증권 MCP로 계좌조회/매매 가능.
백그라운드 워커가 24/7 조건 감지 → 텔레그램 알림 → 텔레그램 봇으로 주문 실행.

---

## 핵심 원칙: 결정이 나면 즉시 반영

대화 중 아래 상황이 발생하면 사용자가 별도로 지시하지 않아도 자동으로 처리한다.
모든 기록/알림은 MCP 도구를 직접 호출하여 처리한다 (bash 명령 사용 금지).

### 주식 실시간 정보는 반드시 키움 MCP로 조회

**현재가, 잔고, 체결, 주문, 계좌 정보 등 주식 관련 실시간 데이터는 반드시 `kiwoom_execute_api` 도구를 직접 호출하여 확인한다.**
DB(portfolio 테이블)는 동기화 시점의 스냅샷이므로 현재가·수익률 등 실시간 수치는 부정확할 수 있다. 절대로 DB 캐시 값만으로 답하지 말 것.

| 조회 내용 | 키움 TR | 비고 |
|----------|---------|------|
| 종목 현재가 | `ka10001` | 단일 종목 현재가 |
| 일봉 차트 | `ka10081` | N일 OHLCV |
| 계좌 잔고/보유 종목 | `kt00018` | 실시간 잔고 |
| 주문 체결 내역 | `kt00007` | 당일 체결 |

트리거 예시:
- "지금 XX 얼마야?", "현재가 알려줘"
- "지금 수익률 어때?", "평가손익 얼마야?"
- "잔고 얼마나 있어?", "계좌 확인해줘"
- "오늘 체결된 거 있어?"

### 보유 여부 확인이 필요한 경우

특정 종목의 보유 여부가 대화 맥락에서 필요하면 반드시 `quant_report(type="portfolio")`를 먼저 호출하여 확인한다. 추측하거나 "모른다"고 답하지 말 것.

트리거 예시:
- "이 종목 가지고 있어?"
- "에프에스티 미보유지?"
- "XX 종목 지금 몇 주야?"
- 신호 수신 후 보유/미보유 판단이 필요한 모든 경우

### 전략/매매 맥락 파악이 필요한 경우

매매 검토·전략 수립·신호 해석 등 판단이 필요한 대화가 시작되면 `quant_report(type="strategy")`를 자동으로 호출해 최근 전략 노트를 파악한다. 이전 결정과 일관성 있는 의견을 제공하기 위해서다.

트리거 예시:
- 신호 수신 후 매수/매도 검토 시
- "전략 어떻게 가져가면 돼?"
- "지금 시장 어떻게 봐?"
- 종목 매매 근거를 물을 때

---

## 상황 1: 조건 변경 결정

**트리거 예시** (이런 말이 나오면 행동)
- "손절가를 XX원으로 낮출게"
- "이 종목 좀 더 타이트하게 관리하자"
- "RSI 기준을 65로 바꾸는 게 낫겠다"
- "목표가 올려야 할 것 같아"
- "이 종목 모니터링 잠깐 꺼둬"

**자동으로 할 것**
1. `quant_watchlist_update` 도구로 DB 즉시 반영 (텔레그램 발송 없음)
2. 모든 변경이 끝난 후 `quant_strategy_log` 1회 호출로 전체 요약 발송:
   - category: `"watchlist"`
   - summary: 변경 내용 전체 요약 한 줄 (예: "한화에어로 손절 상향, LIG 목표가 하향")
   - detail: 대화에서 나온 변경 이유

---

## 상황 2: 매매 실행

**트리거**: kiwoom MCP로 매수/매도 주문 실행 후

**자동으로 할 것**
1. `quant_strategy_log` 도구로 전략 노트 기록 + 텔레그램 발송:
   - category: `"trade"`
   - summary: "종목명 N주 매수/매도"
   - detail: 대화에서 나온 매매 근거
2. `quant_portfolio_sync` 도구로 포트폴리오 DB 동기화

---

## 상황 3: 신호 수신 후 매매 검토

**signal_type 구분**
- `entry`: 미보유 종목 → 신규 매수 타이밍
- `exit`: 보유 종목 → 매도·익절·손절 검토
- `add`: 보유 종목 → 물타기·추가매수 검토
- `both`: 보유/미보유 모두 적용

**미보유 종목 entry 신호 수신 시**
1. `quant_report(type="signals", days=1)`로 최근 신호 확인
2. `quant_watchlist_read()`로 해당 종목 목표가/손절가/조건 확인
3. 매수 결정이 나면 → **상황 2** 흐름으로 처리

**보유 종목 exit/add 신호 수신 시**
- `exit` 신호: 매도·홀드 검토
- `add` 신호: 추가매수·물타기 검토
- entry 신호만 발생했다면 워커가 이미 필터링했으므로 알림 미발송 (정상)

---

## 상황 4: 전략 수립/변경 (전략메모)

**트리거 예시**
- "방산주 비중을 늘리기로 했어"
- "당분간 현금 비중 높게 가져갈게"
- "이번 분기 전략은 XX로 하자"

**자동으로 할 것**
1. `quant_strategy_log` 도구로 전략 노트 기록 + 텔레그램 발송:
   - category: `"general"`
   - summary: 전략 요약
   - detail: 결정 근거
2. 전략 내용에 아래 항목이 포함된 경우 `quant_watchlist_update`로 관심종목에도 즉시 반영:

| 전략 내용 예시 | watchlist 반영 |
|---|---|
| "삼성전자 목표가 90000으로" | `target_price = 90000` |
| "SK하이닉스 손절 83만" | `stop_loss_price = 830000` |
| "한화에어로 RSI 기준 65로" | `rsi_overbought = 65` |
| "LIG 모니터링 꺼둬" | `enabled = false` |
| "이 종목 비중 줄이자" → 목표가/손절가 조정 수반 시 | 해당 필드 업데이트 |

> 수치·종목이 명확하지 않으면 반영하지 말고 사용자에게 확인 후 처리.

---

## 상황 5: 현황 조회 요청

**트리거 예시**
- "오늘 신호 뭐 왔어?", "최근 신호 보여줘"
- "포트폴리오 어때?", "지금 종목 현황은?"
- "최근 매매 내역 보여줘"
- "전략 노트 보여줘"
- "전체 현황 요약해줘"

**자동으로 할 것** (`quant_report` 도구 호출)

| 요청 | 도구 호출 |
|------|----------|
| 오늘 신호 | `quant_report(type="signals", days=1)` |
| 최근 N일 신호 | `quant_report(type="signals", days=N)` |
| 포트폴리오 | `quant_report(type="portfolio")` |
| 최근 매매 내역 | `quant_report(type="trades")` |
| 전략 노트 | `quant_report(type="strategy")` |
| 전체 요약 | `quant_report(type="all")` |

---

## 상황 6: 관심종목/조건 조회 및 변경

| 요청 예시 | 도구 |
|----------|------|
| "관심종목 보여줘", "어떤 종목 보고 있어" | `quant_watchlist_read` |
| "목표가/손절가/RSI 기준 바꿔줘" | `quant_watchlist_update(stock_code, field, value)` |
| "이 종목도 모니터링해줘" | `quant_watchlist_add(code, name, conditions)` |
| "이 종목 감시 목록에서 빼줘" | `quant_watchlist_delete(stock_code)` |
| "어떤 조건으로 신호 보내?" | `quant_conditions_list` |
| "새 조건 추가해줘" | `quant_condition_add(...)` |
| "조건 쿨다운/메시지/설명 바꿔줘" | `quant_condition_update(id, ...)` |
| "조건 정의 삭제해줘" | `quant_condition_remove(id)` |

**신호 로그 (signals 테이블) — 실제 발생한 신호 이력**

> ⚠️ "시그널"이 나오면 반드시 구분할 것:
> - **신호 로그** = 실제 발생한 알림 이력 (`signals` 테이블) → `quant_signal_log_*` 도구
> - **조건 정의** = 신호를 발생시키는 규칙 (`conditions_def` 테이블) → `quant_condition_*` 도구

| 요청 예시 | 도구 |
|----------|------|
| "신호 기록 보여줘", "신호 이력 확인" | `quant_report(type="signals")` |
| "이 종목 신호 이력 지워줘" | `quant_signal_log_delete(stock_code=...)` |
| "특정 신호 1건 삭제" | `quant_signal_log_delete(signal_id=...)` |
| "오래된 신호 정리해줘" | `quant_signal_log_delete(before_date=...)` |
| "신호 로그 전체 삭제" | `quant_signal_log_delete(delete_all=True)` |

**전략 노트 (strategy_notes 테이블)**

> note_id 확인 방법: `quant_report(type="strategy")` 응답에 포함된 id 필드 사용.

| 요청 예시 | 도구 |
|----------|------|
| "전략 노트 보여줘" | `quant_report(type="strategy")` |
| "전략 노트 ID N번 수정해줘" | `quant_strategy_note_update(note_id=N, summary=..., detail=...)` |
| "전략 노트 ID N번 지워줘" | `quant_strategy_note_delete(note_id=N)` |
| "전략 노트 전부 지워줘" | `quant_strategy_note_delete(delete_all=True)` |

---

## 주요 구성

- 종목/조건 설정: DB (`data/trading.db`, SQLite) — MCP 도구로 실시간 반영
- 포트폴리오 동기화: `worker/portfolio_sync.py`
- 리포트 조회: `worker/report.py`
- 신호 로그: `data/db.py`
- 텔레그램 알림: `notifications/telegram.py` (인라인 버튼 포함)
- 텔레그램 봇 주문: `notifications/telegram_bot.py`

## 주의사항
- DB 변경은 워커 재시작 없이 60초 내 자동 반영
- 쿨다운은 매일 09:00 장 시작 시 전체 리셋
- 매매 전 `KIWOOM_ALLOW_TRADE_EXECUTION` 설정 확인 (`true`여야 실제 주문 가능)
- `.env` 내용 절대 출력 금지
