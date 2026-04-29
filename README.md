# Quant Trading (Agent + RAG)

키움 REST API 기반 **Agent + RAG 자동 트레이딩 시스템**입니다.  
이 문서는 단순 실행법이 아니라, **현재 코드의 프레임워크/아키텍처/데이터 흐름/저장 구조/운영 로직**을 기준으로 프로젝트 전체를 설명합니다.

## 1. 기술 스택과 프레임워크

### 런타임
- Python
- APScheduler (잡 스케줄링)
- SQLite (`data/db.py`)

### 외부 API/서비스
- 키움 REST API: 시세/지수/계좌/주문
- OpenAI/Anthropic: 매매 판단, 리서치, 뉴스 리스크 분류
- DART OpenAPI: 공시/재무 컨텍스트
- 네이버 뉴스 API + RSS: 종목/거시 뉴스 컨텍스트
- Telegram Bot API: 알림/승인/수동 명령

### AI 계층
- `worker/claude_judge.py`: 판단 엔진(하네스 + Agent + 레거시 폴백)
- `worker/agents/`: tool-calling 기반 judgment/research agent
- `worker/agents/tools/rag_tools.py`: FAISS + SQLite 하이브리드 검색/인덱싱
- 운영용 Agent는 **2개**입니다: `JudgmentAgent`, `ResearchAgent`
- `base_agent.py`는 공통 실행 엔진(프레임워크)이며, `tools/`는 도구 모음으로 Agent 개수에 포함되지 않습니다.

> 참고: 하네스와 서브에이전트는 신호 처리 결과에 직접 영향을 주는 핵심 경로입니다.  
> 문서에는 운영/개발에 필요한 수준으로만 간결하게 포함했습니다.

---

## 2. 시스템 아키텍처

```text
[Scheduler: worker/main.py]
        |
        |--- run_check() ------------------------------+
        |                                             |
        |                                      [worker/monitor.py]
        |                                      신호 감지(check_stock)
        |                                             |
        |                                             v
        |                                   [worker/claude_judge.py]
        |                             AI 판단(get_trade_opinion 등)
        |                                             |
        |                                             v
        |                              [data/db.py] signals 저장
        |                                             |
        |                                             +--> Telegram 알림
        |                                             |
        |                                             +--> AUTO_TRADE면 주문 실행
        |
        |--- sync_all() / 성과 업데이트 / 스크리닝 / 리뷰 / 뉴스모니터링 / 리플렉션
```

핵심 포인트:
- **스케줄러 중심 오케스트레이션**: `worker/main.py`가 모든 유스케이스를 조정
- **도메인 분리**:
  - 신호탐지: `monitor.py`
  - 판단: `claude_judge.py`
  - 실행/리스크가드: `main.py` 내부 `_auto_execute`
  - 저장/분석: `data/db.py`
- **실행 모드 분리**:
  - `AUTO_TRADE=false`: 알림/모의 기록
  - `AUTO_TRADE=true`: 자동 주문

### 2.1 운영 관점 핵심 기능 프레임
이 프로젝트를 운영할 때 핵심 축은 아래 7가지입니다.
1. 두 에이전트 흐름: `JudgmentAgent` / `ResearchAgent`
2. 하네스 분기: `SKIP` / `DIRECT_SELL` / `AMBIGUOUS`
3. 서브에이전트(tool-calling) 실행 구조
4. Agent 미세 학습/조정: 일일 복기, 리플렉션, adaptive policy
5. 외부 API 활용: 키움, 뉴스, DART, LLM
6. 저장 데이터 구조: signals/market_reports/strategy_notes 등
7. 텔레그램 연동: 알림, 승인, 수동 명령 인터페이스

---

## 3. 디렉터리 구조(현재 코드 기준)

```text
quant_trading/
  config/
    worker.yaml                # 워커 주기/AI/리스크 설정
    conditions.yaml            # 조건 백업/초기값
  data/
    db.py                      # 스키마/마이그레이션/CRUD/분석 쿼리
    trading.db                 # 실운영 DB (환경변수로 변경 가능)
    trading_tuning.db
  notifications/
    telegram.py                # 알림 전송
    telegram_bot.py            # 텔레그램 명령/콜백 처리
  worker/
    main.py                    # 엔트리포인트 + 스케줄러 + 런루프
    monitor.py                 # 종목 조건 평가, Signal 생성
    claude_judge.py            # AI 판단 엔진
    stock_analyzer.py          # 장중/종가 스크리닝 + 리뷰
    portfolio_sync.py          # 계좌/체결 동기화
    indicators.py              # 기술지표/패턴 계산
    adaptive_policy.py         # 과거 성과 기반 진입 강도 조정
    strategy_reflection.py     # 리플렉션/정책 업데이트 루프
    agents/
      base_agent.py            # 공통 런타임(Agent 아님)
      judgment_agent.py        # 운영 Agent 1
      research_agent.py        # 운영 Agent 2
      tools/
  kiwoom_mcp/
  logs/
  .env.example
  requirements.txt
  README.md
```

---

## 4. 실행 프로세스 상세

## 4.1 프로세스 시작 (`worker/main.py`)

1. `.env` 로드 및 타임존 설정
2. DB 초기화 (`init_db`)
3. 포트폴리오 초기 동기화 (`sync_all`)
4. 텔레그램 봇 스레드 시작 (`start_bot_thread`)
5. APScheduler 잡 등록
6. `run_check()` 1회 즉시 실행 후 루프 진입

## 4.2 메인 감시 루프 (`run_check`)

1. 세션 확인 (`get_current_session`)  
   - main(정규장) 외에는 스킵
2. watchlist + conditions 로드
3. (선택) R/R 기준 종목 우선순위 정렬
4. 예수금/실질 매수여력 계산
5. 종목별 `check_stock` 수행
6. 쿨다운 필터 (`filter_new_conditions`) 적용
7. AI 판단 호출
   - 하네스: `SKIP` / `DIRECT_SELL` / `AMBIGUOUS`
   - ambiguous면 모델 판단
   - 운용 지침 주입 시점:
     - `premarket/opening/news_monitor`: 당일 생성 지침을 당일 즉시 반영
     - `daily_review`: 당일분 제외, 다음 거래일(T+1)부터 반영
8. 신호 저장 (`save_signal`), trace 저장, RAG 인덱싱 큐 등록
9. 텔레그램 알림
10. 자동모드면 `_auto_execute`, 수동모드면 `_paper_execute`

---

## 5. 신호 생성 로직 (`worker/monitor.py`)

### 5.1 `check_stock` 처리
- 시세/일봉 조회
- RSI/거래량배율/차트요약 계산
- `conditions_def`의 evaluator 규칙으로 조건 평가
- 보유/미보유에 따라 평가 가능한 signal_type 제한
  - 미보유: `entry`, `both`
  - 보유: `exit`, `add`, `both`

### 5.2 add 신호 분류
- `_classify_add_signal`로 `momentum_add` / `averaging_down` 분기
- 평단 조건 불일치 시 add 신호 스킵
  - `ma5_recovery_add`: 현재가 > 평단 필요
  - `rsi_oversold_add`, `bollinger_lower_break_add`: 현재가 < 평단 필요

---

## 6. AI 판단 아키텍처 (`worker/claude_judge.py`)

### 6.1 백엔드 선택
- `ANTHROPIC_API_KEY` 있으면 Anthropic
- 없고 `OPENAI_API_KEY` 있으면 OpenAI

### 6.2 판단 경로
1. 하네스 검사 (`harness_check`)
2. Agent 모드 판단 (tool-calling)
3. 실패/제한 시 레거시 프롬프트 경로 폴백

### 6.3 컨텍스트 구성
- 신호/차트/지수/섹터
- 포트폴리오/현금/최근 매매이력
- DART 요약, 종목뉴스/거시뉴스
- 최근 유사 신호 성과(RAG/DB)
- 최근 AI 판단 성과 인사이트

### 6.4 출력/추적
- verdict 텍스트(`[매수]`, `[매도]`, `[홀드]` 등)
- 추천수량/주문시장/전환조건
- tool_sequence, reasoning_chain trace 저장

---

## 7. 주문 실행과 리스크 가드 (`_auto_execute`)

주요 가드:
- 의도 판별 실패(매수/매도 모호) 시 주문 스킵
- 추천수량 누락 시 fallback 정책 적용 가능
- 최근 매수 재진입 차단 (`buy_reentry_block_minutes`)
- 보유 종목에서 `signal_type != add` 매수 차단
- adaptive policy로 수량 multiplier 조정
- 매수 하드캡: 현금/증거금 주문가능수량 상한
- 매도 하드캡: 보유수량 초과 방지
- weak exit는 부분매도로 제한 + 확인 지연
- `avoid_targets`는 기본적으로 **소프트 제약**으로 동작
  - 회피 대상 종목이어도 강한 신호면 진입 가능
  - 대신 매수 수량을 정책 계수(예: `avoid_targets_soft_factor`)로 축소

주문 후 후처리:
- trade/notes/cooldown 반영
- 포트폴리오 재동기화
- 매수 시 포지션 생성 및 AI 포지션 값(목표/손절/추매가) 설정
- 전량매도 시 watchlist 결정(keep/drop/reassess)

---

## 8. 스케줄러 잡 구성 (핵심)

`main()`에서 등록되는 대표 잡:
- `run_check`: 장중 주기 감시
- `auto_sync`: 포트폴리오 동기화
- `run_premarket_report`, `run_opening_report`
- `update_signal_results`, `update_trade_results`, `update_paper_results`, `update_screening_results`
- `check_trailing_stops`, `check_inactive_stocks`, `check_removal_candidates`
- `check_market_dip`
- `run_intraday_scan`, `run_daily_screening`, `run_daily_review`
- `run_weekly_performance_report`, `run_weekly_self_correction`
- `run_reflection_policy_cycle`
- `run_news_monitor`
- `run_rag_batch_index`

세부 시간/주기는 `config/worker.yaml`이 소스 오브 트루스입니다.

---

## 9. 데이터 저장 구조 (DB 스키마)

`data/db.py:init_db()` 기준. 마이그레이션은 `ALTER TABLE` 방식으로 누적 적용됩니다.

### 9.1 핵심 테이블

1. `watchlist`
- 종목 마스터 + 조건 필드 + 전략 메모
- 주요 컬럼: `code`, `name`, `enabled`, `horizon`, `strategy_note`, 개별 조건 컬럼들, `sector_code`

2. `conditions_def`
- 조건 정의 테이블
- 주요 컬럼: `id`, `name`, `evaluator`, `param`, `cooldown_minutes`, `message`, `signal_type`

3. `signals`
- 감시 루프에서 확정된 신호/판단 이력
- 주요 컬럼:
  - 기본: `created_at`, `stock_code`, `current_price`, `triggered_conditions`, `signal_type`, `in_portfolio`
  - 판단: `claude_opinion`, `verdict`, `action`, `decision_status`, `decision_confidence`
  - 컨텍스트: `indicator_snapshot`, `dart_summary`, `news_summary`, `market_snapshot`, `portfolio_snapshot`, `stock_feature_snapshot`
  - 실험추적: `model_id`, `prompt_version`, `policy_version`, `source`, `tool_sequence`, `reasoning_chain`
  - 성과: `result_1d`, `result_pct(3d)`, `result_5d`, `result_10d`

4. `portfolio`
- 계좌 보유 스냅샷
- `stock_code`, `quantity`, `avg_price`, `current_price`, `profit_rate`, `updated_at`

5. `positions`
- 포지션 관리값
- `avg_price`, `target_price`, `stop_loss_price`, `add_buy_price`, `quantity`, `updated_at`

6. `trades`
- 실거래 이력
- `trade_id`, `executed_at`, `side`, `quantity`, `price`, `amount`, `fee`, `tax`, `result_1d/3d/5d`

7. `paper_trades`
- 모의 체결 이력
- `order_type`, `quantity`, `price`, `signal_id`, `verdict`, `result_1d/3d/5d`

8. `screening_log`
- 장중/종가 스크리닝 결과 저장
- `source`, `recommendation`, `reason`, `met_conditions`, `rr_ratio`, `indicator_snapshot`, `ai_response`, `user_action`, `result_7d/30d`

9. `cooldowns`
- 신호/종목별 발동 제어
- `key`, `last_sent_at`, `next_allowed_at`

10. `strategy_notes`
- 전략 메모/리포트 본문 저장
  - `meta_json`으로 일일 복기 구조화 데이터 저장 가능
    - 예: `market_context`, `wins/losses`, `next_day_policy`

11. `market_reports`
- premarket/open 리포트 구조화 저장
  - `meta_json.agent_policy` 저장
    - 예: `aggressive_entry`, `avoid_targets`, `increase_cash`, `applied_session`
    - 사후 리플레이(“왜 그날 그렇게 판단했는지”) 용도

12. `agent_action_logs`
- agent 의사결정 trace 장기 저장

13. `strategy_reflection_logs`, `strategy_policy_state`, `strategy_policy_update_queue`
- 리플렉션과 정책 자동 갱신 파이프라인 상태 저장

14. `realized_pnl_snapshots`
- 실현손익 스냅샷(당일/기간)

### 9.2 인덱스/분석 지원
- `signals`, `screening_log`, `agent_action_logs`에 조회 인덱스 구성
- `get_verdict_accuracy`, `get_condition_accuracy`, `get_weekly_performance_report` 등 분석 함수 제공

---

## 10. Agent + RAG 계층

### 10.1 Agent Tool Registry
- `worker/agents/tools/registry.py`
- judgment/research 별 toolset 로딩

### 10.2 Tool 분류
- `market_tools.py`: 시세/차트/지수/랭킹
- `portfolio_tools.py`: 보유/예수금/주문상태/손익
- `db_tools.py`: 이력/정책/성과/watchlist 조작
- `info_tools.py`: 뉴스/DART/거시정보
- `order_tools.py`: 주문 실행(모드별 승인 플로우)
- `rag_tools.py`: 임베딩 인덱싱/유사 검색

### 10.3 RAG 저장소
- 신호/스크리닝/에이전트 메모리를 문서화
- FAISS 인덱스 + SQLite 메타 테이블 혼합
- 실시간 인덱싱 또는 배치 인덱싱 모드 지원

---

## 11. 텔레그램 인터페이스

`notifications/telegram.py`, `notifications/telegram_bot.py`

역할:
- 신호 알림
- 임계값 제안/승인
- 주문 승인/거부 플로우
- 수동 커맨드 처리(`/buy`, `/sell`, `/balance` 등)
- Markdown 파싱 실패 시 plain text 재전송 폴백

운영상 중요 포인트:
- 텔레그램은 단순 알림 채널이 아니라, 자동매매 안전장치와 수동介입 인터페이스 역할을 함께 수행
- AUTO_TRADE 환경에서도 승인 플로우/거부 플로우를 통해 비정상 주문을 차단하는 마지막 게이트로 사용 가능
- 장애 시(포맷 실패/일시 오류) plain text 폴백으로 신호 전달 연속성 유지

---

## 12. 설정 파일 가이드

## 12.1 `.env`
핵심 변수:
- 키움: `KIWOOM_APP_KEY`, `KIWOOM_APP_SECRET`, `KIWOOM_ACCOUNT_NO`, `KIWOOM_BASE_URL`
- 실행모드: `AUTO_TRADE`, `KIWOOM_ALLOW_TRADE_EXECUTION`
- AI: `OPENAI_API_KEY` 또는 `ANTHROPIC_API_KEY`
- 알림: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- 데이터: `DB_PATH`, `LOG_PREFIX`

## 12.2 `config/worker.yaml`
핵심 그룹:
- 워커 주기: `interval_seconds`, `sync_realtime_minutes`
- Agent 파라미터: `agent_max_steps`, `*_model`, `*_max_tokens`
- 리스크: `buy_reentry_block_minutes`, `weak_exit_*`, `missing_qty_policy`
- 스캔/스크리닝: `research_max_candidates`, `prefilter.*`
- RAG: `rag_realtime_index`, `rag_batch_*`

---

## 13. 실행 방법

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt

# 기본 실행
.venv\Scripts\python worker\main.py

# 테스트 모드 (장시간 체크 무시)
.venv\Scripts\python worker\main.py --test

# 다른 env 파일 사용
.venv\Scripts\python worker\main.py --env .env.real
```

---

## 14. 운영/정리 시 체크리스트

1. 실제 주문 안전장치 확인
- `AUTO_TRADE`와 `KIWOOM_ALLOW_TRADE_EXECUTION` 값 재확인

2. 데이터 정합성
- `portfolio` vs `positions` vs `trades` 동기화 상태
- 성과 컬럼(`result_*`) 업데이트 주기 확인

3. 레이트리밋 대응
- `429` 빈도 높으면 주기/후보수/재시도 파라미터 조정

4. 로그 모니터링
- `logs/worker.log`에서 `WARNING` 패턴 추적
- Markdown 파싱 실패, 토큰 만료, API 재시도 빈도 점검

5. 문서/코드 동기화
- 조건/스케줄/테이블 변경 시 `README`와 `worker.yaml` 동시 갱신

---

## 15. 관련 파일 맵

- 워커 엔트리: `worker/main.py`
- 신호 엔진: `worker/monitor.py`
- AI 판단: `worker/claude_judge.py`
- 스크리닝: `worker/stock_analyzer.py`
- DB 계층: `data/db.py`
- 알림/봇: `notifications/telegram.py`, `notifications/telegram_bot.py`
- MCP 확장: `kiwoom_mcp/README.md`

---

## 16. Data Dictionary (컬럼 사전)

아래는 `data/db.py:init_db()` 기준의 실무 핵심 컬럼 사전입니다.

### 16.1 `watchlist`
| 컬럼 | 타입 | 설명 | 예시 |
|---|---|---|---|
| `code` | TEXT PK | 종목코드 | `000270` |
| `name` | TEXT | 종목명 | `기아` |
| `enabled` | INTEGER | 감시 여부(1/0) | `1` |
| `horizon` | TEXT | 매매 기간(단기/중기/장기) | `중기` |
| `strategy_note` | TEXT | 전략 메모/표식 | `[REASSESS_REQUIRED] ...` |
| `sector_code` | TEXT | 업종 코드 | `G25` |
| `target_price` | INTEGER/NULL | (레거시/보조) 목표가 | `165000` |
| `stop_loss_price` | INTEGER/NULL | (레거시/보조) 손절가 | `152000` |
| `add_buy_price` | INTEGER/NULL | (레거시/보조) 추매가 | `150000` |
| `rsi_oversold` 등 조건 컬럼 | INTEGER/REAL/NULL | 조건별 threshold/flag | `35`, `1.8`, `1` |

### 16.2 `conditions_def`
| 컬럼 | 타입 | 설명 | 예시 |
|---|---|---|---|
| `id` | TEXT PK | 조건 ID | `rsi_oversold` |
| `name` | TEXT | 조건명 | `RSI 과매도` |
| `evaluator` | TEXT | 평가 함수 키 | `rsi_lte` |
| `param` | TEXT | watchlist 컬럼명 | `rsi_oversold` |
| `cooldown_minutes` | INTEGER | 재발동 쿨다운 | `120` |
| `message` | TEXT | 트리거 메시지 템플릿 | `RSI {rsi:.1f} <= {threshold}` |
| `signal_type` | TEXT | `entry/exit/add/both` | `entry` |
| `sort_order` | INTEGER | 정렬 우선순위 | `10` |
| `description` | TEXT | 설명 | `단기 과매도 진입` |

### 16.3 `signals`
| 컬럼 | 타입 | 설명 | 예시 |
|---|---|---|---|
| `id` | INTEGER PK | 신호 ID | `1024` |
| `created_at` | TEXT | 생성시각(KST) | `2026-04-28 10:20:47` |
| `stock_code`/`stock_name` | TEXT | 종목 식별 | `000270` / `기아` |
| `current_price` | INTEGER | 신호 시점 가격 | `157900` |
| `triggered_conditions` | TEXT | 발동 조건 목록 문자열 | `RSI..., MA5...` |
| `signal_type` | TEXT | 신호 타입 | `entry` |
| `in_portfolio` | INTEGER | 보유 여부(1/0) | `1` |
| `claude_opinion` | TEXT | AI 원문 판단 | `[매수] ...` |
| `verdict` | TEXT | 요약 판정 | `매수` |
| `action` | TEXT | 실제 실행 액션 | `매수` |
| `decision_status` | TEXT | `normal/fallback/incomplete_context` | `normal` |
| `decision_confidence` | INTEGER | 신뢰도(0~100) | `78` |
| `indicator_snapshot` | TEXT(JSON) | 기술지표 스냅샷 | `{...}` |
| `stock_feature_snapshot` | TEXT(JSON) | 가격/체결/지표 확장 스냅샷 | `{...}` |
| `dart_summary`/`news_summary` | TEXT | 공시/뉴스 요약 | `...` |
| `market_snapshot`/`portfolio_snapshot` | TEXT | 시장/포트폴리오 요약 | `KOSPI ...` |
| `tool_sequence` | TEXT(JSON) | 에이전트 툴 호출 순서 | `["get_price", ...]` |
| `reasoning_chain` | TEXT(JSON) | 에이전트 추론 요약 | `["조건확인", ...]` |
| `model_id`/`prompt_version`/`policy_version` | TEXT | 재현성 메타 | `gpt-4.1` |
| `source` | TEXT | 신호 소스 | `monitor`, `dip_buy`, `news_monitor` |
| `result_1d`/`result_pct`/`result_5d`/`result_10d` | REAL | 사후 성과 | `1.24`, `2.91` |

### 16.4 `positions`
| 컬럼 | 타입 | 설명 | 예시 |
|---|---|---|---|
| `stock_code` | TEXT PK | 종목코드 | `000270` |
| `stock_name` | TEXT | 종목명 | `기아` |
| `avg_price` | INTEGER | 평단 | `157900` |
| `quantity` | INTEGER | 보유수량 | `17` |
| `target_price` | INTEGER | 목표가 | `165000` |
| `stop_loss_price` | INTEGER | 손절가 | `152000` |
| `add_buy_price` | INTEGER | 추가매수가 | `150000` |
| `updated_at` | TEXT | 수정시각 | `2026-04-28 12:00:00` |

### 16.5 `portfolio`
| 컬럼 | 타입 | 설명 | 예시 |
|---|---|---|---|
| `stock_code` | TEXT PK | 종목코드 | `000270` |
| `stock_name` | TEXT | 종목명 | `기아` |
| `quantity` | INTEGER | 수량 | `17` |
| `avg_price` | INTEGER | 평단 | `157900` |
| `current_price` | INTEGER | 현재가 | `159000` |
| `eval_amount` | INTEGER | 평가금액 | `2703000` |
| `profit_loss` | INTEGER | 평가손익 | `18700` |
| `profit_rate` | REAL | 수익률 | `0.70` |
| `updated_at` | TEXT | 동기화 시각 | `...` |

### 16.6 `trades`
| 컬럼 | 타입 | 설명 | 예시 |
|---|---|---|---|
| `trade_id` | TEXT PK | 거래 고유 ID(주문/체결) | `0075632` |
| `executed_at` | TEXT | 체결 시각/일자 | `2026-04-28` |
| `stock_code`/`stock_name` | TEXT | 종목 식별 | `000270` / `기아` |
| `side` | TEXT | `매수/매도` | `매수` |
| `quantity` | INTEGER | 체결 수량 | `17` |
| `price` | INTEGER | 체결가 | `157900` |
| `amount` | INTEGER | 체결대금 | `2684300` |
| `fee`/`tax` | INTEGER | 비용 | `0`, `0` |
| `result_1d`/`result_3d`/`result_5d` | REAL | 사후성과 | `0.91` |

### 16.7 `paper_trades`
| 컬럼 | 타입 | 설명 | 예시 |
|---|---|---|---|
| `id` | INTEGER PK | 모의거래 ID | `55` |
| `created_at` | TEXT | 기록시각 | `...` |
| `stock_code`/`stock_name` | TEXT | 종목 식별 | `...` |
| `order_type` | TEXT | `buy/sell` | `buy` |
| `quantity`/`price` | INTEGER | 수량/가격 | `10` / `25000` |
| `signal_id` | INTEGER/NULL | 연결된 신호 ID | `1024` |
| `verdict` | TEXT | 판단 | `매수` |
| `result_1d`/`result_3d`/`result_5d` | REAL | 사후성과 | `...` |

### 16.8 `screening_log`
| 컬럼 | 타입 | 설명 | 예시 |
|---|---|---|---|
| `id` | INTEGER PK | 스크리닝 로그 ID | `320` |
| `created_at` | TEXT | 생성시각 | `...` |
| `stock_code`/`stock_name` | TEXT | 종목 식별 | `...` |
| `source` | TEXT | 후보 출처 | `intraday`, `daily` |
| `recommendation` | TEXT | AI 결론 | `관심종목 등록` |
| `reason` | TEXT | 근거 요약 | `...` |
| `met_conditions` | TEXT | 충족 조건 | `...` |
| `rr_ratio` | REAL | 손익비 | `1.8` |
| `current_price` | INTEGER | 평가 기준가 | `...` |
| `indicator_snapshot` | TEXT(JSON) | 지표 스냅샷 | `{...}` |
| `ai_response` | TEXT | AI 원문 | `...` |
| `user_action` | TEXT | 사용자/자동 액션 | `auto_accepted` |
| `result_7d`/`result_30d` | REAL | 사후성과 | `...` |

### 16.9 `cooldowns`
| 컬럼 | 타입 | 설명 | 예시 |
|---|---|---|---|
| `key` | TEXT PK | 쿨다운 키 | `000270:rsi_oversold` |
| `last_sent_at` | TEXT | 마지막 발동 시각 | `...` |
| `next_allowed_at` | TEXT | 재허용 시각 | `...` |

### 16.10 리포트/전략/추적 테이블
| 테이블 | 핵심 컬럼 | 설명 |
|---|---|---|
| `strategy_notes` | `category`, `summary`, `detail` | 전략 기록/회고 본문 |
| `market_reports` | `report_date`, `report_type`, `market_regime`, `volatility`, `trend` | 장전/장초 리포트 |
| `agent_action_logs` | `signal_id`, `tool_sequence`, `reasoning_chain`, `final_opinion` | Agent trace 장기 저장 |
| `strategy_reflection_logs` | `agent_type`, `status`, `praise_tags`, `fix_tags` | 리플렉션 로그 |
| `strategy_policy_state` | `agent_type`, `policy_version`, `policy_json` | 현재 정책 스냅샷 |
| `strategy_policy_update_queue` | `status`, `proposed_policy_json` | 정책 업데이트 큐 |
| `realized_pnl_snapshots` | `scope`, `realized_pnl`, `fee`, `tax` | 실현손익 스냅샷 |

