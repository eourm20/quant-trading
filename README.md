# Quant Trading System

AI(Claude)를 활용한 개인용 퀀트 트레이딩 시스템.
수동 모드(Claude Desktop + MCP)와 자동 모드(백그라운드 워커)로 운영.

> 프로세스 상세 가이드: [자동 모드](img/5_auto_mode.html) · [수동 모드](img/6_manual_mode.html)

---

## 수동 모드 vs 자동 모드

두 모드는 독립 실행 가능하며, 같은 DB를 공유. 동시 실행도 가능.

| 기능 | 수동 (Claude Desktop) | 자동 (Worker) |
|------|---------------------|---------------|
| **종목 추천** | MCP로 분석 → 추천만 (사용자가 판단) | 장중 스캔 + 장 마감 AI 분석 → watchlist **자동 추가** |
| **초기 임계치** | 자동 제안 → 사용자 확인 후 등록 | AI가 자동 설정 |
| **신호 대응** | 텔레그램 알림 → **사용자 승인** | AI 판단 → **자동 매매** (`AUTO_TRADE=true`) |
| **포트폴리오 동기화** | MCP auto_sync (10분) | 워커 스케줄러 (2분) |

---

## 거래 세션

| 세션 | 시간 | 매매구분(trde_tp) | 조건 필터 |
|------|------|-----------------|----------|
| 프리장 (장전 시간외) | 08:30~09:00 | 61 (전일 종가) | exit/add/both만 |
| 정규장 | 09:00~15:30 | 0 (시장가) / 3 (지정가) | 전체 |
| 애프터장 (장후 시간외) | 15:40~16:00 | 81 (당일 종가) | exit/add/both만 |
| 시간외 단일가 | 16:00~18:00 | 62 (지정가 필수) | exit/add/both만 |

---

## 워커 스케줄러 (자동 모드)

| 작업 | 주기 | 시간대 | 설명 |
|------|------|--------|------|
| **run_check** | 매 1분 | 08:00~18:59 | 핵심: 종목별 조건 체크 → AI 판단 → 알림/자동매매 |
| reset_all_cooldowns | 1일 1회 | 09:00 | 장 시작 시 전 종목 쿨다운 초기화 |
| sync (4개) | 2분~개별 | 08:30~18:05 | 포트폴리오 실시간 동기화 |
| update_signal_results | 매 30분 | 09:00~18:00 | 신호 후 1/3/5/10일 수익률 자동 계산 |
| check_trailing_stops | 매 30분 | 09:00~15:00 | 수익 구간 손절가 자동 상향 |
| check_inactive_stocks | 1일 1회 | 08:30 | 30일 미발동 종목 경고 |
| check_removal_candidates | 매 30분 | 09:00~15:00 | 미보유 90일 미발동 자동 삭제 |
| **run_intraday_scan** | 1일 2회 | 10:00, 13:00 | 장중 거래량 급증 경량 스캔 → 텔레그램 알림 |
| **run_daily_screening** | 1일 1회 | 15:40 | 장 마감 AI 풀 분석 → watchlist 자동 추가 |
| **run_daily_review** | 1일 1회 | 16:10 | AI 일일 복기 → 전략노트 저장 + 텔레그램 + 학습 피드백 |

---

## 신호 발생 흐름

```
워커 1분 체크 (평일 08:00~18:59)
     │
     ├─ 세션 확인 → 장외 시간이면 return
     ├─ DB에서 watchlist + conditions_def 로드 (실시간 반영)
     │
     ├─ 종목별:
     │   ├─ 현재가 (ka10001) + 90일 일봉 (ka10081)
     │   ├─ 기술적 지표 40+ 계산
     │   │   RSI / MA / MACD / 볼린저 / 스토캐스틱 / CCI / 일목균형표
     │   │   OBV / 캔들 패턴 / 차트 패턴 / 다이버전스 / 피보나치
     │   │
     │   ├─ 조건 평가 (29개 조건)
     │   │   ├─ signal_type 필터 (미보유: entry/both, 보유: exit/add/both)
     │   │   └─ 쿨다운 필터
     │   │
     │   ├─ AI 판단 (Claude API)
     │   │   Input: 차트 + DART 공시/재무 + 뉴스 + 포트폴리오 + 시장지수
     │   │   Output: [매수/매도/홀드] + 추천수량 + 근거
     │   │
     │   ├─ 텔레그램 알림 (인라인 버튼: 시장가/지정가/홀드/무시)
     │   │
     │   └─ AUTO_TRADE=true 시 → 자동 주문 실행
     │
     └─ 신호 DB 저장 (지표 스냅샷 + DART + 차트 패턴 포함)
```

---

## 종목 스크리닝

### 장중 경량 스캔 (10:00, 13:00)
- 거래량 급증 종목만 조회 (API 1회, AI 없음)
- 텔레그램 알림만 발송 (watchlist 추가 안 함)
- 관심 있으면 Claude Desktop에서 상세 분석 요청

### 장 마감 풀 스크리닝 (15:40)
- 후보 수집: 거래량 급증(ka10023) + 눌림목(ka10027) + 외인 순매수(ka10035)
- 기존 watchlist 종목 제외 → 최대 30개 후보
- 후보별 AI 분석: 차트 + DART + 뉴스 + 포트폴리오 + 시장환경
- 편입 조건 5가지 중 2개 이상 충족 평가: 눌림목 / 저평가 / 테마미반영 / 실적개선 / 잠재성장
- 자동 모드: watchlist 자동 추가 (목표가/손절가/RSI/horizon AI 설정 + 각 근거)
- 수동 모드: 텔레그램 알림 + [✅ 관심종목 등록] [❌ 패스] 버튼

### Claude Desktop 종목 추천 (수동)
- "이 종목 어때?" → MCP로 차트+공시+뉴스 직접 분석 → 관심종목 등록 제안
- "종목 추천해줘" → 포트폴리오 확인 후 키움 API로 후보 탐색 → 분석 → 제안
- 등록 시 AI 제안 임계값 + 각 설정 근거 포함

---

## AI 학습 피드백 루프

### 일일 자동 복기 (16:10)
- 장 마감 후 AI가 오늘 신호·매매·포트폴리오·적중률·스크리닝 성과 분석
- 전략노트 (`daily_review`) 저장 + 텔레그램 발송

### 판단 AI 자기 보정
- 매 신호 판정 시 프롬프트에 **14일 적중률 통계** + **최근 복기 인사이트** 주입
- "매수 12건 적중58%, 홀드가 나은 결과" → AI가 보수적으로 보정

### 스크리닝 AI 자기 보정
- 매 스크리닝 시 프롬프트에 **30일 추천 적중률** (7d/30d) + **복기 스크리닝 평가** 주입
- "7일 적중률 45%" → 종목 선별 기준 강화

---

## 텔레그램 봇

```
신호 알림 인라인 버튼:
  [📊 시장가] [💰 지정가] [🚪 홀드] [❌ 무시]

임계값 변경 제안 버튼 (AI 홀드 시):
  [✅ 적용] [✏️ 수정] [❌ 거절]

스크리닝 결과 버튼 (수동 모드):
  [✅ 관심종목 등록] [❌ 패스]

직접 명령:
  /buy 종목명 [수량]    매수 주문
  /sell 종목명 [수량]   매도 주문
  /price 종목명         현재가 조회
  /balance              보유 종목 조회
```

---

## Claude Desktop (MCP 도구)

### Quant MCP (15개)
| 도구 | 설명 |
|------|------|
| `quant_report` | 신호/포트폴리오/매매/전략 조회 |
| `quant_strategy_log` | 전략 노트 기록 + 텔레그램 발송 |
| `quant_portfolio_sync` | 포트폴리오 동기화 |
| `quant_watchlist_read/add/update/delete` | 관심종목 CRUD |
| `quant_conditions_list/add/update/remove` | 조건 정의 관리 |
| `quant_signal_log_delete` | 신호 로그 삭제 |
| `quant_strategy_note_update/delete` | 전략 노트 편집 |
| `quant_cooldown_reset` | 쿨다운 초기화 |

### DART MCP (6개)
`dart_disclosures` · `dart_company_info` · `dart_financial` · `dart_shareholders` · `dart_periodic_report` · `dart_major_event`

### Kiwoom MCP (직접 API)
`ka10001` 현재가 · `ka10081` 일봉 · `kt00018` 잔고 · `kt00007` 체결 · 주문 실행 등

---

## 프로젝트 구조

```
quant_trading/
├── kiwoom_mcp/                  # MCP 서버 (Claude Desktop 연동)
│   └── kiwoom_mcp/
│       ├── server.py            # kiwoom-mcp: 키움 API 도구
│       ├── quant_server.py      # quant-mcp: 리포트/watchlist/조건 관리
│       └── dart_client.py       # DART 공시 클라이언트 (풀)
│
├── worker/                      # 백그라운드 워커
│   ├── main.py                  # 진입점, APScheduler (13개 작업)
│   ├── monitor.py               # 조건 평가 엔진 (29개 조건)
│   ├── indicators.py            # 기술적 지표 40+ (RSI/MA/MACD/볼린저/스토캐스틱/CCI/일목균형표 등)
│   ├── claude_judge.py          # AI 매매 판단 (프롬프트 캐싱 + RAG 자기보정)
│   ├── stock_analyzer.py        # 자동 스크리닝 + 일일 복기 (학습 피드백)
│   ├── cooldown.py              # 신호 쿨다운
│   ├── portfolio_sync.py        # 포트폴리오/매매내역 동기화
│   ├── report.py                # 현황 조회
│   └── clients/                 # 외부 API 경량 클라이언트
│       ├── kiwoom_client.py     # 키움 REST API
│       ├── dart_client.py       # DART 공시 API
│       └── news_client.py       # 네이버 뉴스 API
│
├── notifications/
│   ├── telegram.py              # 텔레그램 알림 (인라인 버튼)
│   └── telegram_bot.py          # 텔레그램 봇 (주문/임계값 버튼)
│
├── data/
│   ├── db.py                    # SQLite CRUD (전 데이터 통합)
│   └── trading.db               # DB 파일 (자동 생성)
│
├── config/
│   ├── worker.yaml              # 워커 설정
│   └── conditions.yaml          # 조건 정의 백업
│
├── img/                         # 프로세스 가이드 (HTML)
│   ├── 5_auto_mode.html         # 자동 모드 프로세스
│   └── 6_manual_mode.html       # 수동 모드 프로세스
│
├── logs/worker.log
├── .env
├── CLAUDE.md                    # Claude Desktop 행동 지침
└── requirements.txt
```

---

## SQLite DB 테이블

| 테이블 | 용도 |
|---|---|
| `watchlist` | 모니터링 종목 + 조건값 (JSON) + horizon |
| `conditions_def` | 시그널 조건 타입 29개 (평가 방식, 쿨다운, signal_type) |
| `portfolio` | 보유 종목 현황 캐시 |
| `trades` | 매매 내역 (30일) |
| `signals` | 발생 신호 로그 (지표 스냅샷, DART, 차트 패턴, 1~10일 수익률) |
| `cooldowns` | 조건별 마지막 알림 시각 (09:00 전체 리셋) |
| `strategy_notes` | 전략 메모 (trade/watchlist/general/daily_review) |

---

## 시그널 조건 (29개)

| 조건 | signal_type | 쿨다운 | 비고 |
|---|---|---|---|
| RSI 과매도 | entry | 120분 | horizon별 RSI 기간 차등 (7/14/21일) |
| RSI 과매수 | exit | 120분 | |
| RSI 과매도 (5분봉) | entry | 30분 | horizon=단기만 |
| RSI 극단 과매도 | both | 5분 | 긴급 반복 알림 |
| 골든크로스 (MA) | entry | 1440분 | 전환형 |
| 데드크로스 (MA) | exit | 1440분 | 전환형 |
| MACD 골든/데드크로스 | entry/exit | 1440분 | 전환형 |
| 볼린저 하단 이탈/상단 돌파 | entry/both | 360분/60분 | 전환형 |
| 볼린저 Critical (3%+) | both | 5분 | 긴급 |
| 거래량 급증 | both | 60분 | |
| 목표가/손절가 도달 | exit | 360분/60분 | |
| MA5/MA20 이탈/돌파 | exit/entry/both | 60분/1440분 | 전환형 |
| 20일 신고가 | both | 1440분 | |
| 스토캐스틱 골든/데드크로스 | entry/exit | - | 전환형 |
| CCI 과매도/과매수 | entry/exit | - | ±100 기준 |
| 일목 전환선 크로스 | entry/exit | - | 전환형 |
| 일목 구름대 돌파/이탈 | entry/exit | - | 전환형 |
| RSI/볼린저 물타기 | add | 240분 | 보유 종목 전용 |

---

## 환경 설정

### 1. 가상환경 및 패키지 설치

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
```

### 2. `.env` 파일 설정

```env
# 키움증권
KIWOOM_APP_KEY=...
KIWOOM_APP_SECRET=...
KIWOOM_ACCOUNT_NO=...
KIWOOM_BASE_URL=https://api.kiwoom.com
KIWOOM_ALLOW_TRADE_EXECUTION=false

# AI (둘 중 하나 필수)
ANTHROPIC_API_KEY=...
OPENAI_API_KEY=...

# 텔레그램
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...

# DART 공시
DART_API_KEY=...

# 네이버 뉴스
NAVER_CLIENT_ID=...
NAVER_CLIENT_SECRET=...

# 자동매매 모드
AUTO_TRADE=false
```

### 3. Claude Desktop MCP 설정

`claude_desktop_config.json`에 kiwoom-mcp와 quant-mcp 서버 등록.

### 4. 실행

```bash
# 자동 모드 (워커)
.venv/Scripts/python worker/main.py

# 테스트 (장 시간 무관 즉시 실행)
.venv/Scripts/python worker/main.py --test

# 수동 모드 (Claude Desktop에서 MCP로 대화)
```

---

## 기술 스택

| 구분 | 기술 |
|---|---|
| AI 판단 | Claude Sonnet (Anthropic API, 프롬프트 캐싱) / GPT 대체 가능 |
| 주식 API | 키움증권 REST API |
| 공시 | DART OpenAPI |
| 뉴스 | 네이버 뉴스 검색 API |
| MCP | kiwoom-mcp + quant-mcp (FastMCP) |
| 스케줄러 | APScheduler |
| 알림/주문 | 텔레그램 Bot API (인라인 버튼 + 양방향 명령) |
| DB | SQLite |
| 지표 | 40+ 기술적 지표 자체 구현 |

---

## 주의사항

- `.env` 파일은 절대 git에 커밋하지 말 것
- `KIWOOM_ALLOW_TRADE_EXECUTION=true` 설정 시 실제 주문 실행됨
- `AUTO_TRADE=true` 설정 시 AI 판단 기반 자동 매매 실행됨
- `trading.db`는 자동 생성되며 최초 실행 시 마이그레이션 자동 수행
- MCP 서버 변경 후 Claude Desktop 재시작 필요
