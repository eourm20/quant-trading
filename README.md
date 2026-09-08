# Quant Trading System (Main Branch)

키움 REST API + AI 판단(Claude/OpenAI) 기반의 자동 트레이딩 워커입니다.  
이 문서는 `main` 브랜치 코드를 기준으로, 실행 구조/데이터 저장/운영 흐름을 정리합니다.

> **브랜치 정책** — 이 저장소는 두 버전을 의도적으로 병행 유지합니다.
> `main`은 에이전트를 쓰지 않는 일반 버전이고, `feature/agent-mode`는 Agent + RAG 버전입니다.
> 어느 쪽도 다른 쪽의 구버전이 아니며 병합 예정도 없습니다. 필요한 AI 키가 서로 다릅니다
> (`main` → `ANTHROPIC_API_KEY`, `feature/agent-mode` → `OPENAI_API_KEY`).

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
  ├─ clients/*             : 키움 / DART / 뉴스 / 글로벌 지수
  ├─ notifications/*       : 텔레그램 알림/봇
  └─ data/db.py            : 저장/조회/성과 집계
```

핵심 특징:
- 정규장 감시 루프 + 보조 배치 작업 동시 운영
- `AUTO_TRADE`로 자동주문/알림모드 전환
- 신호/성과/리뷰 데이터가 SQLite에 누적

## 3. 디렉터리 구조

```text
quant-trading/
  config/
    worker.yaml                # 워커 주기/AI/급락매수/프리필터 설정
    conditions.yaml            # 조건 백업/초기값
  data/
    db.py                      # 스키마/마이그레이션/CRUD/성과 집계
    trading.db                 # 모의 DB (DB_PATH로 변경 가능)
    trading_real.db            # 실전 DB
  notifications/
    telegram.py                # 알림 전송
    telegram_bot.py            # 텔레그램 명령/콜백 처리
  worker/
    main.py                    # 엔트리포인트 + 스케줄러 + _auto_execute
    monitor.py                 # 종목 조건 평가, Signal 생성
    claude_judge.py            # AI 매매 판단
    stock_analyzer.py          # 장중/종가 스크리닝 + 리뷰
    portfolio_sync.py          # 계좌/체결 동기화
    indicators.py              # 기술지표/패턴 계산
    daily_report.py            # 장전/일일 리포트 생성
    report.py                  # 리포트 조회 유틸
    strategy_log.py            # 전략 노트 기록 헬퍼
    cooldown.py                # 쿨다운 키 관리
    clients/
      kiwoom_client.py         # 키움 REST 래퍼
      dart_client.py           # DART 공시/재무
      news_client.py           # 네이버 뉴스 + RSS
      global_market.py         # 글로벌 지수/거시 지표
  kiwoom_mcp/                  # 서브모듈 -> kiwoom-api-mcp-server (quant_trading 브랜치)
  docs/
    oracle_cloud_setup.md      # 서버 배포 + systemd 자동실행 가이드
  scripts/                     # 일회성 점검 스크립트 (gitignore)
  logs/
  CLAUDE.md
  .env.example
  requirements.txt
  README.md
```

`kiwoom_mcp`은 이 저장소에 포함된 파일이 아니라 **서브모듈**입니다.
[eourm20/kiwoom-api-mcp-server](https://github.com/eourm20/kiwoom-api-mcp-server)의 `quant_trading` 브랜치를 가리킵니다.
clone 직후에는 빈 디렉터리이므로 아래로 채웁니다.

```bash
git submodule update --init kiwoom_mcp
```

워커 코드는 `kiwoom_mcp`를 import하지 않으므로, 채우지 않아도 워커 실행에는 영향이 없습니다.

## 4. 실행 흐름

## 4.1 시작

`worker/main.py`에서:
1. 환경변수 로드 (`--env` 지원)
2. DB 초기화 (`init_db`)
3. 포트폴리오 동기화 (`sync_all`)
4. 텔레그램 봇 스레드 시작
5. APScheduler(`BackgroundScheduler`, `Asia/Seoul`) 잡 등록
6. `run_check()` 1회 즉시 실행
7. `scheduler.start()` 후 1초 슬립 루프로 상주

## 4.2 메인 감시 (`run_check`)

1. 세션 확인 (`get_current_session`)  
   - 정규장(main)만 실행
2. `watchlist` + `conditions_def` 로드
3. 종목별 조건 평가 (`check_stock`)
4. 쿨다운 필터링 (`filter_new_conditions`)
5. AI 판단 (`get_trade_opinion`)
   - 신호/차트/지수/섹터에 DART 요약, 종목·거시 뉴스, 글로벌 지수(`worker/clients/global_market.py`) 컨텍스트를 함께 주입
6. 신호 저장 (`save_signal`) + 알림 (`send_signal_alert`)
7. 모드별 처리
   - `AUTO_TRADE=true`: `_auto_execute`
   - `AUTO_TRADE=false`: `_paper_execute`

> 주의: 이 브랜치에는 공휴일 스킵 로직이 없습니다. `get_current_session()`은 **주말과 세션 시간대만** 판정하므로,
> 평일 임시 휴장일에도 스케줄 잡이 실행됩니다.

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

`main()`에서 등록되는 잡 전체입니다. 시간은 KST입니다.

| job id | 시간·주기 | 내용 |
|---|---|---|
| `monitor` | 월–금 08–18시, `interval_seconds`마다 | `run_check()` 감시 루프 |
| `reset_cooldowns` | 매일 09:00 | 전 종목 쿨다운 리셋 |
| `sync_premarket` / `sync_open` / `sync_close` | 08:30 / 09:01 / 18:05 | `auto_sync` |
| `sync_realtime` | 월–금 08–18시, 2분마다 | `auto_sync` (코드에 고정) |
| `result_update` | 월–금 09–18시, 30분마다 | `update_signal_results` |
| `screening_result_update` | 월–금 09–18시, 30분마다 | `update_screening_results` |
| `trailing_stops` | 월–금 09–15시, 30분마다 | `check_trailing_stops` |
| `removal_check` | 월–금 09–15시, 30분마다 | `check_removal_candidates` |
| `dip_buy` | 월–금 09–14시, 30분마다 | `check_market_dip` |
| `reassess_watchlist` | 월–금 09:15 | watchlist 재평가 |
| `inactive_alert` | 월–금 08:30 | 미발동 종목 알림 |
| `intraday_scan` | 월–금 11:00 | 장중 스크리닝 |
| `daily_screening` | 월–금 15:40 | 종가 스크리닝 |
| `daily_review` | 월–금 16:10 | 일일 복기 |

`interval_seconds`만 `config/worker.yaml`에서 조정되며, 나머지 시간은 `worker/main.py`에 하드코딩되어 있습니다.

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
템플릿은 `.env.example`입니다.

| 그룹 | 변수 | 비고 |
|---|---|---|
| 키움 | `KIWOOM_APP_KEY`, `KIWOOM_APP_SECRET`, `KIWOOM_ACCOUNT_NO`, `KIWOOM_BASE_URL` | 필수 |
| 매매 설정 | `AUTO_TRADE` | 워커 자동주문 스위치 |
| | `KIWOOM_ALLOW_TRADE_EXECUTION` | **텔레그램 봇 수동주문 전용 게이트** (아래 10. 참고) |
| 데이터 | `DB_PATH`, `LOG_PREFIX` | 모의 `data/trading.db` / 실전 `data/trading_real.db` |
| 알림 | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | |
| Anthropic | `ANTHROPIC_API_KEY`, `CLAUDE_MODEL`, `CLAUDE_MODEL_MINI` | **이 브랜치의 기본 판단 경로** |
| OpenAI | `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_MODEL_MINI` | `ANTHROPIC_API_KEY` 없을 때 폴백 |
| DART | `DART_API_KEY` | 공시/재무 컨텍스트 |
| 네이버 뉴스 | `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET` | 종목/거시 뉴스 |

## 8.2 `config/worker.yaml`

| 최상위 키 | 항목 |
|---|---|
| `worker` | `interval_seconds`, `use_claude_api`, `claude_model`, `dip_buy_threshold`, `dip_buy_max_stocks`, `dip_buy_cooldown_hours` |
| `watchlist_management` | `inactive_days_alert`, `inactive_days_removal`, `alert_interval_days` |
| `prefilter` | 스크리닝 1차 필터(`market_cap_min`, `change_upper/lower`, `rsi_max`, `ma_ratio`, `volume_ratio_min/max`, `consecutive_candles`) |
| | 레짐 보정(`market_bull_threshold`, `market_bear_threshold`, `bull_*`, `bear_*`), 후보 수 `max_ai_candidates` |

## 8.3 브랜치 주의: 에이전트 판단은 이 브랜치에 없습니다

이 브랜치에는 `worker/agents/`가 없고 `use_agent_mode` 설정도 인식하지 않습니다. 에이전트 판단이 필요하면 `feature/agent-mode` 브랜치를 쓰십시오(그 브랜치는 `OPENAI_API_KEY`가 필수).

## 9. 실행 방법

Windows:

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

Linux (운영 서버):

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python worker/main.py
```

서버 상주 실행은 systemd(`quant-worker.service`)로 관리합니다. unit 정의, 자동실행 on/off 현재 상태,
재개 절차는 `docs/oracle_cloud_setup.md`를 보십시오.

## 10. 운영 체크리스트

1. 실주문 안전
- 워커의 자동주문(`_auto_execute`)은 **`AUTO_TRADE=true`만으로 실행됩니다.**
- `KIWOOM_ALLOW_TRADE_EXECUTION`은 `notifications/telegram_bot.py`에서만 읽히는 **텔레그램 봇 수동주문 게이트**입니다.
  워커 자동주문을 막아주지 않으므로, 자동주문을 끄려면 `AUTO_TRADE=false`로 두십시오.

2. 로그 확인
- `logs/worker.log`
- `429`, 토큰 만료, 텔레그램 파싱 실패 패턴 점검

3. 데이터 정합성
- `portfolio` / `positions` / `trades` 동기화 상태
- 성과 업데이트 컬럼 누락 여부(`result_*`)

4. 서버 배포/자동실행
- systemd unit 정의와 자동실행 on/off 상태는 `docs/oracle_cloud_setup.md`가 소스 오브 트루스입니다.

5. 문서 동기화
- 조건/스케줄/DB 변경 시 README와 설정 파일 동시 업데이트
- 스케줄 시간 대부분이 `worker/main.py`에 하드코딩되어 있으므로, 시간 변경 시 5. 표를 함께 갱신하십시오.
