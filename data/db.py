"""
SQLite 거래 로그 DB
"""

import json
import os
import sqlite3
from datetime import datetime, timezone, timedelta

_KST = timezone(timedelta(hours=9))


def _now_kst() -> datetime:
    return datetime.now(_KST).replace(tzinfo=None)

_default_db = os.path.join(os.path.dirname(__file__), "trading.db")
_env_db = os.getenv("DB_PATH")
if _env_db and not os.path.isabs(_env_db):
    # 상대경로는 프로젝트 루트(data/ 의 부모) 기준으로 해석
    _env_db = os.path.join(os.path.dirname(__file__), '..', _env_db)
DB_PATH = _env_db or _default_db


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _p(value) -> int:
    """문자열 숫자 파싱 (부호/소수점/콤마 처리)"""
    if not value:
        return 0
    try:
        return abs(int(float(str(value).replace(",", "").strip() or "0")))
    except (ValueError, TypeError):
        return 0


def _f(value) -> float:
    if not value:
        return 0.0
    try:
        return float(str(value).replace(",", "").strip() or "0")
    except (ValueError, TypeError):
        return 0.0


# HOLD verdict is treated as "correct" when return stays in neutral band.
HOLD_NEUTRAL_BAND_PCT = max(0.0, float(os.getenv("HOLD_NEUTRAL_BAND_PCT", "1.0")))


def _is_verdict_hit_3d(verdict: str | None, return_3d: float | None, hold_band_pct: float = HOLD_NEUTRAL_BAND_PCT) -> bool:
    if verdict is None or return_3d is None:
        return False
    band = max(0.0, float(hold_band_pct))
    r3 = float(return_3d)
    if verdict == "매수":
        return r3 > band
    if verdict == "매도":
        return r3 < -band
    if verdict == "홀드":
        return -band <= r3 <= band
    return False


_WL_SIGNAL_FIELDS = (
    "rsi_oversold", "rsi_overbought", "rsi_oversold_intraday", "rsi_critical",
    "volume_surge_ratio", "cci_oversold", "cci_overbought",
    "golden_cross", "death_cross", "ma20_support_break", "ma5_support_break",
    "ma5_recovery", "new_high_20d", "macd_golden_cross", "macd_death_cross",
    "bollinger_upper_break", "bollinger_lower_break", "bollinger_critical_below",
    "stochastic_golden_cross", "stochastic_death_cross",
    "ichimoku_golden_cross", "ichimoku_death_cross",
    "ichimoku_cloud_breakout", "ichimoku_cloud_breakdown",
)

def _repair_legacy_watchlist_conditions(conn: sqlite3.Connection, stock_code: str | None = None) -> int:
    """
    Legacy rows may contain only horizon/name and no monitoring conditions.
    Do not infer trading conditions with defaults.
    Instead, mark row as "reassessment required" and disable monitoring until re-judged.
    """
    where_code = " AND code = ?" if stock_code else ""
    rows = conn.execute(
        f"SELECT code, {', '.join(_WL_SIGNAL_FIELDS)} FROM watchlist WHERE 1=1{where_code}",
        ((stock_code,) if stock_code else ()),
    ).fetchall()
    repaired = 0
    for row in rows:
        if any(row[f] is not None for f in _WL_SIGNAL_FIELDS):
            continue
        note_row = conn.execute(
            "SELECT strategy_note FROM watchlist WHERE code = ?",
            (row["code"],),
        ).fetchone()
        old_note = (note_row["strategy_note"] if note_row and note_row["strategy_note"] else "").strip()
        marker = "[REASSESS_REQUIRED]"
        if marker in old_note:
            new_note = old_note
        else:
            ts = _now_kst().strftime("%Y-%m-%d %H:%M:%S")
            extra = f"{marker} {ts} legacy watchlist row had no monitoring conditions."
            new_note = f"{old_note}\n{extra}".strip() if old_note else extra
        conn.execute(
            "UPDATE watchlist SET enabled = 0, strategy_note = ? WHERE code = ?",
            (new_note, row["code"]),
        )
        repaired += 1
    return repaired


def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS watchlist (
                code       TEXT PRIMARY KEY,
                name       TEXT NOT NULL,
                enabled    INTEGER NOT NULL DEFAULT 1
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS conditions_def (
                id               TEXT PRIMARY KEY,
                name             TEXT NOT NULL,
                evaluator        TEXT NOT NULL,
                param            TEXT NOT NULL,
                cooldown_minutes INTEGER NOT NULL DEFAULT 60,
                message          TEXT NOT NULL,
                chart_field      TEXT,
                sort_order       INTEGER NOT NULL DEFAULT 0,
                description      TEXT DEFAULT ''
            )
        """)
        # 기존 DB 마이그레이션
        try:
            conn.execute("ALTER TABLE conditions_def ADD COLUMN description TEXT DEFAULT ''")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE conditions_def ADD COLUMN signal_type TEXT NOT NULL DEFAULT 'both'")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE watchlist ADD COLUMN horizon TEXT NOT NULL DEFAULT '중기'")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE watchlist ADD COLUMN created_at TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass
        # ── watchlist 정규화: conditions JSON → 개별 컬럼 마이그레이션 ──
        _WL_COLUMNS = [
            # value fields
            ("rsi_oversold", "INTEGER DEFAULT NULL"),
            ("rsi_overbought", "INTEGER DEFAULT NULL"),
            ("rsi_oversold_intraday", "INTEGER DEFAULT NULL"),
            ("rsi_critical", "INTEGER DEFAULT NULL"),
            ("volume_surge_ratio", "REAL DEFAULT NULL"),
            ("cci_oversold", "INTEGER DEFAULT NULL"),
            ("cci_overbought", "INTEGER DEFAULT NULL"),
            # flag fields
            ("golden_cross", "INTEGER DEFAULT NULL"),
            ("death_cross", "INTEGER DEFAULT NULL"),
            ("ma20_support_break", "INTEGER DEFAULT NULL"),
            ("ma5_support_break", "INTEGER DEFAULT NULL"),
            ("ma5_recovery", "INTEGER DEFAULT NULL"),
            ("new_high_20d", "INTEGER DEFAULT NULL"),
            ("macd_golden_cross", "INTEGER DEFAULT NULL"),
            ("macd_death_cross", "INTEGER DEFAULT NULL"),
            ("bollinger_upper_break", "INTEGER DEFAULT NULL"),
            ("bollinger_lower_break", "INTEGER DEFAULT NULL"),
            ("bollinger_critical_below", "INTEGER DEFAULT NULL"),
            ("stochastic_golden_cross", "INTEGER DEFAULT NULL"),
            ("stochastic_death_cross", "INTEGER DEFAULT NULL"),
            ("ichimoku_golden_cross", "INTEGER DEFAULT NULL"),
            ("ichimoku_death_cross", "INTEGER DEFAULT NULL"),
            ("ichimoku_cloud_breakout", "INTEGER DEFAULT NULL"),
            ("ichimoku_cloud_breakdown", "INTEGER DEFAULT NULL"),
            # metadata
            ("strategy_note", "TEXT DEFAULT ''"),
        ]
        for col, typedef in _WL_COLUMNS:
            try:
                conn.execute(f"ALTER TABLE watchlist ADD COLUMN {col} {typedef}")
            except Exception:
                pass
        try:
            conn.execute("ALTER TABLE watchlist ADD COLUMN sector_code TEXT DEFAULT NULL")
        except Exception:
            pass
        _migrate_watchlist_columns(conn)
        _repair_legacy_watchlist_conditions(conn)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                stock_code TEXT NOT NULL,
                stock_name TEXT NOT NULL,
                current_price INTEGER NOT NULL,
                triggered_conditions TEXT NOT NULL,
                rsi REAL,
                volume_ratio REAL,
                claude_opinion TEXT,
                in_portfolio INTEGER NOT NULL DEFAULT 0
            )
        """)
        # signals id를 0부터 시작 (신규 DB 또는 시퀀스 미초기화 시에만 적용)
        conn.execute("INSERT OR IGNORE INTO sqlite_sequence (name, seq) VALUES ('signals', -1)")
        try:
            conn.execute("ALTER TABLE signals ADD COLUMN in_portfolio INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE signals ADD COLUMN action TEXT DEFAULT NULL")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE signals ADD COLUMN result_pct REAL DEFAULT NULL")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE signals ADD COLUMN signal_type TEXT DEFAULT NULL")
        except Exception:
            pass
        # RAG/학습용 확장 컬럼
        try:
            conn.execute("ALTER TABLE signals ADD COLUMN verdict TEXT DEFAULT NULL")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE signals ADD COLUMN indicator_snapshot TEXT DEFAULT NULL")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE signals ADD COLUMN dart_summary TEXT DEFAULT NULL")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE signals ADD COLUMN chart_patterns TEXT DEFAULT NULL")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE signals ADD COLUMN result_1d REAL DEFAULT NULL")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE signals ADD COLUMN result_5d REAL DEFAULT NULL")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE signals ADD COLUMN result_10d REAL DEFAULT NULL")
        except Exception:
            pass
        # RAG 확장: 신호 시점 컨텍스트
        try:
            conn.execute("ALTER TABLE signals ADD COLUMN news_summary TEXT DEFAULT NULL")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE signals ADD COLUMN market_snapshot TEXT DEFAULT NULL")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE signals ADD COLUMN portfolio_snapshot TEXT DEFAULT NULL")
        except Exception:
            pass
        # Agent 학습용: 도구 사용 순서 + GPT 중간 추론
        try:
            conn.execute("ALTER TABLE signals ADD COLUMN tool_sequence TEXT DEFAULT NULL")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE signals ADD COLUMN reasoning_chain TEXT DEFAULT NULL")
        except Exception:
            pass
        # 스크리닝 AI 판단 이력 (RAG용)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS screening_log (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at         TEXT NOT NULL,
                stock_code         TEXT NOT NULL,
                stock_name         TEXT NOT NULL,
                source             TEXT,
                recommendation     TEXT,
                reason             TEXT,
                met_conditions     TEXT,
                rr_ratio           REAL,
                current_price      INTEGER DEFAULT NULL,
                indicator_snapshot TEXT,
                dart_summary       TEXT DEFAULT NULL,
                news_summary       TEXT DEFAULT NULL,
                market_snapshot    TEXT,
                ai_response        TEXT DEFAULT NULL,
                user_action        TEXT DEFAULT NULL,
                result_7d          REAL DEFAULT NULL,
                result_30d         REAL DEFAULT NULL
            )
        """)
        # screening_log 마이그레이션 (기존 테이블에 컬럼 추가)
        for col, typedef in [
            ("current_price", "INTEGER DEFAULT NULL"),
            ("dart_summary", "TEXT DEFAULT NULL"),
            ("news_summary", "TEXT DEFAULT NULL"),
            ("ai_response", "TEXT DEFAULT NULL"),
        ]:
            try:
                conn.execute(f"ALTER TABLE screening_log ADD COLUMN {col} {typedef}")
            except Exception:
                pass
        # RAG 검색용 인덱스
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_stock_date ON signals (stock_code, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_verdict ON signals (verdict, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_screening_stock_date ON screening_log (stock_code, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_screening_recommendation ON screening_log (recommendation, created_at)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_action_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                signal_id INTEGER NOT NULL,
                stock_code TEXT NOT NULL,
                stock_name TEXT NOT NULL,
                signal_type TEXT DEFAULT NULL,
                tool_sequence TEXT DEFAULT NULL,
                reasoning_chain TEXT DEFAULT NULL,
                final_opinion TEXT DEFAULT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_action_signal_id ON agent_action_logs (signal_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_action_created_at ON agent_action_logs (created_at)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cooldowns (
                key TEXT PRIMARY KEY,
                last_sent_at TEXT DEFAULT NULL,
                next_allowed_at TEXT DEFAULT NULL
            )
        """)
        cooldown_cols = {row[1] for row in conn.execute("PRAGMA table_info(cooldowns)").fetchall()}
        if "next_allowed_at" not in cooldown_cols:
            conn.execute("ALTER TABLE cooldowns ADD COLUMN next_allowed_at TEXT DEFAULT NULL")
        if "last_sent_at" not in cooldown_cols:
            conn.execute("ALTER TABLE cooldowns ADD COLUMN last_sent_at TEXT DEFAULT NULL")
        legacy_rows = conn.execute(
            "SELECT key, last_sent_at FROM cooldowns WHERE next_allowed_at IS NULL AND last_sent_at IS NOT NULL"
        ).fetchall()
        for row in legacy_rows:
            try:
                legacy_last = datetime.strptime(row["last_sent_at"], "%Y-%m-%d %H:%M:%S")
                next_allowed = _infer_legacy_cooldown_until(row["key"], legacy_last)
                conn.execute(
                    "UPDATE cooldowns SET next_allowed_at = ? WHERE key = ?",
                    (next_allowed.strftime("%Y-%m-%d %H:%M:%S"), row["key"]),
                )
            except ValueError:
                pass
        conn.execute("""
            CREATE TABLE IF NOT EXISTS portfolio (
                stock_code    TEXT PRIMARY KEY,
                stock_name    TEXT NOT NULL,
                quantity      INTEGER NOT NULL,
                avg_price     INTEGER NOT NULL,
                current_price INTEGER NOT NULL,
                eval_amount   INTEGER NOT NULL,
                profit_loss   INTEGER NOT NULL,
                profit_rate   REAL NOT NULL,
                updated_at    TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                trade_id     TEXT PRIMARY KEY,
                executed_at  TEXT NOT NULL,
                stock_code   TEXT NOT NULL,
                stock_name   TEXT NOT NULL,
                side         TEXT NOT NULL,
                quantity     INTEGER NOT NULL,
                price        INTEGER NOT NULL,
                amount       INTEGER NOT NULL,
                fee          INTEGER NOT NULL DEFAULT 0,
                tax          INTEGER NOT NULL DEFAULT 0,
                result_1d    REAL DEFAULT NULL,
                result_3d    REAL DEFAULT NULL,
                result_5d    REAL DEFAULT NULL
            )
        """)
        # trades 마이그레이션: 성과 컬럼 추가
        trade_cols = [r["name"] for r in conn.execute("PRAGMA table_info(trades)").fetchall()]
        if "result_1d" not in trade_cols:
            conn.execute("ALTER TABLE trades ADD COLUMN result_1d REAL DEFAULT NULL")
        if "result_3d" not in trade_cols:
            conn.execute("ALTER TABLE trades ADD COLUMN result_3d REAL DEFAULT NULL")
        if "result_5d" not in trade_cols:
            conn.execute("ALTER TABLE trades ADD COLUMN result_5d REAL DEFAULT NULL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS strategy_notes (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                category   TEXT NOT NULL,
                summary    TEXT NOT NULL,
                detail     TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS strategy_reflection_logs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at      TEXT NOT NULL,
                agent_type      TEXT NOT NULL,          -- judgment | research
                stock_code      TEXT DEFAULT '',
                stock_name      TEXT DEFAULT '',
                status          TEXT NOT NULL,          -- completed | incomplete_context ...
                praise_tags     TEXT DEFAULT '[]',      -- JSON array
                reflection_tags TEXT DEFAULT '[]',      -- JSON array
                quality_score   REAL DEFAULT NULL,      -- 0.0 ~ 1.0
                detail_json     TEXT DEFAULT '{}'
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_reflection_agent_date ON strategy_reflection_logs (agent_type, created_at)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS strategy_policy_state (
                agent_type     TEXT PRIMARY KEY,        -- judgment | research
                policy_version TEXT NOT NULL,
                policy_json    TEXT NOT NULL DEFAULT '{}',
                updated_at     TEXT NOT NULL,
                note           TEXT DEFAULT ''
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS strategy_policy_update_queue (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at          TEXT NOT NULL,
                agent_type          TEXT NOT NULL,
                suggestion_json     TEXT NOT NULL DEFAULT '{}',
                low_risk            INTEGER NOT NULL DEFAULT 1,
                status              TEXT NOT NULL DEFAULT 'pending', -- pending/applied/rejected
                applied_version     TEXT DEFAULT '',
                source_reflection_id INTEGER DEFAULT NULL,
                note                TEXT DEFAULT ''
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_policy_queue_status_date ON strategy_policy_update_queue (status, created_at)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS realized_pnl_snapshots (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at    TEXT NOT NULL,
                scope         TEXT NOT NULL,   -- today | period
                start_dt      TEXT DEFAULT NULL,
                end_dt        TEXT DEFAULT NULL,
                realized_pnl  REAL DEFAULT NULL,
                fee           REAL DEFAULT NULL,
                tax           REAL DEFAULT NULL,
                source_api    TEXT NOT NULL,
                raw_json      TEXT DEFAULT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_realized_pnl_scope_date ON realized_pnl_snapshots (scope, created_at)")
        # ── positions 테이블 (매수 후 포지션 관리용) ──
        conn.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                stock_code          TEXT PRIMARY KEY,
                stock_name          TEXT NOT NULL,
                avg_price           INTEGER NOT NULL DEFAULT 0,
                quantity            INTEGER NOT NULL DEFAULT 0,
                target_price        INTEGER NOT NULL DEFAULT 0,
                stop_loss_price     INTEGER NOT NULL DEFAULT 0,
                add_buy_price       INTEGER NOT NULL DEFAULT 0,
                mid_sell_price      INTEGER NOT NULL DEFAULT 0,
                rsi_oversold_add    INTEGER DEFAULT NULL,
                bollinger_lower_break_add INTEGER DEFAULT NULL,
                ma5_recovery_add    INTEGER DEFAULT NULL,
                strategy_note       TEXT DEFAULT '',
                created_at          TEXT NOT NULL DEFAULT '',
                updated_at          TEXT NOT NULL DEFAULT ''
            )
        """)
        # positions 마이그레이션: 기존 보유종목 자동 생성
        _migrate_positions(conn)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS paper_trades (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at  TEXT NOT NULL,
                stock_code  TEXT NOT NULL,
                stock_name  TEXT NOT NULL,
                order_type  TEXT NOT NULL,
                quantity    INTEGER NOT NULL,
                price       INTEGER NOT NULL,
                signal_id   INTEGER,
                verdict     TEXT,
                result_1d   REAL DEFAULT NULL,
                result_3d   REAL DEFAULT NULL,
                result_5d   REAL DEFAULT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_paper_stock ON paper_trades (stock_code, created_at)")
        conn.commit()
    _seed_conditions()


def _migrate_watchlist_columns(conn):
    """conditions JSON → 개별 컬럼 마이그레이션 (1회성).
    JSON에 값이 있고 컬럼이 NULL이면 복사. 완료 후 conditions를 '{}'로 비움."""
    # conditions 컬럼이 없으면 마이그레이션 불필요 (신규 DB)
    try:
        rows = conn.execute("SELECT code, conditions FROM watchlist WHERE conditions != '{}'").fetchall()
    except Exception:
        return  # conditions 컬럼 없음 — 신규 DB
    if not rows:
        return
    # 첫 번째 행에서 이미 컬럼에 값이 있는지 확인 (이미 마이그레이션 완료)
    first = conn.execute("SELECT rsi_oversold FROM watchlist LIMIT 1").fetchone()
    if first and first["rsi_oversold"] is not None:
        return

    _VALUE_FIELDS = {"rsi_oversold", "rsi_overbought", "rsi_oversold_intraday", "rsi_critical",
                     "volume_surge_ratio", "cci_oversold", "cci_overbought"}
    _FLAG_FIELDS = {"golden_cross", "death_cross", "ma20_support_break", "ma5_support_break",
                    "ma5_recovery", "new_high_20d", "macd_golden_cross", "macd_death_cross",
                    "bollinger_upper_break", "bollinger_lower_break", "bollinger_critical_below",
                    "stochastic_golden_cross", "stochastic_death_cross",
                    "ichimoku_golden_cross", "ichimoku_death_cross",
                    "ichimoku_cloud_breakout", "ichimoku_cloud_breakdown"}
    _ALL_FIELDS = _VALUE_FIELDS | _FLAG_FIELDS | {"strategy_note"}

    for row in rows:
        cond = json.loads(row["conditions"])
        sets = []
        vals = []
        for field in _ALL_FIELDS:
            val = cond.get(field)
            if val is None:
                continue
            if field in _FLAG_FIELDS:
                val = 1 if val else 0
            elif field == "strategy_note":
                val = str(val)
            sets.append(f"{field} = ?")
            vals.append(val)
        if sets:
            vals.append(row["code"])
            conn.execute(f"UPDATE watchlist SET {', '.join(sets)} WHERE code = ?", vals)

    # JSON 비우기
    conn.execute("UPDATE watchlist SET conditions = '{}'")


def _migrate_positions(conn):
    """기존 portfolio + watchlist 데이터로 positions 초기 생성 (1회성 마이그레이션)."""
    pos_count = conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
    port_count = conn.execute("SELECT COUNT(*) FROM portfolio").fetchone()[0]
    if pos_count > 0 or port_count == 0:
        return  # 이미 마이그레이션 완료이거나 포트폴리오 없음

    now = _now_kst().strftime("%Y-%m-%d %H:%M:%S")
    holdings = conn.execute("SELECT * FROM portfolio").fetchall()
    watchlist_map = {}
    for row in conn.execute("SELECT code, strategy_note FROM watchlist").fetchall():
        watchlist_map[row["code"]] = row["strategy_note"] or ""

    for h in holdings:
        code = h["stock_code"]
        avg_price = h["avg_price"]

        target_price = int(avg_price * 1.15) if avg_price else 0
        stop_loss_price = int(avg_price * 0.93) if avg_price else 0

        conn.execute(
            """INSERT OR IGNORE INTO positions
                (stock_code, stock_name, avg_price, quantity,
                 target_price, stop_loss_price, add_buy_price,
                 strategy_note, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                code, h["stock_name"], avg_price, h["quantity"],
                target_price, stop_loss_price,
                int(avg_price * 0.92) if avg_price else 0,
                watchlist_map.get(code, ""),
                now, now,
            ),
        )


def _seed_conditions():
    """conditions_def 초기 시드 데이터 (29개). DB가 비어있을 때만 INSERT."""
    SEED = [
        ("target_price", "목표가 도달", "price_gte", "target_price", 240, "목표가 도달 ({price:,}원 >= {threshold:,}원)", None, 0, "설정한 목표 주가에 도달했을 때 발생. 익절 타이밍 신호. 목표가 이상이면 트리거.", "exit"),
        ("stop_loss_price", "손절가 도달", "price_lte", "stop_loss_price", 240, "손절가 도달 ({price:,}원 <= {threshold:,}원)", None, 1, "설정한 손절 주가 이하로 떨어졌을 때 발생. 손실 확대 방지 신호. 손절가 이하면 트리거.", "exit"),
        ("rsi_overbought", "RSI 과매수", "rsi_gte", "rsi_overbought", 120, "RSI 과매수 ({rsi:.1f} >= {threshold})", None, 2, "RSI(상대강도지수)가 과매수 기준(보통 70) 이상일 때. 단기 고점 도달 가능성. 매도 검토 신호.", "exit"),
        ("rsi_oversold", "RSI 과매도", "rsi_lte", "rsi_oversold", 120, "RSI 과매도 ({rsi:.1f} <= {threshold})", None, 3, "RSI가 과매도 기준(보통 30) 이하일 때. 단기 저점 도달 가능성. 반등 매수 검토 신호.", "entry"),
        ("rsi_oversold_intraday", "RSI 과매도 (5분봉)", "rsi_lte_intraday", "rsi_oversold_intraday", 60, "RSI 5분봉 과매도 ({rsi_intraday:.1f} <= {threshold})", None, 4, "5분봉 RSI(14기간)가 과매도 기준(종목별 30~35) 이하일 때. 단기 종목 당일 급락 감지 신호.", "entry"),
        ("golden_cross", "MA 골든크로스", "flag", "golden_cross", 1440, "골든크로스 (MA5 {ma5:,} > MA20 {ma20:,})", "golden_cross", 5, "단기 이동평균(MA5)이 장기 이동평균(MA20)을 아래에서 위로 돌파하는 순간. 중기 상승 추세 전환 신호.", "entry"),
        ("death_cross", "MA 데드크로스", "flag", "death_cross", 1440, "데드크로스 (MA5 {ma5:,} < MA20 {ma20:,})", "death_cross", 6, "단기 이동평균(MA5)이 장기 이동평균(MA20)을 위에서 아래로 돌파하는 순간. 중기 하락 추세 전환 신호.", "exit"),
        ("new_high_20d", "20일 신고가 돌파", "flag", "new_high_20d", 240, "20일 신고가 돌파 ({price:,}원)", "new_high_20d", 7, "현재가가 최근 20거래일 중 가장 높은 고가를 돌파. 강한 상승 모멘텀 신호. 신고가 돌파 매수 전략에 활용.", "both"),
        ("ma20_support_break", "MA20 하향 이탈", "flag", "ma20_support_break", 240, "MA20 하향 이탈 ({price:,}원 < MA20 {ma20:,}원)", "broke_below_ma20", 8, "전일까지 MA20 위에 있다가 오늘 MA20 아래로 이탈. 중기 지지선 붕괴 신호. 추가 하락 가능성.", "exit"),
        ("ma5_support_break", "MA5 하향 이탈", "flag", "ma5_support_break", 120, "MA5 하향 이탈 ({price:,}원 < MA5 {ma5:,}원)", "broke_below_ma5", 9, "전일까지 MA5 위에 있다가 오늘 MA5 아래로 이탈. 단기 지지선 붕괴 신호. 단기 조정 진입 가능성.", "both"),
        ("ma5_recovery", "MA5 상향 돌파 (회복)", "flag", "ma5_recovery", 120, "MA5 상향 돌파 ({price:,}원 > MA5 {ma5:,}원)", "broke_above_ma5", 10, "전일까지 MA5 아래에 있다가 오늘 MA5 위로 돌파. 단기 반등 회복 신호. 단기 매수 진입 검토.", "entry"),
        ("macd_golden_cross", "MACD 골든크로스", "flag", "macd_golden_cross", 1440, "MACD 골든크로스 (MACD {macd:.0f} > Signal {signal:.0f})", "macd_golden_cross", 11, "MACD 라인(EMA12-EMA26)이 시그널 라인(MACD의 EMA9)을 아래에서 위로 돌파. 상승 모멘텀 강화 신호.", "entry"),
        ("macd_death_cross", "MACD 데드크로스", "flag", "macd_death_cross", 1440, "MACD 데드크로스 (MACD {macd:.0f} < Signal {signal:.0f})", "macd_death_cross", 12, "MACD 라인이 시그널 라인을 위에서 아래로 돌파. 하락 모멘텀 강화 신호. 매도 검토.", "exit"),
        ("bollinger_upper_break", "볼린저 밴드 상단 돌파", "flag", "bollinger_upper_break", 120, "볼린저 상단 돌파 ({price:,}원 > 상단 {upper:,}원)", "bollinger_above_upper", 13, "현재가가 볼린저 상단(MA20 + 2σ)을 돌파. 강한 상승 돌파 또는 과열 신호. 추세 추종 or 과매수 주의.", "both"),
        ("bollinger_lower_break", "볼린저 밴드 하단 이탈", "flag", "bollinger_lower_break", 120, "볼린저 하단 이탈 ({price:,}원 < 하단 {lower:,}원)", "bollinger_below_lower", 14, "현재가가 볼린저 하단(MA20 - 2σ) 아래로 이탈. 급락 또는 과매도 신호. 반등 가능성 검토.", "entry"),
        ("rsi_oversold_add", "RSI 과매도 (추가매수)", "rsi_lte", "rsi_oversold", 120, "📉 물타기 타이밍 — RSI {rsi:.1f} 과매도 (기준 {threshold})", None, 15, "보유 중 종목의 RSI가 과매도 기준 이하로 하락. 물타기(평단 낮추기) 타이밍 검토.", "add"),
        ("bollinger_lower_break_add", "볼린저 하단 이탈 (추가매수)", "flag", "bollinger_lower_break", 120, "볼린저 하단 이탈 물타기 타이밍 ({price:,}원 < 하단 {lower:,}원)", "bollinger_below_lower", 16, "보유 중 종목이 볼린저 하단을 이탈. 과매도 구간 진입, 물타기 타이밍 검토.", "add"),
        ("ma5_recovery_add", "MA5 상향 돌파 (추가매수)", "flag", "ma5_recovery", 120, "MA5 회복 추가매수 타이밍 ({price:,}원 > MA5 {ma5:,}원)", "broke_above_ma5", 17, "보유 중 종목이 하락 후 MA5를 상향 돌파. 반등 확인 후 추가매수 타이밍 검토.", "add"),
        ("rsi_critical", "RSI 극단적 과매도 (심각)", "rsi_lte", "rsi_critical", 5, "RSI 극단적 과매도 경고 (RSI {rsi:.1f} <= {threshold}) — 즉각 검토 필요", None, 18, "RSI 극단 과매도 심각 경고. 홀드 후에도 독립 재알림.", "both"),
        ("bollinger_critical_below", "볼린저 하단 3% 이탈 (심각)", "flag", "bollinger_lower_break", 5, "볼린저 하단 3% 이상 급락 경고 ({price:,}원 << 하단 {lower:,}원) — 즉각 검토 필요", "bollinger_critical_below", 19, "볼린저 하단 3% 이상 이탈. 극단적 과매도. 홀드 후에도 독립 재알림.", "both"),
        ("volume_surge_ratio", "거래량 급증", "volume_gte", "volume_surge_ratio", 120, "거래량 급증 ({ratio:.1f}배)", None, 20, "오늘 거래량이 최근 20일 평균 거래량 대비 N배 이상일 때. 세력 개입, 뉴스/공시 등 이슈 발생 가능성 신호.", "both"),
        ("stochastic_golden_cross", "스토캐스틱 골든크로스", "flag", "stochastic_golden_cross", 1440, "스토캐스틱 골든크로스 (%K {stoch_k:.0f} > %D {stoch_d:.0f})", "stochastic_golden_cross", 21, "%K가 %D를 상향 돌파 (과매도 구간에서 더 강력)", "entry"),
        ("stochastic_death_cross", "스토캐스틱 데드크로스", "flag", "stochastic_death_cross", 1440, "스토캐스틱 데드크로스 (%K {stoch_k:.0f} < %D {stoch_d:.0f})", "stochastic_death_cross", 22, "%K가 %D를 하향 돌파 (과매수 구간에서 더 강력)", "exit"),
        ("cci_oversold", "CCI 과매도", "cci_lte", "cci_oversold", 120, "CCI 과매도 ({cci:.0f} <= {threshold})", None, 23, "CCI가 -100 이하로 진입 (과매도 → 반등 가능)", "entry"),
        ("cci_overbought", "CCI 과매수", "cci_gte", "cci_overbought", 120, "CCI 과매수 ({cci:.0f} >= {threshold})", None, 24, "CCI가 +100 이상으로 진입 (과매수 → 조정 가능)", "exit"),
        ("ichimoku_golden_cross", "일목 전환선 골든크로스", "flag", "ichimoku_golden_cross", 1440, "일목균형표 전환선 골든크로스", "ichimoku_tenkan_golden", 25, "전환선(9일)이 기준선(26일) 상향 돌파 → 매수 신호", "entry"),
        ("ichimoku_death_cross", "일목 전환선 데드크로스", "flag", "ichimoku_death_cross", 1440, "일목균형표 전환선 데드크로스", "ichimoku_tenkan_dead", 26, "전환선(9일)이 기준선(26일) 하향 돌파 → 매도 신호", "exit"),
        ("ichimoku_cloud_breakout", "일목 구름대 돌파", "flag", "ichimoku_cloud_breakout", 1440, "일목균형표 구름대 돌파 ({price:,}원)", "ichimoku_above_cloud", 27, "가격이 구름대 위로 돌파 → 상승 추세 전환", "entry"),
        ("ichimoku_cloud_breakdown", "일목 구름대 이탈", "flag", "ichimoku_cloud_breakdown", 1440, "일목균형표 구름대 이탈 ({price:,}원)", "ichimoku_below_cloud", 28, "가격이 구름대 아래로 이탈 → 하락 추세 전환", "exit"),
    ]
    with get_conn() as conn:
        if conn.execute("SELECT COUNT(*) FROM conditions_def").fetchone()[0] == 0:
            for row in SEED:
                conn.execute(
                    "INSERT INTO conditions_def "
                    "(id, name, evaluator, param, cooldown_minutes, message, chart_field, sort_order, description, signal_type) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)", row
                )
            conn.commit()


# ── watchlist ──────────────────────────────────────────────────────────────

_WL_CONDITION_FIELDS = {
    "rsi_oversold", "rsi_overbought", "rsi_oversold_intraday", "rsi_critical",
    "volume_surge_ratio", "cci_oversold", "cci_overbought",
    "golden_cross", "death_cross", "ma20_support_break", "ma5_support_break",
    "ma5_recovery", "new_high_20d", "macd_golden_cross", "macd_death_cross",
    "bollinger_upper_break", "bollinger_lower_break", "bollinger_critical_below",
    "stochastic_golden_cross", "stochastic_death_cross",
    "ichimoku_golden_cross", "ichimoku_death_cross",
    "ichimoku_cloud_breakout", "ichimoku_cloud_breakdown",
    "strategy_note",
}
_WL_FLAG_FIELDS = {
    "golden_cross", "death_cross", "ma20_support_break", "ma5_support_break",
    "ma5_recovery", "new_high_20d", "macd_golden_cross", "macd_death_cross",
    "bollinger_upper_break", "bollinger_lower_break", "bollinger_critical_below",
    "stochastic_golden_cross", "stochastic_death_cross",
    "ichimoku_golden_cross", "ichimoku_death_cross",
    "ichimoku_cloud_breakout", "ichimoku_cloud_breakdown",
}


def repair_watchlist_conditions(stock_code: str | None = None) -> int:
    """Public helper: repair legacy watchlist rows missing every monitoring condition."""
    with get_conn() as conn:
        repaired = _repair_legacy_watchlist_conditions(conn, stock_code=stock_code)
        conn.commit()
    return repaired


def get_watchlist() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM watchlist ORDER BY rowid").fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["enabled"] = bool(d["enabled"])
        # flag 필드: 0/1 → bool 변환
        for f in _WL_FLAG_FIELDS:
            if f in d and d[f] is not None:
                d[f] = bool(d[f])
        # conditions 컬럼은 하위호환용 — 비어있으면 제거
        d.pop("conditions", None)
        result.append(d)
    return result


def upsert_stock(code: str, name: str, enabled: bool, conditions: dict):
    """watchlist 종목 추가/갱신. conditions dict의 필드를 개별 컬럼으로 저장."""
    now = _now_kst().strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO watchlist (code, name, enabled, created_at) VALUES (?,?,?,?) "
            "ON CONFLICT(code) DO UPDATE SET name=excluded.name, enabled=excluded.enabled, "
            "created_at=CASE WHEN watchlist.created_at = '' OR watchlist.created_at IS NULL THEN excluded.created_at ELSE watchlist.created_at END",
            (code, name, int(enabled), now)
        )
        # 개별 컬럼 업데이트
        _updatable = _WL_CONDITION_FIELDS | {"horizon"}
        for field, value in conditions.items():
            if field not in _updatable:
                continue
            if field in _WL_FLAG_FIELDS:
                value = 1 if value else 0
            conn.execute(f"UPDATE watchlist SET {field} = ? WHERE code = ?", (value, code))
        conn.commit()


def update_stock_field(code: str, field: str, value) -> bool:
    """종목 필드 수정 — 모든 필드가 개별 컬럼."""
    _all_fields = {"name", "enabled", "horizon"} | _WL_CONDITION_FIELDS
    if field not in _all_fields:
        return False
    if field in _WL_FLAG_FIELDS:
        value = 1 if value else 0
    with get_conn() as conn:
        cur = conn.execute(f"UPDATE watchlist SET {field} = ? WHERE code = ?", (value, code))
        conn.commit()
    return cur.rowcount > 0


def delete_stock(code: str) -> bool:
    with get_conn() as conn:
        result = conn.execute("DELETE FROM watchlist WHERE code = ?", (code,))
        conn.commit()
    return result.rowcount > 0


# ── positions (매수 후 포지션 관리) ────────────────────────────────────────

def get_positions() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM positions ORDER BY rowid").fetchall()
    return [dict(r) for r in rows]


def get_position(stock_code: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM positions WHERE stock_code = ?", (stock_code,)).fetchone()
    return dict(row) if row else None


def upsert_position(
    stock_code: str, stock_name: str, avg_price: int, quantity: int,
    target_price: int = 0, stop_loss_price: int = 0,
    add_buy_price: int = 0, mid_sell_price: int = 0,
    **kwargs,
):
    """포지션 생성/갱신. kwargs로 rsi_oversold_add, bollinger_lower_break_add, ma5_recovery_add, strategy_note 전달 가능."""
    now = _now_kst().strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO positions
                (stock_code, stock_name, avg_price, quantity,
                 target_price, stop_loss_price, add_buy_price, mid_sell_price,
                 rsi_oversold_add, bollinger_lower_break_add, ma5_recovery_add,
                 strategy_note, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(stock_code) DO UPDATE SET
                stock_name=excluded.stock_name, avg_price=excluded.avg_price,
                quantity=excluded.quantity, target_price=excluded.target_price,
                stop_loss_price=excluded.stop_loss_price, add_buy_price=excluded.add_buy_price,
                mid_sell_price=excluded.mid_sell_price,
                rsi_oversold_add=excluded.rsi_oversold_add,
                bollinger_lower_break_add=excluded.bollinger_lower_break_add,
                ma5_recovery_add=excluded.ma5_recovery_add,
                strategy_note=excluded.strategy_note,
                updated_at=excluded.updated_at
            """,
            (
                stock_code, stock_name, avg_price, quantity,
                target_price, stop_loss_price, add_buy_price, mid_sell_price,
                kwargs.get("rsi_oversold_add"),
                kwargs.get("bollinger_lower_break_add"),
                kwargs.get("ma5_recovery_add"),
                kwargs.get("strategy_note", ""),
                now, now,
            ),
        )
        conn.commit()


def update_position_field(stock_code: str, field: str, value) -> bool:
    """포지션 단일 필드 수정."""
    allowed = {
        "target_price", "stop_loss_price", "add_buy_price", "mid_sell_price",
        "rsi_oversold_add", "bollinger_lower_break_add", "ma5_recovery_add",
        "strategy_note", "avg_price", "quantity",
    }
    if field not in allowed:
        return False
    now = _now_kst().strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE positions SET {field} = ?, updated_at = ? WHERE stock_code = ?",
            (value, now, stock_code),
        )
        conn.commit()
    return cur.rowcount > 0


def delete_position(stock_code: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM positions WHERE stock_code = ?", (stock_code,))
        conn.commit()
    return cur.rowcount > 0


def create_position_from_trade(stock_code: str, stock_name: str, avg_price: int, quantity: int) -> bool:
    """매수 체결 후 포지션 자동 생성. watchlist 조건에서 add 관련 값을 복사."""
    with get_conn() as conn:
        existing = conn.execute("SELECT stock_code FROM positions WHERE stock_code = ?", (stock_code,)).fetchone()
        if existing:
            return False  # 이미 존재

        # watchlist에서 strategy_note 복사
        wl_row = conn.execute("SELECT strategy_note FROM watchlist WHERE code = ?", (stock_code,)).fetchone()
        note = wl_row["strategy_note"] if wl_row and wl_row["strategy_note"] else ""

        # 평단가 기준 기본값 계산
        target_price = int(avg_price * 1.15) if avg_price else 0
        stop_loss_price = int(avg_price * 0.93) if avg_price else 0
        add_buy_price = int(avg_price * 0.92) if avg_price else 0

    upsert_position(
        stock_code, stock_name, avg_price, quantity,
        target_price=target_price, stop_loss_price=stop_loss_price,
        add_buy_price=add_buy_price,
        strategy_note=note,
    )
    return True


# ── conditions_def ─────────────────────────────────────────────────────────

def get_conditions() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM conditions_def ORDER BY sort_order, rowid"
        ).fetchall()
    return [dict(r) for r in rows]


def add_condition(cond: dict) -> bool:
    with get_conn() as conn:
        try:
            next_order = conn.execute(
                "SELECT COALESCE(MAX(sort_order)+1, 0) FROM conditions_def"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO conditions_def "
                "(id, name, evaluator, param, cooldown_minutes, message, chart_field, sort_order, description, signal_type) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (cond["id"], cond["name"], cond["evaluator"], cond["param"],
                 cond.get("cooldown_minutes", 60), cond["message"],
                 cond.get("chart_field"), next_order, cond.get("description", ""),
                 cond.get("signal_type", "both"))
            )
            conn.commit()
            return True
        except Exception:
            return False


def update_condition(cond_id: str, fields: dict) -> bool:
    """조건 정의 필드 수정. fields: 변경할 컬럼명→값 딕셔너리."""
    allowed = {"name", "cooldown_minutes", "message", "description", "signal_type", "chart_field"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM conditions_def WHERE id = ?", (cond_id,)).fetchone()
        if not row:
            return False
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(
            f"UPDATE conditions_def SET {set_clause} WHERE id = ?",
            (*updates.values(), cond_id)
        )
        conn.commit()
    return True


def remove_condition(cond_id: str) -> bool:
    with get_conn() as conn:
        result = conn.execute("DELETE FROM conditions_def WHERE id = ?", (cond_id,))
        conn.commit()
    return result.rowcount > 0


# ── 포트폴리오 ─────────────────────────────────────────────────────────────

def upsert_portfolio(holdings: list[dict]):
    """API 응답으로 포트폴리오 전체 갱신 (UPSERT)"""
    now = _now_kst().strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        # 기존 전체 삭제 후 재삽입 (잔량 0인 종목 자동 제거)
        conn.execute("DELETE FROM portfolio")
        for h in holdings:
            code = str(h.get("stk_cd") or "").replace("A", "").strip()
            if not code:
                continue
            conn.execute(
                """
                INSERT OR REPLACE INTO portfolio
                    (stock_code, stock_name, quantity, avg_price, current_price,
                     eval_amount, profit_loss, profit_rate, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    code,
                    h.get("stk_nm", ""),
                    _p(h.get("rmnd_qty")),
                    _p(h.get("pur_pric")),
                    _p(h.get("cur_prc")),
                    _p(h.get("evlt_amt")),
                    _p(h.get("evltv_prft")),
                    _f(h.get("prft_rt")),
                    now,
                ),
            )
        conn.commit()


def get_portfolio() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM portfolio ORDER BY eval_amount DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_portfolio_updated_at() -> str | None:
    with get_conn() as conn:
        row = conn.execute("SELECT updated_at FROM portfolio LIMIT 1").fetchone()
    return row["updated_at"] if row else None


# ── 매매 내역 ──────────────────────────────────────────────────────────────

def upsert_trades(trades: list[dict]):
    """매매 내역 upsert.
    기존 임시 기록(price=0, fee/tax=0)을 실제 체결 데이터로 갱신한다.
    """
    with get_conn() as conn:
        for t in trades:
            trade_id = str(t.get("trde_no") or t.get("ord_no") or "")
            executed_at = str(t.get("trde_dt") or "")
            if len(executed_at) == 8:
                executed_at = f"{executed_at[:4]}-{executed_at[4:6]}-{executed_at[6:]}"
            code = str(t.get("stk_cd") or "").replace("A", "").strip()
            if not trade_id or not code:
                continue
            io_tp = str(t.get("io_tp") or "")
            side = "매수" if io_tp == "2" else "매도"
            conn.execute(
                """
                INSERT INTO trades
                    (trade_id, executed_at, stock_code, stock_name, side,
                     quantity, price, amount, fee, tax)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_id) DO UPDATE SET
                    executed_at = excluded.executed_at,
                    stock_code = excluded.stock_code,
                    stock_name = excluded.stock_name,
                    side = excluded.side,
                    quantity = CASE WHEN excluded.quantity > 0 THEN excluded.quantity ELSE trades.quantity END,
                    price = CASE WHEN excluded.price > 0 THEN excluded.price ELSE trades.price END,
                    amount = CASE WHEN excluded.amount > 0 THEN excluded.amount ELSE trades.amount END,
                    fee = CASE WHEN excluded.fee > 0 THEN excluded.fee ELSE trades.fee END,
                    tax = CASE WHEN excluded.tax > 0 THEN excluded.tax ELSE trades.tax END
                """,
                (
                    trade_id,
                    executed_at,
                    code,
                    t.get("stk_nm", ""),
                    side,
                    _p(t.get("trde_qty_jwa_cnt")),
                    _p(t.get("trde_unit")),
                    _p(t.get("trde_amt")),
                    _p(t.get("fee")),
                    _p(t.get("tax")),
                ),
            )
        conn.commit()


def backfill_trades_from_executions(executions: list[dict]) -> dict:
    """kt00007 체결내역으로 trades 가격/수량/금액 보정.

    우선순위:
    1) order_no == trade_id 직접 매칭
    2) fallback: 당일 동일 종목/매매구분 + price<=0 인 최근 행 매칭
    """
    from collections import defaultdict

    grouped: dict[tuple[str, str, str], dict] = defaultdict(lambda: {
        "stock_name": "",
        "qty_sum": 0,
        "amt_sum": 0,
    })

    for e in executions or []:
        code = str(e.get("stock_code") or "").replace("A", "", 1).strip()
        side = str(e.get("side") or "").strip()
        order_no = str(e.get("order_no") or "").strip()
        qty = _p(e.get("quantity"))
        price = _p(e.get("price"))
        if not code or not side or qty <= 0 or price <= 0:
            continue
        key = (order_no, code, side)
        grouped[key]["stock_name"] = str(e.get("stock_name") or "").strip() or grouped[key]["stock_name"]
        grouped[key]["qty_sum"] += qty
        grouped[key]["amt_sum"] += qty * price

    if not grouped:
        return {"updated": 0, "matched_by_order_no": 0, "matched_by_fallback": 0}

    today = _now_kst().strftime("%Y-%m-%d")
    updated = 0
    matched_by_order = 0
    matched_by_fallback = 0

    with get_conn() as conn:
        for (order_no, code, side), agg in grouped.items():
            qty = int(agg["qty_sum"])
            amt = int(agg["amt_sum"])
            if qty <= 0 or amt <= 0:
                continue
            avg_price = int(round(amt / qty))
            stock_name = agg["stock_name"]

            # 1) order_no 직접 매칭
            if order_no:
                cur = conn.execute(
                    """UPDATE trades
                       SET stock_name = COALESCE(NULLIF(?, ''), stock_name),
                           quantity = ?,
                           price = ?,
                           amount = ?
                       WHERE trade_id = ?""",
                    (stock_name, qty, avg_price, amt, order_no),
                )
                if cur.rowcount > 0:
                    updated += cur.rowcount
                    matched_by_order += cur.rowcount
                    continue

            # 2) fallback: 당일 임시행(price<=0) 보정
            row = conn.execute(
                """SELECT trade_id FROM trades
                   WHERE stock_code = ?
                     AND side = ?
                     AND executed_at = ?
                     AND (price IS NULL OR price <= 0)
                   ORDER BY rowid DESC
                   LIMIT 1""",
                (code, side, today),
            ).fetchone()
            if not row:
                continue
            cur = conn.execute(
                """UPDATE trades
                   SET stock_name = COALESCE(NULLIF(?, ''), stock_name),
                       quantity = ?,
                       price = ?,
                       amount = ?
                   WHERE trade_id = ?""",
                (stock_name, qty, avg_price, amt, row["trade_id"]),
            )
            if cur.rowcount > 0:
                updated += cur.rowcount
                matched_by_fallback += cur.rowcount

        conn.commit()

    return {
        "updated": updated,
        "matched_by_order_no": matched_by_order,
        "matched_by_fallback": matched_by_fallback,
    }


def upsert_trades_from_executions(executions: list[dict], executed_at: str | None = None) -> int:
    """kt00007 체결내역을 trades로 누적 저장/갱신.

    - order_no 기준으로 부분체결을 합산하여 1건으로 저장
    - fee/tax는 0으로 유지 (정산 API 반영 전까지 미확정)
    """
    from collections import defaultdict

    if not executions:
        return 0

    default_day = _now_kst().strftime("%Y-%m-%d")
    if executed_at:
        dt = str(executed_at).replace("-", "").strip()
        if len(dt) == 8 and dt.isdigit():
            default_day = f"{dt[:4]}-{dt[4:6]}-{dt[6:]}"

    def _norm_day(raw) -> str:
        s = str(raw or "").strip().replace("-", "")
        if len(s) == 8 and s.isdigit():
            return f"{s[:4]}-{s[4:6]}-{s[6:]}"
        return ""

    grouped: dict[tuple[str, str, str, str], dict] = defaultdict(lambda: {
        "stock_name": "",
        "qty_sum": 0,
        "amt_sum": 0,
    })
    fallback_idx = 0

    for e in executions:
        code = str(e.get("stock_code") or "").replace("A", "", 1).strip()
        side = str(e.get("side") or "").strip() or "매수"
        order_no = str(e.get("order_no") or "").strip()
        qty = _p(e.get("quantity"))
        price = _p(e.get("price"))
        if not code or qty <= 0 or price <= 0:
            continue
        executed_day = _norm_day(e.get("executed_date")) or _norm_day(e.get("executed_at")) or default_day
        if not order_no:
            fallback_idx += 1
            order_no = f"EXE-{executed_day}-{code}-{side}-{fallback_idx}"
        key = (order_no, code, side, executed_day)
        grouped[key]["stock_name"] = str(e.get("stock_name") or "").strip() or grouped[key]["stock_name"]
        grouped[key]["qty_sum"] += qty
        grouped[key]["amt_sum"] += qty * price

    if not grouped:
        return 0

    upserted = 0
    with get_conn() as conn:
        for (trade_id, code, side, executed_day), agg in grouped.items():
            qty = int(agg["qty_sum"])
            amt = int(agg["amt_sum"])
            if qty <= 0 or amt <= 0:
                continue
            avg_price = int(round(amt / qty))
            stock_name = agg["stock_name"]
            cur = conn.execute(
                """
                INSERT INTO trades
                    (trade_id, executed_at, stock_code, stock_name, side,
                     quantity, price, amount, fee, tax)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0)
                ON CONFLICT(trade_id) DO UPDATE SET
                    executed_at = excluded.executed_at,
                    stock_code = excluded.stock_code,
                    stock_name = excluded.stock_name,
                    side = excluded.side,
                    quantity = CASE WHEN excluded.quantity > 0 THEN excluded.quantity ELSE trades.quantity END,
                    price = CASE WHEN excluded.price > 0 THEN excluded.price ELSE trades.price END,
                    amount = CASE WHEN excluded.amount > 0 THEN excluded.amount ELSE trades.amount END
                """,
                (trade_id, executed_day, code, stock_name, side, qty, avg_price, amt),
            )
            if cur.rowcount > 0:
                upserted += cur.rowcount
        conn.commit()
    return upserted


def insert_trade_direct(
    trade_id: str,
    stock_code: str,
    stock_name: str,
    side: str,
    quantity: int,
    price: int,
    executed_at: str = "",
) -> None:
    """주문 체결 직후 trades 테이블에 즉시 기록 (실거래/모의투자 공통).
    trade_id = ord_no, price = 지정가(시장가는 0), fee/tax = 0으로 기록 후 수동 수정 가능."""
    from datetime import datetime

    if not executed_at:
        executed_at = datetime.now().strftime("%Y-%m-%d")
    amount = price * quantity
    with get_conn() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO trades
                (trade_id, executed_at, stock_code, stock_name, side,
                 quantity, price, amount, fee, tax)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0)
            """,
            (trade_id, executed_at, stock_code, stock_name, side, quantity, price, amount),
        )
        conn.commit()


def get_trades(limit: int = 50) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY executed_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def update_trade_result(trade_id: str, result_pct: float, period: str = "3d"):
    """실거래 1일/3일/5일 성과 업데이트."""
    col = {"1d": "result_1d", "3d": "result_3d", "5d": "result_5d"}.get(period)
    if not col:
        raise ValueError(f"지원하지 않는 period: {period}")
    with get_conn() as conn:
        conn.execute(f"UPDATE trades SET {col} = ? WHERE trade_id = ?", (result_pct, trade_id))
        conn.commit()


def get_recent_trades_for_stock(stock_code: str, days: int = 3) -> list[dict]:
    """특정 종목의 최근 N일 매매 이력 조회 (최신순)"""
    from datetime import timedelta
    cutoff = (_now_kst() - timedelta(days=days)).strftime("%Y-%m-%d")
    with get_conn() as conn:
        try:
            rows = conn.execute(
                "SELECT * FROM trades WHERE stock_code = ? AND executed_at >= ? ORDER BY executed_at DESC",
                (stock_code, cutoff),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []


def save_realized_pnl_snapshot(
    scope: str,
    source_api: str,
    realized_pnl: float | None = None,
    fee: float | None = None,
    tax: float | None = None,
    start_dt: str | None = None,
    end_dt: str | None = None,
    raw: dict | None = None,
) -> int:
    """실현손익 API 스냅샷 저장. 반환: snapshot id."""
    now = _now_kst().strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO realized_pnl_snapshots
               (created_at, scope, start_dt, end_dt, realized_pnl, fee, tax, source_api, raw_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                now,
                scope,
                start_dt,
                end_dt,
                realized_pnl,
                fee,
                tax,
                source_api,
                json.dumps(raw, ensure_ascii=False) if raw is not None else None,
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)


def get_recent_realized_pnl_snapshots(limit: int = 20, scope: str = "") -> list[dict]:
    """최근 실현손익 스냅샷 조회."""
    with get_conn() as conn:
        if scope:
            rows = conn.execute(
                """SELECT * FROM realized_pnl_snapshots
                   WHERE scope = ?
                   ORDER BY created_at DESC
                   LIMIT ?""",
                (scope, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM realized_pnl_snapshots
                   ORDER BY created_at DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]


# ── 전략 노트 ──────────────────────────────────────────────────────────────

def save_strategy_note(category: str, summary: str, detail: str = ""):
    """전략 결정 기록 저장
    category: 'trade' | 'watchlist' | 'general'
    """
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS strategy_notes (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at  TEXT NOT NULL,
                category    TEXT NOT NULL,
                summary     TEXT NOT NULL,
                detail      TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO strategy_notes (created_at, category, summary, detail) VALUES (?, ?, ?, ?)",
            (_now_kst().strftime("%Y-%m-%d %H:%M:%S"), category, summary, detail),
        )
        conn.commit()


def get_strategy_notes(limit: int = 20) -> list[dict]:
    with get_conn() as conn:
        try:
            rows = conn.execute(
                "SELECT * FROM strategy_notes ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []


def get_strategy_note(note_id: int) -> dict | None:
    with get_conn() as conn:
        try:
            row = conn.execute("SELECT * FROM strategy_notes WHERE id = ?", (note_id,)).fetchone()
            return dict(row) if row else None
        except Exception:
            return None


def update_strategy_note(note_id: int, summary: str = "", detail: str = "", category: str = "") -> bool:
    """전략 노트 수정. 입력된 항목만 업데이트."""
    fields = {}
    if summary:
        fields["summary"] = summary
    if detail:
        fields["detail"] = detail
    if category:
        fields["category"] = category
    if not fields:
        return False
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM strategy_notes WHERE id = ?", (note_id,)).fetchone()
        if not row:
            return False
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(
            f"UPDATE strategy_notes SET {set_clause} WHERE id = ?",
            (*fields.values(), note_id)
        )
        conn.commit()
    return True


def delete_strategy_note(note_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM strategy_notes WHERE id = ?", (note_id,))
        return cur.rowcount > 0


def get_strategy_notes_before(before_date: str) -> list[dict]:
    with get_conn() as conn:
        try:
            rows = conn.execute(
                "SELECT * FROM strategy_notes WHERE created_at < ? ORDER BY created_at ASC",
                (before_date,)
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []


def delete_strategy_notes_before(before_date: str) -> int:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM strategy_notes WHERE created_at < ?", (before_date,))
        return cur.rowcount


def delete_all_strategy_notes() -> int:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM strategy_notes")
        return cur.rowcount


def get_cooldown(key: str) -> datetime | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT last_sent_at, next_allowed_at FROM cooldowns WHERE key = ?", (key,)
        ).fetchone()
    if row:
        next_allowed_at = row["next_allowed_at"]
        if next_allowed_at:
            return datetime.strptime(next_allowed_at, "%Y-%m-%d %H:%M:%S")
        legacy_last = row["last_sent_at"]
        if legacy_last:
            return _infer_legacy_cooldown_until(key, datetime.strptime(legacy_last, "%Y-%m-%d %H:%M:%S"))
    return None


def get_cooldown_record(key: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT key, last_sent_at, next_allowed_at FROM cooldowns WHERE key = ?", (key,)
        ).fetchone()
    if not row:
        return None
    return {
        "key": row["key"],
        "last_sent_at": row["last_sent_at"],
        "next_allowed_at": row["next_allowed_at"],
    }


def set_cooldown(key: str, cooldown_minutes: int = 0, sent_at: datetime | None = None):
    sent_dt = sent_at or _now_kst()
    next_dt = sent_dt + timedelta(minutes=max(0, cooldown_minutes))
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO cooldowns (key, last_sent_at, next_allowed_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET last_sent_at = excluded.last_sent_at, next_allowed_at = excluded.next_allowed_at",
            (
                key,
                sent_dt.strftime("%Y-%m-%d %H:%M:%S"),
                next_dt.strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        conn.commit()


def reset_all_cooldowns() -> int:
    """모든 쿨다운 초기화. 장 시작 시 호출. 반환: 삭제된 항목 수."""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM cooldowns")
        conn.commit()
    return cur.rowcount


def reset_cooldowns_for_stock(stock_code: str) -> int:
    """매매 체결 후 해당 종목의 쿨다운 전체 삭제. 반환: 삭제된 항목 수."""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM cooldowns WHERE key LIKE ?", (f"{stock_code}:%",))
        conn.commit()
    return cur.rowcount


def _infer_legacy_cooldown_until(key: str, legacy_last: datetime) -> datetime:
    if key.startswith("intraday_scan:"):
        return legacy_last + timedelta(hours=12)
    if key.startswith("dip_buy:"):
        return legacy_last + timedelta(hours=2)
    if key.endswith(":inactive_alert") or key.endswith(":removal_check"):
        return legacy_last + timedelta(days=7)

    cond_id = key.split(":", 1)[1] if ":" in key else ""
    cond_map = {c["id"]: c.get("cooldown_minutes", 60) for c in get_conditions()}
    return legacy_last + timedelta(minutes=cond_map.get(cond_id, 60))


def set_add_cooldown_after_trade(stock_code: str, suppress_minutes: int = 60) -> int:
    """매수 체결 후 add/both 조건 신호를 suppress_minutes 동안 억제.
    → 해당 조건의 next_allowed_at이 now + suppress_minutes가 되도록 설정.
    반환: 억제 설정된 조건 수.
    """
    cond_map = {c["id"]: c.get("cooldown_minutes", 60) for c in get_conditions()
                if c.get("signal_type") in ("add", "both")}
    now = _now_kst()
    count = 0
    with get_conn() as conn:
        for cond_id, cooldown_minutes in cond_map.items():
            key = f"{stock_code}:{cond_id}"
            conn.execute(
                "INSERT INTO cooldowns (key, last_sent_at, next_allowed_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET last_sent_at = excluded.last_sent_at, next_allowed_at = excluded.next_allowed_at",
                (
                    key,
                    now.strftime("%Y-%m-%d %H:%M:%S"),
                    (now + timedelta(minutes=suppress_minutes)).strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            count += 1
        conn.commit()
    return count


def shorten_cooldowns_for_stock(stock_code: str, ratio: float = 0.25, min_minutes: int = 30) -> int:
    """홀드 결정 후 해당 종목의 남은 쿨다운을 원래의 ratio 비율로 단축.
    예: 원래 120분 쿨다운 → 30분 후 재알림 (ratio=0.25, min=30)
    반환: 단축된 조건 수
    """
    cond_map = {c["id"]: c["cooldown_minutes"] for c in get_conditions()}
    now = _now_kst()
    count = 0
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT key, last_sent_at, next_allowed_at FROM cooldowns WHERE key LIKE ?",
            (f"{stock_code}:%",)
        ).fetchall()
        for row in rows:
            key = row["key"]
            cond_id = key.split(":", 1)[1]
            original_minutes = cond_map.get(cond_id, 60)
            short_minutes = max(min_minutes, int(original_minutes * ratio))
            current_last = row["last_sent_at"] or now.strftime("%Y-%m-%d %H:%M:%S")
            new_next = now + timedelta(minutes=short_minutes)
            conn.execute(
                "UPDATE cooldowns SET last_sent_at = ?, next_allowed_at = ? WHERE key = ?",
                (current_last, new_next.strftime("%Y-%m-%d %H:%M:%S"), key)
            )
            count += 1
        conn.commit()
    return count


def _extract_verdict(claude_opinion: str | None) -> str | None:
    """AI 판단 텍스트에서 [매수]/[매도]/[홀드] 추출."""
    if not claude_opinion:
        return None
    first_line = claude_opinion.strip().splitlines()[0] if claude_opinion.strip() else ""
    # 정확한 패턴: [매수], [매도], [홀드]
    for v in ["매수", "매도", "홀드"]:
        if f"[{v}]" in first_line:
            return v
    # 복합 패턴: [추가매수(매수)], [물타기(매수)] 등
    for v in ["매수", "매도", "홀드"]:
        if "[" in first_line and f"({v})" in first_line:
            return v
    # 대괄호 없이 시작하는 패턴: "매수 — ...", "매도\n..."
    for v in ["매수", "매도", "홀드"]:
        if first_line.startswith(v):
            return v
    # 첫 단어가 판정인 경우
    words = [w.strip() for w in first_line.split() if w.strip()]
    if words and words[0] in ["매수", "매도", "홀드"]:
        return words[0]
    return None


def extract_verdict(claude_opinion: str | None) -> str | None:
    """외부에서 AI 판정 텍스트 파싱 시 사용 (public wrapper)."""
    return _extract_verdict(claude_opinion)


def _build_indicator_snapshot(signal) -> str | None:
    """신호 시점의 전체 지표 스냅샷을 JSON으로 생성."""
    chart = getattr(signal, "chart", None)
    if not chart:
        return json.dumps({"rsi": signal.rsi, "volume_ratio": signal.volume_ratio})

    snapshot = {
        "rsi": signal.rsi,
        "volume_ratio": signal.volume_ratio,
        "ma5": chart.ma5,
        "ma20": chart.ma20,
        "trend": chart.trend,
        "above_ma5": chart.above_ma5,
        "above_ma20": chart.above_ma20,
        "price_change_5d": chart.price_change_5d,
        "macd_line": chart.macd_line,
        "macd_signal": chart.macd_signal,
        "bollinger_upper": chart.bollinger_upper,
        "bollinger_lower": chart.bollinger_lower,
        "stochastic_k": chart.stochastic_k,
        "stochastic_d": chart.stochastic_d,
        "cci": chart.cci,
        "ichimoku_tenkan": chart.ichimoku_tenkan,
        "ichimoku_kijun": chart.ichimoku_kijun,
        "ichimoku_above_cloud": chart.ichimoku_above_cloud,
        "ichimoku_cloud_thickness": chart.ichimoku_cloud_thickness,
        "obv_trend": chart.obv_trend,
        "rsi_divergence": chart.rsi_divergence,
        "macd_divergence": chart.macd_divergence,
        "volume_spread": chart.volume_spread,
        "volume_price_trend": chart.volume_price_trend,
        "support_level": chart.support_level,
        "resistance_level": chart.resistance_level,
    }
    # None 값 제거 (용량 절감)
    return json.dumps({k: v for k, v in snapshot.items() if v is not None}, ensure_ascii=False)


def build_indicator_snapshot(signal) -> str | None:
    """외부(main.py 등)에서 indicator_snapshot을 빌드할 때 사용."""
    return _build_indicator_snapshot(signal)


def save_signal(
    signal,
    claude_opinion: str | None = None,
    in_portfolio: bool = False,
    dart_summary: str | None = None,
    news_summary: str | None = None,
    market_snapshot: str | None = None,
    portfolio_snapshot: str | None = None,
) -> int:
    """신호 저장 후 signal_id 반환. 지표 스냅샷 + verdict 자동 추출."""
    verdict = _extract_verdict(claude_opinion)
    indicator_snapshot = _build_indicator_snapshot(signal)

    chart = getattr(signal, "chart", None)
    chart_patterns = None
    if chart:
        patterns = (chart.candle_patterns or []) + (chart.chart_patterns or [])
        if patterns:
            chart_patterns = json.dumps(patterns, ensure_ascii=False)

    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO signals
                (created_at, stock_code, stock_name, current_price,
                 triggered_conditions, rsi, volume_ratio, claude_opinion, in_portfolio, signal_type,
                 verdict, indicator_snapshot, dart_summary, chart_patterns,
                 news_summary, market_snapshot, portfolio_snapshot)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _now_kst().strftime("%Y-%m-%d %H:%M:%S"),
                signal.stock_code,
                signal.stock_name,
                signal.current_price,
                ", ".join(signal.triggered_conditions),
                signal.rsi,
                signal.volume_ratio,
                claude_opinion,
                int(in_portfolio),
                getattr(signal, "signal_type", None) or None,
                verdict,
                indicator_snapshot,
                dart_summary,
                chart_patterns,
                news_summary,
                market_snapshot,
                portfolio_snapshot,
            ),
        )
        conn.commit()
        signal_id = cur.lastrowid

    return signal_id


def update_signal_agent_trace(
    signal_id: int,
    tool_sequence: list[str],
    reasoning_chain: list[str],
) -> bool:
    """Agent가 사용한 도구 순서 + GPT 중간 추론을 signals 테이블에 저장."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE signals SET tool_sequence = ?, reasoning_chain = ? WHERE id = ?",
            (
                json.dumps(tool_sequence, ensure_ascii=False),
                json.dumps(reasoning_chain, ensure_ascii=False),
                signal_id,
            ),
        )
        conn.commit()
        return cur.rowcount > 0


def save_agent_action_log(
    signal_id: int,
    stock_code: str,
    stock_name: str,
    signal_type: str | None,
    tool_sequence: list[str],
    reasoning_chain: list[str],
    final_opinion: str | None = None,
) -> int:
    """Persist one agent trace record into the archive table."""
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO agent_action_logs
                (created_at, signal_id, stock_code, stock_name, signal_type,
                 tool_sequence, reasoning_chain, final_opinion)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _now_kst().strftime("%Y-%m-%d %H:%M:%S"),
                signal_id,
                stock_code,
                stock_name,
                signal_type or None,
                json.dumps(tool_sequence or [], ensure_ascii=False),
                json.dumps(reasoning_chain or [], ensure_ascii=False),
                final_opinion,
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)


def purge_agent_action_logs(days: int = 7) -> int:
    """Delete archived agent traces older than the retention window."""
    keep_days = max(1, int(days or 7))
    cutoff = (_now_kst() - timedelta(days=keep_days)).strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM agent_action_logs WHERE created_at < ?", (cutoff,))
        conn.commit()
        return int(cur.rowcount or 0)


def update_signal_result(signal_id: int, result_pct: float, period: str = "3d") -> bool:
    """신호 발생 후 N일 결과 수익률 업데이트.
    period: '1d', '3d', '5d', '10d'
    """
    col_map = {"1d": "result_1d", "3d": "result_pct", "5d": "result_5d", "10d": "result_10d"}
    col = col_map.get(period, "result_pct")
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE signals SET {col} = ? WHERE id = ?", (result_pct, signal_id)
        )
        conn.commit()
    return cur.rowcount > 0


def get_signal_history(stock_code: str, signal_type: str = "", limit: int = 5) -> list[dict]:
    """해당 종목의 과거 AI 판단 이력 (결과 수익률 포함, 최신순).
    signal_type 지정 시 해당 타입만 조회.
    """
    with get_conn() as conn:
        if signal_type:
            rows = conn.execute(
                "SELECT created_at, current_price, claude_opinion, action, result_pct "
                "FROM signals WHERE stock_code = ? AND claude_opinion IS NOT NULL "
                "AND signal_type = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (stock_code, signal_type, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT created_at, current_price, claude_opinion, action, result_pct "
                "FROM signals WHERE stock_code = ? AND claude_opinion IS NOT NULL "
                "ORDER BY created_at DESC LIMIT ?",
                (stock_code, limit),
            ).fetchall()
    return [dict(r) for r in rows]


def update_signal_action(signal_id: int, action: str) -> bool:
    """신호에 대한 사용자 행동 기록 (매수/매도/홀드)."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE signals SET action = ? WHERE id = ?", (action, signal_id)
        )
        conn.commit()
    return cur.rowcount > 0


def save_screening_log(
    stock_code: str,
    stock_name: str,
    source: str,
    recommendation: str,
    reason: str,
    met_conditions: list | None = None,
    rr_ratio: float | None = None,
    current_price: int | None = None,
    indicator_snapshot: str | None = None,
    dart_summary: str | None = None,
    news_summary: str | None = None,
    market_snapshot: str | None = None,
    ai_response: str | None = None,
) -> int:
    """스크리닝 AI 판단 이력 저장. screening_log_id 반환."""
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO screening_log
                (created_at, stock_code, stock_name, source, recommendation, reason,
                 met_conditions, rr_ratio, current_price, indicator_snapshot,
                 dart_summary, news_summary, market_snapshot, ai_response)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                _now_kst().strftime("%Y-%m-%d %H:%M:%S"),
                stock_code, stock_name, source, recommendation, reason,
                json.dumps(met_conditions or [], ensure_ascii=False),
                rr_ratio, current_price, indicator_snapshot,
                dart_summary, news_summary, market_snapshot, ai_response,
            ),
        )
        conn.commit()
        return cur.lastrowid


def update_screening_action(log_id: int, action: str) -> bool:
    """스크리닝 결과에 대한 사용자 행동 기록 (accepted/rejected)."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE screening_log SET user_action = ? WHERE id = ?", (action, log_id)
        )
        conn.commit()
    return cur.rowcount > 0


def update_screening_result(log_id: int, result_pct: float, period: str = "7d") -> bool:
    """스크리닝 종목의 사후 수익률 업데이트."""
    col = "result_7d" if period == "7d" else "result_30d"
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE screening_log SET {col} = ? WHERE id = ?", (result_pct, log_id)
        )
        conn.commit()
    return cur.rowcount > 0


def get_last_signal_date(stock_code: str) -> datetime | None:
    """해당 종목의 가장 최근 신호 발생 일시. 없으면 None."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT MAX(created_at) as last FROM signals WHERE stock_code = ?",
            (stock_code,),
        ).fetchone()
    if row and row["last"]:
        try:
            return datetime.fromisoformat(row["last"])
        except ValueError:
            return None
    return None


def get_today_signals() -> list[dict]:
    today = _now_kst().strftime("%Y-%m-%d")
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM signals WHERE created_at LIKE ? ORDER BY created_at DESC",
            (f"{today}%",),
        ).fetchall()
    return [dict(r) for r in rows]


def get_recent_signals(limit: int = 20) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM signals ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_signal(signal_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM signals WHERE id = ?", (signal_id,))
    return cur.rowcount > 0


def delete_signals_by_stock(stock_code: str) -> int:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM signals WHERE stock_code = ?", (stock_code,))
    return cur.rowcount


def delete_signals_before(date_str: str) -> int:
    """date_str: 'YYYY-MM-DD' 형식. 해당 날짜 이전(미포함) 신호 삭제."""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM signals WHERE created_at < ?", (date_str,))
    return cur.rowcount


def delete_all_signals() -> int:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM signals")
    return cur.rowcount


# ── 일일 복기 / RAG 데이터 ─────────────────────────────────────────────────


def get_verdict_accuracy(days: int = 14) -> dict:
    """최근 N일간 AI 판정(verdict)별 적중률 통계.
    result_1d/result_pct(3d)/result_5d 가 채워진 신호만 집계.
    Returns: {verdict: {count, avg_1d, avg_3d, avg_5d, hit_rate_3d}}
    """
    since = (_now_kst() - timedelta(days=days)).strftime("%Y-%m-%d")
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT verdict, result_1d, result_pct, result_5d
               FROM signals
               WHERE created_at >= ? AND verdict IS NOT NULL
                 AND result_pct IS NOT NULL""",
            (since,),
        ).fetchall()
    stats: dict = {}
    for r in rows:
        v = r["verdict"]
        if v not in stats:
            stats[v] = {"count": 0, "sum_1d": 0.0, "sum_3d": 0.0, "sum_5d": 0.0, "hit_3d": 0, "n_1d": 0, "n_5d": 0}
        s = stats[v]
        s["count"] += 1
        r3 = r["result_pct"] or 0
        s["sum_3d"] += r3
        if _is_verdict_hit_3d(v, r3):
            s["hit_3d"] += 1
        if r["result_1d"] is not None:
            s["sum_1d"] += r["result_1d"]
            s["n_1d"] += 1
        if r["result_5d"] is not None:
            s["sum_5d"] += r["result_5d"]
            s["n_5d"] += 1
    result = {}
    for v, s in stats.items():
        result[v] = {
            "count": s["count"],
            "avg_1d": round(s["sum_1d"] / s["n_1d"], 2) if s["n_1d"] else None,
            "avg_3d": round(s["sum_3d"] / s["count"], 2) if s["count"] else None,
            "avg_5d": round(s["sum_5d"] / s["n_5d"], 2) if s["n_5d"] else None,
            "hit_rate_3d": round(s["hit_3d"] / s["count"] * 100, 1) if s["count"] else None,
        }
    return result


def get_recent_daily_reviews(limit: int = 3) -> list[dict]:
    """최근 daily_review 전략 노트 조회 (판단 AI 프롬프트 주입용)."""
    with get_conn() as conn:
        try:
            rows = conn.execute(
                "SELECT created_at, summary, detail FROM strategy_notes "
                "WHERE category = 'daily_review' ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []


def get_screening_accuracy(days: int = 30) -> dict:
    """최근 N일 스크리닝 추천 종목의 성과 통계.
    Returns: {total, hit_7d, hit_30d, avg_7d, avg_30d}
    """
    since = (_now_kst() - timedelta(days=days)).strftime("%Y-%m-%d")
    with get_conn() as conn:
        try:
            rows = conn.execute(
                """SELECT recommendation, result_7d, result_30d
                   FROM screening_log
                   WHERE created_at >= ? AND recommendation = '관심종목 등록'""",
                (since,),
            ).fetchall()
        except Exception:
            return {}
    if not rows:
        return {}
    total = len(rows)
    r7_vals = [r["result_7d"] for r in rows if r["result_7d"] is not None]
    r30_vals = [r["result_30d"] for r in rows if r["result_30d"] is not None]
    return {
        "total": total,
        "avg_7d": round(sum(r7_vals) / len(r7_vals), 2) if r7_vals else None,
        "avg_30d": round(sum(r30_vals) / len(r30_vals), 2) if r30_vals else None,
        "hit_7d": round(sum(1 for v in r7_vals if v > 0) / len(r7_vals) * 100, 1) if r7_vals else None,
        "hit_30d": round(sum(1 for v in r30_vals if v > 0) / len(r30_vals) * 100, 1) if r30_vals else None,
    }


def search_similar_signals(
    rsi: float | None = None,
    trend: str | None = None,
    signal_type: str = "",
    volume_ratio: float | None = None,
    above_ma20: bool | None = None,
    limit: int = 5,
    days: int = 90,
    rsi_tolerance: float = 5.0,
    volume_low_multiplier: float = 0.5,
    volume_high_multiplier: float = 2.0,
) -> list[dict]:
    """현재 지표와 유사한 과거 신호 검색 (SQL 범위 필터 기반 RAG).

    반환: [{created_at, stock_name, signal_type, verdict, result_pct, result_3d,
             result_5d, triggered_conditions, indicator_snapshot}]
    """
    since = (_now_kst() - timedelta(days=days)).strftime("%Y-%m-%d")
    conditions = ["created_at >= ?", "verdict IS NOT NULL"]
    params: list = [since]

    if rsi is not None:
        tol = max(0.5, float(rsi_tolerance))
        conditions.append("json_extract(indicator_snapshot, '$.rsi') BETWEEN ? AND ?")
        params += [rsi - tol, rsi + tol]
    if trend:
        conditions.append("json_extract(indicator_snapshot, '$.trend') = ?")
        params.append(trend)
    if signal_type:
        conditions.append("signal_type = ?")
        params.append(signal_type)
    if volume_ratio is not None:
        low_mul = max(0.01, float(volume_low_multiplier))
        high_mul = max(low_mul, float(volume_high_multiplier))
        conditions.append("json_extract(indicator_snapshot, '$.volume_ratio') BETWEEN ? AND ?")
        params += [volume_ratio * low_mul, volume_ratio * high_mul]
    if above_ma20 is not None:
        conditions.append("json_extract(indicator_snapshot, '$.above_ma20') = ?")
        params.append(1 if above_ma20 else 0)

    where = " AND ".join(conditions)
    params.append(limit)

    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT created_at, stock_name, signal_type, verdict,
                       result_pct, result_1d, result_5d,
                       triggered_conditions, indicator_snapshot
                FROM signals WHERE {where}
                ORDER BY created_at DESC LIMIT ?""",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def get_condition_accuracy(days: int = 30, min_count: int = 3) -> list[dict]:
    """triggered_conditions 항목별 적중률 통계.

    반환: [{condition, count, hit_rate_3d, avg_3d, avg_5d}] — hit_rate 낮은 순 정렬
    """
    since = (_now_kst() - timedelta(days=days)).strftime("%Y-%m-%d")
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT triggered_conditions, verdict, result_pct, result_5d
               FROM signals
               WHERE created_at >= ? AND verdict IS NOT NULL AND result_pct IS NOT NULL""",
            (since,),
        ).fetchall()

    stats: dict[str, dict] = {}
    for r in rows:
        raw = r["triggered_conditions"] or ""
        # "조건A, 조건B" → ["조건A", "조건B"]
        conds = [c.strip() for c in raw.split(",") if c.strip()]
        v = r["verdict"]
        r3 = r["result_pct"] or 0
        r5 = r["result_5d"]
        for cond in conds:
            if cond not in stats:
                stats[cond] = {"count": 0, "hit_3d": 0, "sum_3d": 0.0, "sum_5d": 0.0, "n_5d": 0}
            s = stats[cond]
            s["count"] += 1
            s["sum_3d"] += r3
            if _is_verdict_hit_3d(v, r3):
                s["hit_3d"] += 1
            if r5 is not None:
                s["sum_5d"] += r5
                s["n_5d"] += 1

    result = []
    for cond, s in stats.items():
        if s["count"] < min_count:
            continue
        result.append({
            "condition": cond,
            "count": s["count"],
            "hit_rate_3d": round(s["hit_3d"] / s["count"] * 100, 1),
            "avg_3d": round(s["sum_3d"] / s["count"], 2),
            "avg_5d": round(s["sum_5d"] / s["n_5d"], 2) if s["n_5d"] else None,
        })
    return sorted(result, key=lambda x: x["hit_rate_3d"])


def get_pattern_accuracy(days: int = 30, min_count: int = 2) -> list[dict]:
    """chart_patterns 항목별 적중률 통계.

    반환: [{pattern, count, hit_rate_3d, avg_3d}] — hit_rate 높은 순 정렬
    """
    since = (_now_kst() - timedelta(days=days)).strftime("%Y-%m-%d")
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT chart_patterns, verdict, result_pct
               FROM signals
               WHERE created_at >= ? AND verdict IS NOT NULL
                 AND result_pct IS NOT NULL AND chart_patterns IS NOT NULL""",
            (since,),
        ).fetchall()

    stats: dict[str, dict] = {}
    for r in rows:
        try:
            patterns = json.loads(r["chart_patterns"] or "[]")
        except Exception:
            continue
        v = r["verdict"]
        r3 = r["result_pct"] or 0
        for pat in patterns:
            if not pat:
                continue
            if pat not in stats:
                stats[pat] = {"count": 0, "hit_3d": 0, "sum_3d": 0.0}
            s = stats[pat]
            s["count"] += 1
            s["sum_3d"] += r3
            if _is_verdict_hit_3d(v, r3):
                s["hit_3d"] += 1

    result = []
    for pat, s in stats.items():
        if s["count"] < min_count:
            continue
        result.append({
            "pattern": pat,
            "count": s["count"],
            "hit_rate_3d": round(s["hit_3d"] / s["count"] * 100, 1),
            "avg_3d": round(s["sum_3d"] / s["count"], 2),
        })
    return sorted(result, key=lambda x: x["hit_rate_3d"], reverse=True)


def save_paper_trade(
    stock_code: str, stock_name: str, order_type: str,
    quantity: int, price: int, signal_id: int | None = None, verdict: str | None = None,
) -> int:
    """모의투자 체결 기록. 반환: paper_trade id."""
    now = _now_kst().strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO paper_trades
               (created_at, stock_code, stock_name, order_type, quantity, price, signal_id, verdict)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (now, stock_code, stock_name, order_type, quantity, price, signal_id, verdict),
        )
        conn.commit()
        return cur.lastrowid


def update_paper_result(paper_id: int, result_pct: float, period: str = "3d") -> None:
    col = {"1d": "result_1d", "3d": "result_3d", "5d": "result_5d"}.get(period)
    if not col:
        return
    with get_conn() as conn:
        conn.execute(f"UPDATE paper_trades SET {col}=? WHERE id=?", (result_pct, paper_id))
        conn.commit()


def get_paper_trades(days: int = 30) -> list[dict]:
    """최근 N일 모의투자 내역."""
    since = (_now_kst() - timedelta(days=days)).strftime("%Y-%m-%d")
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM paper_trades WHERE created_at >= ? ORDER BY created_at DESC""",
            (since,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_weekly_performance_report(days: int = 7) -> dict:
    """최근 N일간 AI 신호 성과 통계를 통합/포트폴리오/워치리스트로 집계."""
    since = (_now_kst() - timedelta(days=days)).strftime("%Y-%m-%d")
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT stock_name, verdict, result_1d, result_pct AS result_3d, result_5d, in_portfolio
               FROM signals
               WHERE created_at >= ? AND verdict IS NOT NULL
               ORDER BY created_at DESC""",
            (since,),
        ).fetchall()

    rows = [dict(r) for r in rows]

    def _calc_block(block_rows: list[dict]) -> dict:
        signal_count = len(block_rows)
        rated = [r for r in block_rows if r["result_3d"] is not None]
        rated_count = len(rated)

        if not rated:
            return {
                "signal_count": signal_count,
                "rated_count": 0,
                "win_rate_3d": None,
                "avg_return_3d": None,
                "avg_return_1d": None,
                "avg_return_5d": None,
                "max_gain_3d": None,
                "max_loss_3d": None,
                "best_stock": None,
                "worst_stock": None,
                "verdict_breakdown": {},
                "buy_count": 0,
                "hold_count": 0,
                "sell_count": 0,
            }

        returns_3d = [r["result_3d"] for r in rated]
        wins = sum(1 for r in rated if _is_verdict_hit_3d(r["verdict"], r["result_3d"]))
        win_rate = round(wins / rated_count * 100, 1)
        avg_3d = round(sum(returns_3d) / rated_count, 2)

        r1d_vals = [r["result_1d"] for r in rated if r["result_1d"] is not None]
        r5d_vals = [r["result_5d"] for r in rated if r["result_5d"] is not None]
        avg_1d = round(sum(r1d_vals) / len(r1d_vals), 2) if r1d_vals else None
        avg_5d = round(sum(r5d_vals) / len(r5d_vals), 2) if r5d_vals else None

        max_gain = round(max(returns_3d), 2)
        max_loss = round(min(returns_3d), 2)

        best = max(rated, key=lambda r: r["result_3d"])
        worst = min(rated, key=lambda r: r["result_3d"])

        vbreakdown: dict = {}
        for r in rated:
            verdict = r["verdict"]
            if verdict not in vbreakdown:
                vbreakdown[verdict] = {"count": 0, "wins": 0, "sum_3d": 0.0}
            s = vbreakdown[verdict]
            s["count"] += 1
            s["sum_3d"] += r["result_3d"]
            if _is_verdict_hit_3d(verdict, r["result_3d"]):
                s["wins"] += 1

        verdict_breakdown = {
            v: {
                "count": s["count"],
                "win_rate": round(s["wins"] / s["count"] * 100, 1),
                "avg_return": round(s["sum_3d"] / s["count"], 2),
            }
            for v, s in vbreakdown.items()
        }

        return {
            "signal_count": signal_count,
            "rated_count": rated_count,
            "win_rate_3d": win_rate,
            "avg_return_3d": avg_3d,
            "avg_return_1d": avg_1d,
            "avg_return_5d": avg_5d,
            "max_gain_3d": max_gain,
            "max_loss_3d": max_loss,
            "best_stock": {"name": best["stock_name"], "return": round(best["result_3d"], 2)},
            "worst_stock": {"name": worst["stock_name"], "return": round(worst["result_3d"], 2)},
            "verdict_breakdown": verdict_breakdown,
            "buy_count": vbreakdown.get("매수", {}).get("count", 0),
            "hold_count": vbreakdown.get("홀드", {}).get("count", 0),
            "sell_count": vbreakdown.get("매도", {}).get("count", 0),
        }

    total_block = _calc_block(rows)
    portfolio_block = _calc_block([r for r in rows if int(r.get("in_portfolio") or 0) == 1])
    watchlist_block = _calc_block([r for r in rows if int(r.get("in_portfolio") or 0) == 0])

    paper_summary = None
    try:
        with get_conn() as conn:
            p_rows = conn.execute(
                """SELECT order_type, verdict, result_3d
                   FROM paper_trades WHERE created_at >= ? AND result_3d IS NOT NULL""",
                (since,),
            ).fetchall()
        if p_rows:
            p_buy = [r["result_3d"] for r in p_rows if r["order_type"] == "BUY" and r["result_3d"] is not None]
            paper_summary = {
                "count": len(p_buy),
                "avg_return": round(sum(p_buy) / len(p_buy), 2) if p_buy else None,
                "win_rate": round(sum(1 for r in p_buy if r > 0) / len(p_buy) * 100, 1) if p_buy else None,
            }
    except Exception:
        pass

    return {
        "period_days": days,
        "signal_count": total_block["signal_count"],
        "rated_count": total_block["rated_count"],
        "win_rate_3d": total_block["win_rate_3d"],
        "avg_return_3d": total_block["avg_return_3d"],
        "avg_return_1d": total_block["avg_return_1d"],
        "avg_return_5d": total_block["avg_return_5d"],
        "max_gain_3d": total_block["max_gain_3d"],
        "max_loss_3d": total_block["max_loss_3d"],
        "best_stock": total_block["best_stock"],
        "worst_stock": total_block["worst_stock"],
        "verdict_breakdown": total_block["verdict_breakdown"],
        "buy_count": total_block["buy_count"],
        "hold_count": total_block["hold_count"],
        "sell_count": total_block["sell_count"],
        "portfolio": portfolio_block,
        "watchlist": watchlist_block,
        "paper_summary": paper_summary,
    }
