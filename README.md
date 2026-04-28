# Quant Trading System (Main Branch)

키움 REST API + AI 판단(Claude/OpenAI) 기반의 자동 트레이딩 워커입니다.  
이 문서는 `main` 브랜치 코드를 기준으로, 실행 구조/데이터 저장/운영 흐름을 정리합니다.

## 1. 핵심 스택

- Python
- APScheduler (주기 실행)
- SQLite (`data/db.py`)
- 키움 REST API
- Anthropic/OpenAI API (판단 모델)
- Telegram Bot API (알림/수동 명령)
- DART + 네이버 뉴스 API (컨텍스트)

## 2. 아키텍처 개요

```text
worker/main.py (Scheduler + Orchestrator)
  ├─ monitor.py            : 신호 감지(check_stock)
  ├─ claude_judge.py       : AI 매매 판단
  ├─ stock_analyzer.py     : 장중/종가 스크리닝 + 리뷰
  ├─ portfolio_sync.py     : 계좌/체결 동기화
  ├─ notifications/*       : 텔레그램 알림/봇
  └─ data/db.py            : 저장/조회/성과 집계
```

핵심 특징:
- 정규장 감시 루프 + 보조 배치 작업 동시 운영
- `AUTO_TRADE`로 자동주문/알림모드 전환
- 신호/성과/리뷰 데이터가 SQLite에 누적

## 3. 디렉터리 구조

```text
quant_trading/
  config/
    worker.yaml
    conditions.yaml
  data/
    db.py
  notifications/
    telegram.py
    telegram_bot.py
  worker/
    main.py
    monitor.py
    claude_judge.py
    stock_analyzer.py
    portfolio_sync.py
    indicators.py
    clients/
  kiwoom_mcp/
  README.md
```

## 4. 실행 흐름

## 4.1 시작

`worker/main.py`에서:
1. 환경변수 로드 (`--env` 지원)
2. DB 초기화 (`init_db`)
3. 포트폴리오 동기화 (`sync_all`)
4. 텔레그램 봇 스레드 시작
5. 스케줄 등록 + 루프 실행

## 4.2 메인 감시 (`run_check`)

1. 세션 확인 (`get_current_session`)  
   - 정규장(main)만 실행
2. `watchlist` + `conditions_def` 로드
3. 종목별 조건 평가 (`check_stock`)
4. 쿨다운 필터링 (`filter_new_conditions`)
5. AI 판단 (`get_trade_opinion`)
6. 신호 저장 (`save_signal`) + 알림 (`send_signal_alert`)
7. 모드별 처리
   - `AUTO_TRADE=true`: `_auto_execute`
   - `AUTO_TRADE=false`: `_paper_execute`

## 4.3 자동주문 리스크 가드

`_auto_execute`에서 주요 보호 로직:
- 판단 문구에서 매수/매도 의도 판별 실패 시 주문 스킵
- 추천수량 누락 시 fallback 정책 적용 가능
- 최근 재매수 차단
- 보유종목의 비-`add` 매수 차단
- 매수: 예수금/증거금 기반 하드캡
- 매도: 보유수량 하드캡
- 약한 매도 신호는 확인 시간/부분매도 제한

## 5. 스케줄러 잡 (main 코드 기준)

`main()` 등록 작업:
- `run_check`
- `reset_all_cooldowns`
- `auto_sync` (장전/장초/장마감 + 주기)
- `update_signal_results`
- `update_screening_results`
- `check_trailing_stops`
- `reassess_watchlist`
- `check_inactive_stocks`
- `check_removal_candidates`
- `check_market_dip`
- `run_intraday_scan`
- `run_daily_screening`
- `run_daily_review`

세부 주기/시간은 `config/worker.yaml`을 기준으로 변경합니다.

## 6. 데이터 저장 구조

`data/db.py:init_db()`에서 생성/마이그레이션합니다.

핵심 테이블:
- `watchlist`: 감시 종목 + 조건값
- `conditions_def`: 조건 정의/evaluator/cooldown
- `signals`: 신호/판단/성과
- `screening_log`: 스크리닝 결과/성과
- `cooldowns`: 종목-조건 발동 제어
- `portfolio`: 계좌 스냅샷
- `trades`: 실거래 이력
- `positions`: 목표/손절/추매가 관리
- `strategy_notes`: 전략 노트/리뷰

## 7. Data Dictionary (핵심 컬럼)

### 7.1 `watchlist`
| 컬럼 | 타입 | 설명 |
|---|---|---|
| `code` | TEXT PK | 종목코드 |
| `name` | TEXT | 종목명 |
| `enabled` | INTEGER | 감시 여부 |
| `horizon` | TEXT | 단기/중기/장기 |
| `strategy_note` | TEXT | 전략 메모 |
| 조건 관련 컬럼들 | INTEGER/REAL | RSI/MA/볼린저/스토캐스틱 등 threshold/flag |

### 7.2 `conditions_def`
| 컬럼 | 타입 | 설명 |
|---|---|---|
| `id` | TEXT PK | 조건 ID |
| `name` | TEXT | 조건명 |
| `evaluator` | TEXT | 평가기 키 (`rsi_lte`, `price_gte`, `flag` 등) |
| `param` | TEXT | watchlist 필드명 |
| `cooldown_minutes` | INTEGER | 쿨다운 분 |
| `signal_type` | TEXT | `entry/exit/add/both` |

### 7.3 `signals`
| 컬럼 | 타입 | 설명 |
|---|---|---|
| `id` | INTEGER PK | 신호 ID |
| `created_at` | TEXT | 생성시각 |
| `stock_code`, `stock_name` | TEXT | 종목 식별 |
| `current_price` | INTEGER | 신호 시점 가격 |
| `triggered_conditions` | TEXT | 발동 조건 문자열 |
| `signal_type` | TEXT | 신호 타입 |
| `claude_opinion` | TEXT | AI 판단 원문 |
| `verdict` | TEXT | 요약 판정 |
| `action` | TEXT | 실제 액션 |
| `result_1d`, `result_pct`, `result_5d`, `result_10d` | REAL | 사후 성과 |

### 7.4 `positions`
| 컬럼 | 타입 | 설명 |
|---|---|---|
| `stock_code` | TEXT PK | 종목코드 |
| `stock_name` | TEXT | 종목명 |
| `avg_price` | INTEGER | 평단 |
| `quantity` | INTEGER | 수량 |
| `target_price` | INTEGER | 목표가 |
| `stop_loss_price` | INTEGER | 손절가 |
| `add_buy_price` | INTEGER | 추가매수가 |

### 7.5 `trades`
| 컬럼 | 타입 | 설명 |
|---|---|---|
| `trade_id` | TEXT PK | 거래 식별자 |
| `executed_at` | TEXT | 체결 시각 |
| `stock_code`, `stock_name` | TEXT | 종목 식별 |
| `side` | TEXT | 매수/매도 |
| `quantity`, `price`, `amount` | INTEGER | 체결 정보 |
| `fee`, `tax` | INTEGER | 비용 |
| `result_1d`, `result_3d`, `result_5d` | REAL | 사후 성과 |

## 8. 설정

## 8.1 `.env`
필수:
- `KIWOOM_APP_KEY`
- `KIWOOM_APP_SECRET`
- `KIWOOM_ACCOUNT_NO`

주요:
- `AUTO_TRADE`
- `KIWOOM_ALLOW_TRADE_EXECUTION`
- `DB_PATH`
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- `ANTHROPIC_API_KEY` 또는 `OPENAI_API_KEY`
- `DART_API_KEY`, `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET`

## 8.2 `config/worker.yaml`
- 워커 주기: `interval_seconds`
- AI 사용: `use_claude_api`
- 급락매수: `dip_buy_*`
- watchlist 관리: `watchlist_management.*`
- 스크리닝 prefilter: `prefilter.*`

## 9. 실행 방법

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt

# 기본 실행
.venv\Scripts\python worker\main.py

# 테스트 모드
.venv\Scripts\python worker\main.py --test

# 다른 env 파일
.venv\Scripts\python worker\main.py --env .env.real
```

## 10. 운영 체크리스트

1. 실주문 안전
- `AUTO_TRADE=true` + `KIWOOM_ALLOW_TRADE_EXECUTION=true` 동시 활성화 시 실제 주문

2. 로그 확인
- `logs/worker.log`
- `429`, 토큰 만료, 텔레그램 파싱 실패 패턴 점검

3. 데이터 정합성
- `portfolio` / `positions` / `trades` 동기화 상태
- 성과 업데이트 컬럼 누락 여부(`result_*`)

4. 문서 동기화
- 조건/스케줄/DB 변경 시 README와 설정 파일 동시 업데이트
