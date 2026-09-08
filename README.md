# Quant Trading (Agent + RAG)

키움 REST API 기반 **Agent + RAG 자동 트레이딩 시스템**입니다.  
이 문서는 단순 실행법이 아니라, **현재 코드의 프레임워크/아키텍처/데이터 흐름/저장 구조/운영 로직**을 기준으로 프로젝트 전체를 설명합니다.

> **브랜치 정책** — 이 저장소는 두 버전을 의도적으로 병행 유지합니다.
> `feature/agent-mode`(이 브랜치)는 Agent + RAG 버전이고, `main`은 에이전트를 쓰지 않는 일반 버전입니다.
> 어느 쪽도 다른 쪽의 구버전이 아니며 병합 예정도 없습니다. 필요한 AI 키가 서로 다릅니다
> (`feature/agent-mode` → `OPENAI_API_KEY`, `main` → `ANTHROPIC_API_KEY`).
> `main`에는 `worker/agents/`가 없고 `use_agent_mode` 설정도 인식하지 않습니다.

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
quant-trading/
  config/
    worker.yaml                # 워커 주기/AI/리스크/프리필터 설정
    conditions.yaml            # 조건 백업/초기값
    krx_holidays.yaml          # 휴장일 목록 (_run_on_open_day 판정)
  data/
    db.py                      # 스키마/마이그레이션/CRUD/분석 쿼리
    trading.db                 # 모의 DB (DB_PATH로 변경 가능)
    trading_real.db            # 실전 DB
    trading_tuning.db
    faiss_*.bin                # RAG 인덱스 7종 (gitignore)
  notifications/
    telegram.py                # 알림 전송
    telegram_bot.py            # 텔레그램 명령/콜백 처리
  worker/
    main.py                    # 엔트리포인트 + 스케줄러 + 잡 구현 + _auto_execute
    monitor.py                 # 종목 조건 평가, Signal 생성
    claude_judge.py            # AI 판단 엔진 (하네스 + Agent + 레거시 폴백)
    stock_analyzer.py          # 장중/종가 스크리닝 + 리뷰
    portfolio_sync.py          # 계좌/체결 동기화
    indicators.py              # 기술지표/패턴 계산
    adaptive_policy.py         # 과거 성과 기반 진입 강도 조정 + 진입 게이트
    strategy_reflection.py     # 리플렉션/정책 업데이트 루프
    watchlist_policy.py        # watchlist/positions 필드 정규화
    watchlist_manager.py       # watchlist 재평가/등록/삭제
    result_tracker.py          # 신호/거래/스크리닝 사후 성과 갱신
    worker_utils.py            # 세션/시간/포맷 공통 유틸
    daily_report.py            # 장전/장초/일일 리포트 생성
    report.py                  # 리포트 조회 유틸
    strategy_log.py            # 전략 노트 기록 헬퍼
    cooldown.py                # 쿨다운 키 관리
    rag_indexer.py             # RAG 배치/실시간 인덱싱
    clients/
      kiwoom_client.py         # 키움 REST 래퍼
      dart_client.py           # DART 공시/재무
      news_client.py           # 네이버 뉴스 + RSS
      global_market.py         # 글로벌 지수/거시 지표
    agents/
      base_agent.py            # 공통 런타임(Agent 아님)
      judgment_agent.py        # 운영 Agent 1
      research_agent.py        # 운영 Agent 2
      tools/
        registry.py            # 역할별 toolset 로딩
        market_tools.py  portfolio_tools.py  db_tools.py
        info_tools.py    order_tools.py      rag_tools.py
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

---

## 4. 실행 프로세스 상세

## 4.1 프로세스 시작 (`worker/main.py`)

1. `.env` 로드 및 타임존 설정(`Asia/Seoul`)
2. DB 초기화 (`init_db`)
3. 포트폴리오 초기 동기화 (`sync_all`)
4. 텔레그램 봇 스레드 시작 (`start_bot_thread`)
5. APScheduler(`BackgroundScheduler`) 잡 등록
6. `_repair_verdicts()` 1회 실행 — 미판정 신호 verdict 재파싱 복구
7. `startup_run_check` 1회 실행 (개장일에만)
8. `SIGTERM`/`SIGINT` 핸들러 등록 후 `scheduler.start()`, 이후 1초 슬립 루프로 상주

모든 스케줄 잡은 `_run_on_open_day()` 래퍼를 통해 실행되며, `config/krx_holidays.yaml` 기준 휴장일에는 건너뜁니다.

## 4.2 메인 감시 루프 (`run_check`)

`run_check(mode)`는 스케줄러에서 두 모드로 나뉘어 호출됩니다.

- `entry_only` — 진입 후보 평가, `entry_interval_seconds`(기본 600초) 주기
- `exit_only` — 보유 종목 청산 점검, `exit_interval_seconds`(기본 60초) 주기

기동 직후 `startup_run_check`는 모드 없이 전체 평가로 1회 실행됩니다.

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
- **Agent 모드** (`use_agent_mode: true`): 항상 OpenAI Function Calling 사용 (`OPENAI_API_KEY` 필수)
  - `judgment_agent_model` / `judgment_agent_api_key` / `judgment_agent_base_url`로 worker.yaml에서 오버라이드 가능
  - OpenAI 호환 엔드포인트(`base_url` 설정)를 통해 다른 모델로도 교체 가능
- **레거시 모드** (폴백 또는 `use_agent_mode: false`): `ANTHROPIC_API_KEY` 있으면 Anthropic Claude, 없으면 OpenAI

### 6.2 판단 경로
1. 하네스 검사 (`harness_check`)
2. Agent 모드 판단 (tool-calling)
3. 실패/제한 시 레거시 프롬프트 경로 폴백

하네스 분기 기준:
- `SKIP` — 당일 홀드 3회 이상 + 강한 전환 신호 없음. 다음 거래일 09:00 쿨다운 리셋으로 자동 해제됩니다.
  이 게이트는 `entry`/`both`에만 적용되고, **`exit`/`add`는 제외**됩니다(보유 포지션 청산·추가매수 판단을 막지 않기 위함).
- `DIRECT_SELL` — 강한 청산 조건(`strong_exit_condition_ids`) 충족 시 모델 호출 없이 매도 판정
- `AMBIGUOUS` — 모델 판단으로 위임

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
- adaptive policy로 수량 multiplier 조정 및 신규 진입 게이트 적용
  - `entry` 신호가 게이트에 막히면 주문은 내지 않지만, 신호 자체는 `source="adaptive_gate_block"`으로 DB에 저장합니다.
    3일 후 `result_pct`가 자동 기록되어 adaptive 통계가 실제 시장 결과로 계속 갱신됩니다
    (차단이 통계를 얼려 스스로를 다시 차단하는 순환 트랩 방지)
- 정책 최소 신뢰도 미달 차단 — `decision_confidence`가 정책 하한 미만이면 주문 스킵
- 매수 하드캡: 현금/증거금 주문가능수량 상한
- 매도 하드캡: 보유수량 초과 방지
- weak exit는 부분매도로 제한 + 확인 지연
- `avoid_targets`는 기본적으로 **소프트 제약**으로 동작
  - 회피 대상 종목이어도 강한 신호면 진입 가능
  - 대신 매수 수량을 정책 계수(예: `avoid_targets_soft_factor`)로 축소

### 7.1 Adaptive policy 스탠스

`worker/adaptive_policy.py`가 **최근 90일** 동종 신호 성과(표본 수, 3일 승률, 3일 평균수익)와 시장 레짐,
최근 매수 연속손실, 발동 조건 개수로 score를 계산해 스탠스를 정합니다.

| 스탠스 | 조건 | 수량 배수 | 신규 진입 |
|---|---|---|---|
| `aggressive` | `score >= 2` | `1.2` | 허용 |
| `conservative` | `score <= -2` | `0.7` | `add`이거나 (발동조건 3개 이상 + `risk_off` 아님)일 때만 |
| `balanced` | 그 외 | `1.0` | 표본 15건 이상 + 3일 승률 45% 미만이면 차단(`neg_gate`) |

`balanced`의 차단 기준에 **표본 15건 이상** 조건이 붙어 있어, 90일 창에서 표본이 소진되면 게이트가 자동 해제됩니다.

주문 후 후처리:
- trade/notes/cooldown 반영
- 포트폴리오 재동기화
- 매수 시 포지션 생성 및 AI 포지션 값(목표/손절/추매가) 설정
- 전량매도 시 watchlist 결정(keep/drop/reassess)

---

## 8. 스케줄러 잡 구성 (핵심)

`main()`에서 등록되는 잡 전체입니다. 시간은 KST이고, 모두 `_run_on_open_day()`로 감싸여 휴장일에는 실행되지 않습니다.

### 8.1 감시/동기화

| job id | 시간·주기 | 내용 |
|---|---|---|
| `monitor_entry` | 월–금 08–18시, `entry_interval_seconds`마다 | `run_check("entry_only")` |
| `monitor_exit` | 월–금 08–18시, `exit_interval_seconds`마다 | `run_check("exit_only")` |
| `reset_cooldowns` | 매일 09:00 | 전 종목 쿨다운 리셋 |
| `sync_premarket` / `sync_open` / `sync_close` | 08:30 / 09:01 / 18:05 | `auto_sync` |
| `sync_realtime` | 월–금 08–18시, `sync_realtime_minutes`마다 | `auto_sync` |

### 8.2 리포트/스크리닝

| job id | 시간·주기 | 내용 |
|---|---|---|
| `premarket_report` | `premarket_report_time` (기본 08:50) | 장전 리포트 |
| `opening_report` | `opening_report_time` (기본 09:05) | 장초 리포트 |
| `intraday_scan` / `intraday_scan_pm` | 11:00 / 14:00 | 장중 스크리닝 |
| `daily_screening` | 15:40 | 종가 스크리닝 |
| `daily_review` | 16:10 | 일일 복기 + 체크리스트 |
| `weekly_performance_report` | 월 09:00 | 주간 성과 리포트 |
| `weekly_self_correction` | 월 09:05 | 주간 자기교정 |
| `news_monitor_fixed_N` | `news_monitor_times` (기본 08:55, 12:00) | 뉴스 리스크 모니터링 |

### 8.3 성과 갱신/정비

| job id | 시간·주기 | 내용 |
|---|---|---|
| `result_update` / `screening_result_update` / `trade_result_update` | 월–금 09–18시, 30분마다 | 신호·스크리닝·거래 사후 성과 갱신 |
| `trailing_stops` | 월–금 09–15시, 30분마다 | 트레일링 스톱 점검 |
| `removal_check` | 월–금 09–15시, 30분마다 | 삭제 후보 점검 |
| `dip_buy` | 월–금 09–14시, 30분마다 | 시장 급락 스캔 |
| `reassess_watchlist` | 월–금 09:15 | watchlist 재평가 |
| `inactive_alert` | 월–금 08:30 | 미발동 종목 알림 |
| `daily_reflection_policy_loop` | 월–금 16:20 | 리플렉션 → 정책 갱신 사이클 |
| `verdict_repair` | 월–금 16:35 (+기동 시 1회) | 미판정 신호 verdict 재파싱 |
| `weekly_agent_action_log_refresh` | 월 09:10 | agent trace 로그 정비 |

### 8.4 RAG 인덱싱

`rag_realtime_index: false`일 때만 배치 잡을 등록합니다.

| job id | 시간·주기 | 내용 |
|---|---|---|
| `rag_batch_index_fixed_N` | `rag_batch_times` (기본 08:55, 13:05, 16:05) | 배치 인덱싱 |
| `rag_batch_index_fixed` | `rag_batch_times`가 비고 `rag_batch_hours`가 있을 때 | 시간대 지정 배치 |
| `rag_batch_index_close` | 18:20, `rag_batch_run_close: true`일 때만 | 장마감 후 보정 인덱싱 |

세부 시간/주기는 `config/worker.yaml`이 소스 오브 트루스입니다.

---

## 9. 데이터 저장 구조 (DB 스키마)

`data/db.py:init_db()` 기준. 마이그레이션은 `ALTER TABLE` 방식으로 누적 적용됩니다.

### 9.1 핵심 테이블

1. `watchlist`
- 종목 마스터 + 조건 필드 + 전략 메모
- 주요 컬럼: `code`, `name`, `enabled`, `horizon`, `strategy_note_id`, 개별 조건 컬럼들, `sector_code`

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

7. `screening_log`
- 장중/종가 스크리닝 결과 저장
- `source`, `recommendation`, `reason`, `met_conditions`, `rr_ratio`, `indicator_snapshot`, `ai_response`, `user_action`, `result_7d/30d`

8. `cooldowns`
- 신호/종목별 발동 제어
- `key`, `last_sent_at`, `next_allowed_at`

9. `strategy_notes`
- 전략 메모/리포트 본문 저장
  - `meta_json`으로 일일 복기 구조화 데이터 저장 가능
    - 예: `market_context`, `wins/losses`, `next_day_policy`

10. `market_reports`
- premarket/open 리포트 구조화 저장
  - `meta_json.agent_policy` 저장
    - 예: `aggressive_entry`, `avoid_targets`, `increase_cash`, `applied_session`
    - 사후 리플레이(“왜 그날 그렇게 판단했는지”) 용도

11. `agent_action_logs`
- agent 의사결정 trace 장기 저장

12. `strategy_reflection_logs`, `strategy_policy_state`, `strategy_policy_update_queue`
- 리플렉션과 정책 자동 갱신 파이프라인 상태 저장

13. `strategy_policy_cycle_logs`
- 정책 갱신 사이클 실행 이력
- `agent_type`, `outcome`(`applied/queued/skipped/rejected`), `reason_code`, `sample_count`, `avg_quality`

14. `realized_pnl_snapshots`
- 실현손익 스냅샷(당일/기간)

15. `watchlist_strategy_note_links`, `position_strategy_note_links`
- watchlist/positions ↔ `strategy_notes` 연결 이력
- `stock_code`, `strategy_note_id`, `linked_at`, `reason`, `is_active`

16. `improvement_issues`
- 일일 복기에서 도출된 개선 이슈 트래커
- `issue_date`, `title`, `category`, `priority`(`P1/P2/...`), `status`(`open/done/wontfix`), `resolution_note`, `resolved_at`

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
- 텔레그램은 단순 알림 채널이 아니라, 자동매매 안전장치와 수동 개입 인터페이스 역할을 함께 수행
- AUTO_TRADE 환경에서도 승인 플로우/거부 플로우를 통해 비정상 주문을 차단하는 마지막 게이트로 사용 가능
- 장애 시(포맷 실패/일시 오류) plain text 폴백으로 신호 전달 연속성 유지

---

## 12. 설정 파일 가이드

## 12.1 `.env`
템플릿은 `.env.example`입니다.

| 그룹 | 변수 | 비고 |
|---|---|---|
| 키움 | `KIWOOM_APP_KEY`, `KIWOOM_APP_SECRET`, `KIWOOM_ACCOUNT_NO`, `KIWOOM_BASE_URL` | 공통 필수 |
| 실행모드 | `AUTO_TRADE` | `false`면 알림/모의 기록만 |
| 데이터 | `DB_PATH`, `LOG_PREFIX` | 모의 `data/trading.db` / 실전 `data/trading_real.db` |
| 알림 | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | |
| OpenAI | `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_MODEL_MINI` | **Agent 모드(`use_agent_mode: true`)에서 필수** |
| Anthropic | `ANTHROPIC_API_KEY`, `CLAUDE_MODEL`, `CLAUDE_MODEL_MINI` | 레거시 폴백 경로에서 사용 |
| DART | `DART_API_KEY` | 공시/재무 컨텍스트 |
| 네이버 뉴스 | `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET` | 종목/거시 뉴스 |
| Alpha Vantage | `ALPHAVANTAGE_API_KEY`, `ALPHAVANTAGE_MIN_INTERVAL_SEC`, `ALPHAVANTAGE_MAX_RETRIES`, `ALPHAVANTAGE_RETRY_BACKOFF_SEC`, `ALPHAVANTAGE_CACHE_TTL_SEC` | 글로벌 지수, 레이트리밋 튜닝 |

AI 키는 모드에 따라 갈립니다. 이 브랜치는 `use_agent_mode: true`가 기본이므로 `OPENAI_API_KEY`가 필수입니다.

## 12.2 `config/worker.yaml`

| 최상위 키 | 핵심 항목 |
|---|---|
| `worker` | 주기(`interval_seconds`, `entry_interval_seconds`, `exit_interval_seconds`, `sync_realtime_minutes`) |
| | 판단 모드(`use_ai_judgment`, `use_agent_mode`) |
| | 모델 라우팅(`judgment_agent_model` / `research_agent_model` + 각 `_base_url`, `_api_key`) |
| | Agent 실행(`agent_max_steps`, `agent_max_tokens`, `agent_target_unique_tools`, `agent_rate_limit_*`, `research_agent_*`) |
| | 리스크(`buy_reentry_block_minutes`, `weak_exit_confirm_minutes`, `weak_exit_partial_ratio`, `missing_qty_policy`, `fallback_buy_ratio`, `min_target_profit_pct`, `strong_exit_condition_ids`) |
| | 급락 스캔(`dip_buy_threshold`, `dip_buy_max_stocks`, `dip_buy_cooldown_hours`) |
| | 리포트/뉴스 시각(`premarket_report_time`, `opening_report_time`, `news_monitor_times`, `news_cooldown_hours`) |
| | RAG(`rag_realtime_index`, `rag_batch_times`, `rag_batch_hours`, `rag_batch_minute`, `rag_batch_size`, `rag_batch_run_close`) |
| | `strategy_tuning` 하위 블록 |
| `watchlist_management` | `inactive_days_alert`, `no_trade_days_removal`, `no_trade_days_after_liquidation`, `alert_interval_days` |
| `sector` | `max_holdings` — 동일 섹터 최대 보유 종목 수 |
| `prefilter` | 스크리닝 1차 필터(`market_cap_min`, `change_upper/lower`, `rsi_max`, `ma_ratio`, `volume_ratio_min/max`, `consecutive_candles`) |
| | 레짐 보정(`market_bull_threshold`, `market_bear_threshold`, `bull_*`, `bear_*`) |
| | 후보 수 제한(`prefilter_input_cap`, `prefilter_fallback_candidates`, `max_ai_candidates`) |

---

## 13. 실행 방법

Windows:

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

Linux (운영 서버):

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python worker/main.py
```

서버 상주 실행은 systemd(`quant-worker.service`)로 관리합니다. unit 정의, 자동실행 on/off 현재 상태,
재개 절차는 `docs/oracle_cloud_setup.md`를 보십시오.

---

## 14. 운영/정리 시 체크리스트

1. 실제 주문 안전장치 확인
- `AUTO_TRADE` 값 재확인

2. 데이터 정합성
- `portfolio` vs `positions` vs `trades` 동기화 상태
- 성과 컬럼(`result_*`) 업데이트 주기 확인

3. 레이트리밋 대응
- `429` 빈도 높으면 주기/후보수/재시도 파라미터 조정

4. 로그 모니터링
- `logs/worker.log`에서 `WARNING` 패턴 추적
- Markdown 파싱 실패, 토큰 만료, API 재시도 빈도 점검

5. 사후 성과 데이터 품질
- `worker/result_tracker.py`가 ±200%를 넘는 수익률을 경고 로그로 남깁니다. 반복되면 기준가/액면분할 등 데이터 이슈를 확인하십시오.
- `source="adaptive_gate_block"` 신호는 주문이 없었던 차단 기록입니다. 승률·평균 집계에서 제외하고 해석하십시오.

6. 서버 배포/자동실행
- systemd unit 정의와 자동실행 on/off 상태는 `docs/oracle_cloud_setup.md`가 소스 오브 트루스입니다.

7. 문서/코드 동기화
- 조건/스케줄/테이블 변경 시 `README`와 `worker.yaml` 동시 갱신

---

## 15. 관련 파일 맵

- 워커 엔트리 + 스케줄러 + 주문 실행: `worker/main.py`
- 신호 엔진: `worker/monitor.py`
- AI 판단: `worker/claude_judge.py`
- Agent/도구: `worker/agents/`, `worker/agents/tools/registry.py`
- 진입 게이트/수량 정책: `worker/adaptive_policy.py`
- 스크리닝: `worker/stock_analyzer.py`
- 사후 성과 갱신: `worker/result_tracker.py`
- watchlist 재평가: `worker/watchlist_manager.py`, `worker/watchlist_policy.py`
- 리플렉션/정책 갱신: `worker/strategy_reflection.py`
- 외부 API 클라이언트: `worker/clients/`
- DB 계층: `data/db.py`
- 알림/봇: `notifications/telegram.py`, `notifications/telegram_bot.py`
- 서버 배포/자동실행: `docs/oracle_cloud_setup.md`
- MCP 확장: `kiwoom_mcp/` 서브모듈 (`git submodule update --init` 후 `kiwoom_mcp/README.md`)

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
| `strategy_note_id` | INTEGER/NULL | `strategy_notes.id` 링크 (최근 전략 노트 연결) | `18` |
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

### 16.7 `screening_log`
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

### 16.8 `cooldowns`
| 컬럼 | 타입 | 설명 | 예시 |
|---|---|---|---|
| `key` | TEXT PK | 쿨다운 키 | `000270:rsi_oversold` |
| `last_sent_at` | TEXT | 마지막 발동 시각 | `...` |
| `next_allowed_at` | TEXT | 재허용 시각 | `...` |

### 16.9 리포트/전략/추적 테이블
| 테이블 | 핵심 컬럼 | 설명 |
|---|---|---|
| `strategy_notes` | `id`, `category`, `summary`, `detail`, `meta_json` | 전략 메모/복기 저장 (id로 watchlist/positions 연결) |
| `market_reports` | `report_date`, `report_type`, `market_regime`, `volatility`, `trend` | 장전/장초 리포트 |
| `agent_action_logs` | `signal_id`, `tool_sequence`, `reasoning_chain`, `final_opinion` | Agent trace 장기 저장 |
| `strategy_reflection_logs` | `agent_type`, `status`, `praise_tags`, `fix_tags` | 리플렉션 로그 |
| `strategy_policy_state` | `agent_type`, `policy_version`, `policy_json` | 현재 정책 스냅샷 |
| `strategy_policy_update_queue` | `status`, `proposed_policy_json` | 정책 업데이트 큐 |
| `realized_pnl_snapshots` | `scope`, `realized_pnl`, `fee`, `tax` | 실현손익 스냅샷 |

