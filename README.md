# Quant Trading System

AI(Claude)를 활용한 개인용 퀀트 트레이딩 시스템.
키움증권 REST API 기반 24/7 자동 모니터링 + AI 매매 판단 → 텔레그램 봇 반자동 매매.

---

## 거래 세션

| 세션 | 시간 | 매매구분(trde_tp) | 조건 필터 |
|------|------|-----------------|----------|
| 프리장 (장전 시간외) | 08:30~09:00 | 61 (전일 종가) | exit/add/both만 |
| 정규장 | 09:00~15:30 | 0 (시장가) / 3 (지정가) | 전체 |
| 애프터장 (장후 시간외) | 15:40~16:00 | 81 (당일 종가) | exit/add/both만 |
| 시간외 단일가 | 16:00~18:00 | 62 (지정가 필수) | exit/add/both만 |

> 프리/애프터장 entry 조건 제외: 유동성 낮은 시간대 신규 매수 오발신호 방지
> 시간외 단일가 시장가 주문 시 현재가 자동 조회 후 지정가로 변환

---

## 신호 발생 흐름 (상세)

```
워커 1분 체크 (평일 08:00~18:59 cron — 장외 시간 미실행)
     │
     ├─ 현재 세션 확인 (premarket / main / aftermarket / offhours)
     │   └─ 세션 없음(갭/장외) → 즉시 return
     │
     ├─ DB에서 watchlist + conditions_def 로드 (최신 즉시 반영)
     │   └─ 정규장 외 세션: entry 조건 자동 제외
     │
     ├─ 종목별:
     │   ├─ 현재가 조회 (ka10001)
     │   ├─ 40일 일봉 조회 (ka10081)
     │   ├─ 지표 계산:
     │   │   RSI / MA5·MA20 / MACD / 볼린저 밴드
     │   │   골든크로스·데드크로스 / 신고가 / 지지선 이탈
     │   │
     │   ├─ [조건 평가] conditions_def 기반 동적 평가
     │   │   ├─ price_gte / price_lte  → 현재가 비교
     │   │   ├─ rsi_gte / rsi_lte      → RSI 비교
     │   │   ├─ volume_gte             → 거래량 배율
     │   │   └─ flag                  → 차트 불리언 필드
     │   │
     │   ├─ [signal_type 필터]
     │   │   ├─ 미보유 종목: entry / both 조건만 평가
     │   │   ├─ 보유 종목:  exit / add / both 조건만 평가
     │   │   └─ add: 물타기·추가매수 전용 조건
     │   │
     │   ├─ [쿨다운 필터] 조건별 재발송 방지
     │   │   (조건마다 쿨다운 시간 다름: 5분 ~ 6.5시간)
     │   │   (매일 09:00 장 시작 시 전체 리셋)
     │   │
     │   ├─ [AI 판단] ANTHROPIC_API_KEY → Claude Sonnet
     │   │   Input: 시장지수 + 섹터 + 차트 + 조건 + 포트폴리오
     │   │   Output: [매수/매도/홀드] + 근거 + 추천 주문방식
     │   │
     │   └─ [텔레그램 발송]
     │       메시지1: 신호 요약 (종목/가격/조건)
     │       메시지2: AI 판단 의견
     │       인라인 버튼: 시장가매수 / 지정가매수 / 매도 / 홀드
     │
     └─ 신호 DB 저장 (signals 테이블)
```

---

## 텔레그램 봇 주문 흐름

```
신호 알림 수신
     │
     ├─ [시장가 매수/매도] 버튼 → 수량 입력 → 최종 확인 → 주문
     │
     └─ [지정가 매수/매도] 버튼 → 가격 입력 → 수량 입력 → 최종 확인 → 주문

직접 명령:
  /buy 종목명  → 세션별 주문방식 선택 → (지정가면 가격 입력) → 수량 입력 → 확인
  /sell 종목명 → 위와 동일
  /price 종목명 → 현재가 조회
  /balance      → 보유 종목 조회

세션별 주문 버튼:
  정규장        → [📊 시장가] [💰 지정가]
  프리/애프터장 → [📋 종가 주문]  (가격 불필요 — 종가 자동 적용)
  시간외단일가  → [💰 지정가]     (시장가 선택 시 현재가 자동 지정가 변환)

주문 시 trde_tp 자동 선택:
  프리장(08:30~09:00) → 61, 정규장 → 0/3, 애프터장(15:40~16:00) → 81, 시간외단일가 → 62

홀드 버튼: 해당 종목 쿨다운을 원래의 25%로 단축 → 신호 지속 시 조기 재알림
```

---

## Claude Desktop (claude.ai) 기능

```
사용자 발화 예시                     → claude.ai 자동 행동
────────────────────────────────────────────────────────
"오늘 신호 뭐 왔어?"                 → quant_report(signals)
"포트폴리오 보여줘"                   → quant_report(portfolio)
"최근 매매 내역"                      → quant_report(trades)
"전략 노트 보여줘"                    → quant_report(strategy)

"한화에어로 목표가 얼마야?"           → quant_watchlist_read()
"SK하이닉스 손절가 830000으로 바꿔"  → quant_watchlist_update()
"삼성전자 모니터링 꺼줘"              → quant_watchlist_update(enabled=false)
"현대로템 추가해줘"                   → quant_watchlist_add()
"LIG 감시 목록에서 빼줘"              → quant_watchlist_delete()

"어떤 조건으로 신호 보내?"            → quant_conditions_list()
"볼린저 조건 추가해줘"                → quant_condition_add()
"RSI 쿨다운 180분으로 바꿔줘"        → quant_condition_update()
"MA5 이탈 조건 삭제해줘"             → quant_condition_remove()

"포트폴리오 동기화해줘"               → quant_portfolio_sync()
"삼성전자 200주 매수"                 → kiwoom_execute_api() [주문]
"계좌 잔고 확인해줘"                  → kiwoom_execute_api() [조회]
```

---

## 프로젝트 구조

```
quant_trading/
├── kiwoom_mcp/                  # MCP 서버 (Claude Desktop 연동)
│   └── kiwoom_mcp/
│       ├── server.py            # kiwoom-mcp: 키움 API 도구
│       └── quant_server.py      # quant-mcp: 리포트/watchlist/조건 관리
│
├── worker/                      # 백그라운드 워커
│   ├── main.py                  # 진입점, APScheduler
│   ├── kiwoom_client.py         # 키움 REST API 직접 호출 (ka10099 종목캐시 포함)
│   ├── monitor.py               # 조건 평가 엔진 (signal_type 필터 포함)
│   ├── indicators.py            # RSI / MA / MACD / 볼린저 밴드 계산
│   ├── claude_judge.py          # AI 매매 판단 (Claude Sonnet)
│   ├── cooldown.py              # 신호 쿨다운 (DB 기반, 09:00 전체 리셋)
│   ├── portfolio_sync.py        # 포트폴리오/매매내역 동기화
│   ├── report.py                # 현황 조회 (신호/포트폴리오/매매/전략)
│   └── daily_report.py          # 일일 리포트 (15:35)
│
├── notifications/
│   ├── telegram.py              # 텔레그램 신호 알림 (인라인 버튼 포함)
│   └── telegram_bot.py          # 텔레그램 봇 (양방향 주문 명령 처리)
│
├── data/
│   ├── db.py                    # SQLite CRUD (전 데이터 통합 관리)
│   └── trading.db               # DB 파일 (자동 생성)
│
├── config/
│   ├── worker.yaml              # 워커 설정값 (장 시간, 인터벌 등)
│   └── conditions.yaml          # 조건 정의 백업 (실제 사용은 DB)
│
├── logs/
│   └── worker.log
├── .env
└── requirements.txt
```

---

## SQLite DB 테이블

| 테이블 | 용도 |
|---|---|
| `watchlist` | 모니터링 종목 + 조건값 (JSON) |
| `conditions_def` | 시그널 조건 타입 정의 (평가 방식, 쿨다운, signal_type) |
| `portfolio` | 보유 종목 현황 캐시 |
| `trades` | 매매 내역 |
| `signals` | 발생 신호 로그 |
| `cooldowns` | 조건별 마지막 알림 시각 (09:00 전체 리셋) |
| `strategy_notes` | 전략 메모 |

---

## 시그널 조건 (20가지)

| 조건 | 평가 방식 | signal_type | 쿨다운 |
|---|---|---|---|
| 목표가 도달 | 현재가 >= 설정값 | exit | 6.5시간 |
| 손절가 도달 | 현재가 <= 설정값 | exit | 1시간 |
| RSI 과매수 | RSI >= 설정값 (기본 70) | exit | 2시간 |
| RSI 과매도 | RSI <= 설정값 (기본 30) | entry | 2시간 |
| 거래량 급증 | 거래량 / 20일평균 >= 설정배수 | both | 1시간 |
| MA 골든크로스 | 전일 MA5<MA20 → 오늘 MA5>MA20 | entry | 6.5시간 |
| MA 데드크로스 | 전일 MA5>MA20 → 오늘 MA5<MA20 | exit | 6.5시간 |
| 20일 신고가 돌파 | 현재가 > 최근 20일 최고가 | both | 6.5시간 |
| MA20 하향 이탈 | 전일 >= MA20, 오늘 < MA20 | exit | 6.5시간 |
| MA5 하향 이탈 | 전일 >= MA5, 오늘 < MA5 | both | 1시간 |
| MA5 상향 돌파 | 전일 <= MA5, 오늘 > MA5 | entry | 1시간 |
| MACD 골든크로스 | MACD 라인이 시그널 상향 돌파 | entry | 6.5시간 |
| MACD 데드크로스 | MACD 라인이 시그널 하향 돌파 | exit | 6.5시간 |
| 볼린저 상단 돌파 | 현재가 > 볼린저 상단 | both | 1시간 |
| 볼린저 하단 이탈 | 현재가 < 볼린저 하단 | entry | 1시간 |
| RSI 극단 과매도 | RSI <= 설정값 (기본 20) | both | **5분** |
| 볼린저 3% 이탈 | 현재가 < 볼린저 하단 × 0.97 | both | **5분** |
| RSI 과매도 (물타기) | RSI <= 설정값, 보유 중 | add | 4시간 |
| 볼린저 하단 이탈 (물타기) | 볼린저 하단 이탈, 보유 중 | add | 4시간 |
| MA5 회복 (물타기) | MA5 상향 돌파, 보유 중 | add | 4시간 |

> - `entry`: 미보유 종목 진입 타이밍
> - `exit`: 보유 종목 매도·관리
> - `add`: 보유 종목 물타기·추가매수
> - `both`: 보유/미보유 모두 적용
> - `quant_condition_add` / `quant_condition_update` 도구로 코드 수정 없이 관리 가능

---

## 쿨다운 동작

- 조건별 개별 쿨다운 (같은 종목+조건은 쿨다운 내 재발송 없음)
- **매일 09:00 장 시작 시 전체 리셋** → 매일 첫 체크에서 신호 발생 가능
- 홀드 버튼 클릭 시 해당 종목 쿨다운 25%로 단축 (신호 지속 시 조기 재알림)

---

## 포트폴리오 동기화 타이밍

| 시점 | 방식 |
|---|---|
| 워커 시작 시 | 자동 1회 |
| 평일 08:00~18:59 (10분마다) | 워커 자동 스케줄 |
| 08:30 | 프리장 시작 직후 동기화 |
| 09:01 | 정규장 시작 직후 동기화 |
| 18:05 | 시간외단일가 종료 후 확정 동기화 |
| claude.ai 요청 시 | `quant_portfolio_sync` 수동 호출 |

---

## 환경 설정

### 1. 가상환경 및 패키지 설치

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt

# MCP 서버 가상환경
cd kiwoom_mcp && python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
```

### 2. `.env` 파일 설정

```env
# 키움증권
KIWOOM_APP_KEY=...
KIWOOM_APP_SECRET=...
KIWOOM_ACCOUNT_NO=...
KIWOOM_BASE_URL=https://mockapi.kiwoom.com   # 모의투자
# KIWOOM_BASE_URL=https://api.kiwoom.com     # 실서버

# 매매 실행 허용 (false면 주문 불가)
KIWOOM_ALLOW_TRADE_EXECUTION=false

# AI
ANTHROPIC_API_KEY=...

# 텔레그램
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...

# MCP 경로
QUANT_TRADING_PATH=C:\Users\...\quant_trading
```

### 3. Claude Desktop MCP 설정 (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "kiwoom-mcp": {
      "command": "C:\\...\\quant_trading\\.venv\\Scripts\\python.exe",
      "args": ["-m", "kiwoom_mcp.server"],
      "cwd": "C:\\...\\quant_trading\\kiwoom_mcp",
      "env": { "QUANT_TRADING_PATH": "C:\\...\\quant_trading" }
    },
    "quant-mcp": {
      "command": "C:\\...\\quant_trading\\.venv\\Scripts\\python.exe",
      "args": ["-m", "kiwoom_mcp.quant_server"],
      "cwd": "C:\\...\\quant_trading\\kiwoom_mcp",
      "env": { "QUANT_TRADING_PATH": "C:\\...\\quant_trading" }
    }
  }
}
```

### 4. 워커 실행

```bash
# 평일 08:00~18:59 자동 실행 (프리장~시간외단일가 전 세션 커버)
.venv/Scripts/python worker/main.py

# 테스트 (장 시간 무관 즉시 실행)
.venv/Scripts/python worker/main.py --test
```

---

## 개발 단계

| Phase | 설명 | 상태 |
|---|---|---|
| Phase 1 | 반자동 — 조건 감지 → 알림 → 텔레그램 봇으로 매매 | ✅ 완료 |
| Phase 2 | 완전 자동 — 신뢰 축적 후 워커가 직접 주문 실행 | 예정 |

---

## 기술 스택

| 구분 | 기술 |
|---|---|
| AI 판단 | Claude Sonnet (Anthropic API) |
| 주식 API | 키움증권 REST API (ka10001 / ka10081 / kt00018 / ka10099 등) |
| MCP | kiwoom-mcp + quant-mcp (FastMCP) |
| 스케줄러 | APScheduler |
| 알림/주문 | 텔레그램 Bot API (인라인 버튼 + 양방향 명령) |
| DB | SQLite (전 설정/데이터 통합) |
| HTTP | httpx |
| 지표 | RSI / MA / MACD / 볼린저 밴드 (자체 구현) |

---

## 주의사항

- `.env` 파일은 절대 git에 커밋하지 말 것
- `KIWOOM_ALLOW_TRADE_EXECUTION=true` 설정 시 실제 주문 실행됨
- 모의서버(`mockapi.kiwoom.com`)는 rate limit → 요청 간 1초 딜레이
- `trading.db`는 자동 생성되며 최초 실행 시 YAML → DB 마이그레이션 자동 수행
- MCP 서버 변경 후 Claude Desktop 재시작 필요
