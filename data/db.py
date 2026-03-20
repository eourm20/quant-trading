"""
SQLite 거래 로그 DB
"""

import json
import os
import sqlite3
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "trading.db")


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


def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS watchlist (
                code       TEXT PRIMARY KEY,
                name       TEXT NOT NULL,
                enabled    INTEGER NOT NULL DEFAULT 1,
                conditions TEXT NOT NULL DEFAULT '{}'
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
            CREATE TABLE IF NOT EXISTS cooldowns (
                key TEXT PRIMARY KEY,
                last_sent_at TEXT NOT NULL
            )
        """)
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
                tax          INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS strategy_notes (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                category   TEXT NOT NULL,
                summary    TEXT NOT NULL,
                detail     TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.commit()
    _seed_conditions()


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

def get_watchlist() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM watchlist ORDER BY rowid").fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["conditions"] = json.loads(d["conditions"])
        d["enabled"] = bool(d["enabled"])
        result.append(d)
    return result


def upsert_stock(code: str, name: str, enabled: bool, conditions: dict):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO watchlist (code, name, enabled, conditions) VALUES (?,?,?,?) "
            "ON CONFLICT(code) DO UPDATE SET name=excluded.name, enabled=excluded.enabled, conditions=excluded.conditions",
            (code, name, int(enabled), json.dumps(conditions, ensure_ascii=False))
        )
        conn.commit()


def update_stock_field(code: str, field: str, value) -> bool:
    """종목 최상위 필드(name, enabled) 또는 conditions 내부 필드 수정"""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM watchlist WHERE code = ?", (code,)).fetchone()
        if not row:
            return False
        if field in ("name", "enabled", "horizon"):
            conn.execute(f"UPDATE watchlist SET {field} = ? WHERE code = ?", (value, code))
        else:
            cond = json.loads(row["conditions"])
            cond[field] = value
            conn.execute("UPDATE watchlist SET conditions = ? WHERE code = ?",
                         (json.dumps(cond, ensure_ascii=False), code))
        conn.commit()
    return True


def delete_stock(code: str) -> bool:
    with get_conn() as conn:
        result = conn.execute("DELETE FROM watchlist WHERE code = ?", (code,))
        conn.commit()
    return result.rowcount > 0


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
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        # 기존 전체 삭제 후 재삽입 (잔량 0인 종목 자동 제거)
        conn.execute("DELETE FROM portfolio")
        for h in holdings:
            code = str(h.get("stk_cd") or "").replace("A", "").strip()
            if not code:
                continue
            conn.execute(
                """
                INSERT INTO portfolio
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
    """매매 내역 추가 (중복 무시)"""
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
                INSERT OR IGNORE INTO trades
                    (trade_id, executed_at, stock_code, stock_name, side,
                     quantity, price, amount, fee, tax)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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


def get_trades(limit: int = 50) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY executed_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_recent_trades_for_stock(stock_code: str, days: int = 3) -> list[dict]:
    """특정 종목의 최근 N일 매매 이력 조회 (최신순)"""
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    with get_conn() as conn:
        try:
            rows = conn.execute(
                "SELECT * FROM trades WHERE stock_code = ? AND executed_at >= ? ORDER BY executed_at DESC",
                (stock_code, cutoff),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []


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
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), category, summary, detail),
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
            "SELECT last_sent_at FROM cooldowns WHERE key = ?", (key,)
        ).fetchone()
    if row:
        return datetime.strptime(row["last_sent_at"], "%Y-%m-%d %H:%M:%S")
    return None


def set_cooldown(key: str):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO cooldowns (key, last_sent_at) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET last_sent_at = excluded.last_sent_at",
            (key, now),
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


def set_add_cooldown_after_trade(stock_code: str, suppress_minutes: int = 60) -> int:
    """매수 체결 후 add/both 조건 신호를 suppress_minutes 동안 억제.
    last_sent_at = (now + suppress_minutes) - cooldown_minutes
    → 해당 조건의 다음 발동 시각이 now + suppress_minutes가 되도록 설정.
    반환: 억제 설정된 조건 수.
    """
    from datetime import timedelta
    cond_map = {c["id"]: c.get("cooldown_minutes", 60) for c in get_conditions()
                if c.get("signal_type") in ("add", "both")}
    now = datetime.now()
    count = 0
    with get_conn() as conn:
        for cond_id, cooldown_minutes in cond_map.items():
            key = f"{stock_code}:{cond_id}"
            # last_sent_at을 설정해 suppress_minutes 후에 다시 발동하도록 함
            effective_last = now + timedelta(minutes=suppress_minutes - cooldown_minutes)
            conn.execute(
                "INSERT INTO cooldowns (key, last_sent_at) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET last_sent_at = excluded.last_sent_at",
                (key, effective_last.strftime("%Y-%m-%d %H:%M:%S")),
            )
            count += 1
        conn.commit()
    return count


def shorten_cooldowns_for_stock(stock_code: str, ratio: float = 0.25, min_minutes: int = 30) -> int:
    """홀드 결정 후 해당 종목의 남은 쿨다운을 원래의 ratio 비율로 단축.
    예: 원래 120분 쿨다운 → 30분 후 재알림 (ratio=0.25, min=30)
    반환: 단축된 조건 수
    """
    from datetime import timedelta
    cond_map = {c["id"]: c["cooldown_minutes"] for c in get_conditions()}
    now = datetime.now()
    count = 0
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT key, last_sent_at FROM cooldowns WHERE key LIKE ?",
            (f"{stock_code}:%",)
        ).fetchall()
        for row in rows:
            key = row["key"]
            cond_id = key.split(":", 1)[1]
            original_minutes = cond_map.get(cond_id, 60)
            short_minutes = max(min_minutes, int(original_minutes * ratio))
            # last_sent_at을 뒤로 밀어서 남은 쿨다운 = short_minutes
            new_last = now - timedelta(minutes=original_minutes - short_minutes)
            conn.execute(
                "UPDATE cooldowns SET last_sent_at = ? WHERE key = ?",
                (new_last.strftime("%Y-%m-%d %H:%M:%S"), key)
            )
            count += 1
        conn.commit()
    return count


def _extract_verdict(claude_opinion: str | None) -> str | None:
    """AI 판단 텍스트에서 [매수]/[매도]/[홀드] 추출."""
    if not claude_opinion:
        return None
    first_line = claude_opinion.strip().splitlines()[0] if claude_opinion.strip() else ""
    for v in ["매수", "매도", "홀드"]:
        if f"[{v}]" in first_line:
            return v
    return None


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
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
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
        return cur.lastrowid


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
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
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
    today = datetime.now().strftime("%Y-%m-%d")
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
