from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from core.models import Candle
from utilities.settings import logger


class EventType(str, Enum):
    DATA_SYNC = "DATA_SYNC"
    CHART = "CHART"
    LLM = "LLM"
    DECISION = "DECISION"
    RISK = "RISK"
    TRADE = "TRADE"
    SYSTEM = "SYSTEM"
    ERROR = "ERROR"


class Ledger:
    """SQLite ledger for candles, decisions, LLM metrics, risk checks, and trade API responses."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS candles (
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    time INTEGER NOT NULL,
                    time_iso TEXT NOT NULL,
                    ts_ms INTEGER NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL NOT NULL,
                    spread REAL DEFAULT 0,
                    real_volume REAL DEFAULT 0,
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY(symbol, timeframe, time)
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS event_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    symbol TEXT,
                    timeframe TEXT,
                    uid INTEGER,
                    strategy TEXT,
                    payload TEXT NOT NULL
                );
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_event_lookup ON event_log(symbol, uid, strategy, event_type);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_event_ts ON event_log(ts);")

    def upsert_candles(self, candles: list[Candle]) -> int:
        if not candles:
            return 0
        now = datetime.now(timezone.utc).isoformat()
        rows = [
            (
                c.symbol, c.timeframe, c.time, c.time_iso, c.ts_ms,
                c.open, c.high, c.low, c.close, c.volume, c.spread, c.real_volume, now,
            )
            for c in candles
        ]
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO candles(symbol,timeframe,time,time_iso,ts_ms,open,high,low,close,volume,spread,real_volume,fetched_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol,timeframe,time) DO UPDATE SET
                    time_iso=excluded.time_iso,
                    ts_ms=excluded.ts_ms,
                    open=excluded.open,
                    high=excluded.high,
                    low=excluded.low,
                    close=excluded.close,
                    volume=excluded.volume,
                    spread=excluded.spread,
                    real_volume=excluded.real_volume,
                    fetched_at=excluded.fetched_at;
                """,
                rows,
            )
        return len(rows)

    def latest_candle_time(self, symbol: str, timeframe: str) -> int | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(time) AS t FROM candles WHERE symbol=? AND timeframe=?",
                (symbol, timeframe),
            ).fetchone()
        return int(row["t"]) if row and row["t"] is not None else None

    def load_candles_df(self, symbol: str, timeframe: str, limit: int, end_time: int | None = None):
        import pandas as pd

        query = """
                SELECT ts_ms AS ts, open, high, low, close, volume, time_iso, time
                FROM candles
                WHERE symbol=? AND timeframe=?
                {where_end}
                ORDER BY time DESC
                LIMIT ?
                """
        params: tuple[Any, ...]
        if end_time is None:
            query = query.format(where_end="")
            params = (symbol, timeframe, limit)
        else:
            query = query.format(where_end="AND time <= ?")
            params = (symbol, timeframe, end_time, limit)

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        rows = list(reversed([dict(r) for r in rows]))
        df = pd.DataFrame(rows)
        if not df.empty:
            df["datetime"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
            df["uid"] = df["datetime"].dt.strftime("%Y%m%d%H%M").astype(int)
        return df

    def log(self, event_type: EventType, symbol: str | None, uid: int | None, strategy: str | None, data: Any,
            timeframe: str | None = None) -> None:
        payload = _json(data)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO event_log(ts,event_type,symbol,timeframe,uid,strategy,payload) VALUES(?,?,?,?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(), event_type.value, symbol, timeframe, uid, strategy, payload),
            )

    def get_last_event(self, event_type: EventType, symbol: str, uid: int, strategy: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT payload FROM event_log
                WHERE event_type=? AND symbol=? AND uid=? AND strategy=?
                ORDER BY id DESC LIMIT 1
                """,
                (event_type.value, symbol, uid, strategy),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def get_last_decision(self, symbol: str, uid: int, strategy: str) -> dict[str, Any] | None:
        return self.get_last_event(EventType.DECISION, symbol, uid, strategy)

    def recent_llm_metrics(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT ts, symbol, uid, strategy, payload
                FROM event_log
                WHERE event_type='LLM'
                ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            out.append(item)
        return out


def _json(data: Any) -> str:
    if is_dataclass(data):
        data = asdict(data)
    return json.dumps(data, default=str, ensure_ascii=False)
