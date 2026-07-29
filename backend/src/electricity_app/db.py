from __future__ import annotations

from contextlib import contextmanager
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Iterator
from zoneinfo import ZoneInfo

from electricity_app.domain import ElectricityRecord, SyncOutcome


_SHANGHAI = ZoneInfo("Asia/Shanghai")


class Database:
    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS electricity_records (
                    unique_key TEXT PRIMARY KEY,
                    upstream_id TEXT,
                    room_name TEXT NOT NULL,
                    device_name TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    energy TEXT NOT NULL,
                    money TEXT NOT NULL,
                    rate TEXT,
                    balance TEXT,
                    first_seen_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_occurred_at
                ON electricity_records(occurred_at);

                CREATE TABLE IF NOT EXISTS sync_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('success', 'failed', 'auth_required')),
                    fetched INTEGER NOT NULL,
                    inserted INTEGER NOT NULL,
                    updated INTEGER NOT NULL,
                    error_code TEXT
                );

                CREATE TABLE IF NOT EXISTS balance_snapshots (
                    observed_at TEXT PRIMARY KEY,
                    effective_at TEXT NOT NULL,
                    balance TEXT NOT NULL,
                    source TEXT NOT NULL CHECK(source IN ('property_detail', 'property_balance'))
                );

                CREATE TABLE IF NOT EXISTS daily_summaries (
                    day TEXT PRIMARY KEY,
                    total_energy TEXT NOT NULL,
                    total_cost TEXT NOT NULL,
                    record_count INTEGER NOT NULL,
                    peak_energy TEXT,
                    peak_started_at TEXT,
                    baseline_energy TEXT,
                    anomaly_score TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS wechat_allowlist (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    openid_hmac TEXT NOT NULL UNIQUE,
                    openid_ciphertext TEXT,
                    enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0, 1)),
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS daily_reminder_deliveries (
                    day TEXT NOT NULL,
                    openid_hmac TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    PRIMARY KEY (day, openid_hmac),
                    FOREIGN KEY (openid_hmac) REFERENCES wechat_allowlist(openid_hmac)
                );

                CREATE TABLE IF NOT EXISTS oauth_nonces (
                    nonce_digest TEXT PRIMARY KEY,
                    expires_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_oauth_nonces_expires_at
                ON oauth_nonces(expires_at);

                CREATE TABLE IF NOT EXISTS runtime_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            snapshot_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(balance_snapshots)"
                ).fetchall()
            }
            if "effective_at" not in snapshot_columns:
                connection.execute(
                    "ALTER TABLE balance_snapshots ADD COLUMN effective_at TEXT"
                )
                connection.execute(
                    """
                    UPDATE balance_snapshots
                    SET effective_at = observed_at
                    WHERE effective_at IS NULL
                    """
                )
            allowlist_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(wechat_allowlist)"
                ).fetchall()
            }
            if "openid_ciphertext" not in allowlist_columns:
                connection.execute(
                    "ALTER TABLE wechat_allowlist ADD COLUMN openid_ciphertext TEXT"
                )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_balance_effective_at
                ON balance_snapshots(effective_at, observed_at)
                """
            )

    def upsert_records(self, records: Iterable[ElectricityRecord]) -> tuple[int, int]:
        with self.connection() as connection:
            return self._upsert_records(connection, records)

    def apply_sync(
        self,
        records: Iterable[ElectricityRecord],
        outcome: SyncOutcome,
        observed_at: datetime,
    ) -> tuple[int, int]:
        records = list(records)
        with self.connection() as connection:
            inserted, updated = self._upsert_records(connection, records)
            latest_record = max(
                (record for record in records if record.balance is not None),
                key=lambda record: record.occurred_at,
                default=None,
            )
            if latest_record is not None:
                connection.execute(
                    """
                    INSERT INTO balance_snapshots (
                        observed_at, effective_at, balance, source
                    )
                    VALUES (?, ?, ?, 'property_detail')
                    ON CONFLICT(observed_at) DO UPDATE SET
                        effective_at = excluded.effective_at,
                        balance = excluded.balance,
                        source = excluded.source
                    """,
                    (
                        _datetime_to_text(observed_at),
                        _datetime_to_text(latest_record.occurred_at),
                        str(latest_record.balance),
                    ),
                )
            connection.execute(
                """
                INSERT INTO sync_runs (
                    started_at, finished_at, start_date, end_date, status, fetched,
                    inserted, updated, error_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _datetime_to_text(outcome.started_at),
                    _datetime_to_text(outcome.finished_at),
                    outcome.start_date.isoformat(),
                    outcome.end_date.isoformat(),
                    outcome.status,
                    outcome.fetched,
                    inserted,
                    updated,
                    outcome.error_code,
                ),
            )
            if outcome.status == "auth_required":
                connection.execute(
                    """
                    INSERT INTO runtime_state (key, value, updated_at)
                    VALUES ('property_auth_gate', 'blocked', ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at
                    """,
                    (_datetime_to_text(outcome.finished_at),),
                )
            return inserted, updated

    def list_records(self, start_date: date, end_date: date) -> list[ElectricityRecord]:
        end_exclusive = end_date + timedelta(days=1)
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT unique_key, upstream_id, room_name, device_name, occurred_at,
                       energy, money, rate, balance
                FROM electricity_records
                WHERE occurred_at >= ? AND occurred_at < ?
                ORDER BY occurred_at, unique_key
                """,
                (start_date.isoformat(), end_exclusive.isoformat()),
            ).fetchall()
        return [self._record_from_row(row) for row in rows]

    def count_records(self) -> int:
        with self.connection() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM electricity_records").fetchone()[0])

    def latest_balance(self) -> Decimal | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT balance
                FROM balance_snapshots
                ORDER BY julianday(effective_at) DESC,
                         effective_at DESC,
                         julianday(observed_at) DESC,
                         observed_at DESC
                LIMIT 1
                """
            ).fetchone()
        return Decimal(row["balance"]) if row is not None else None

    def latest_balance_for_day(self, day: date) -> Decimal | None:
        start = datetime.combine(day, time.min, _SHANGHAI)
        end = start + timedelta(days=1)
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT balance
                FROM electricity_records
                WHERE balance IS NOT NULL
                  AND julianday(occurred_at) >= julianday(?)
                  AND julianday(occurred_at) < julianday(?)
                ORDER BY julianday(occurred_at) DESC,
                         occurred_at DESC,
                         unique_key DESC
                LIMIT 1
                """,
                (_datetime_to_text(start), _datetime_to_text(end)),
            ).fetchone()
        return Decimal(row["balance"]) if row is not None else None

    def last_successful_sync(self) -> SyncOutcome | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT started_at, finished_at, start_date, end_date, status, fetched,
                       inserted, updated, error_code
                FROM sync_runs
                WHERE status = 'success'
                ORDER BY finished_at DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
        if row is None:
            return None
        return SyncOutcome(
            started_at=_datetime_from_text(row["started_at"]),
            finished_at=_datetime_from_text(row["finished_at"]),
            start_date=date.fromisoformat(row["start_date"]),
            end_date=date.fromisoformat(row["end_date"]),
            status=row["status"],
            fetched=row["fetched"],
            inserted=row["inserted"],
            updated=row["updated"],
            error_code=row["error_code"],
        )

    def latest_sync_status(self) -> str | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT status FROM sync_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return str(row["status"]) if row is not None else None

    def auth_gate_active(self) -> bool:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT value FROM runtime_state
                WHERE key = 'property_auth_gate'
                """
            ).fetchone()
        return row is not None and row["value"] == "blocked"

    def clear_auth_gate(self) -> bool:
        with self.connection() as connection:
            cursor = connection.execute(
                """
                DELETE FROM runtime_state
                WHERE key = 'property_auth_gate'
                """
            )
        return cursor.rowcount == 1

    def replace_daily_summary(
        self,
        day: date,
        *,
        total_energy: Decimal,
        total_cost: Decimal,
        record_count: int,
        peak_energy: Decimal | None,
        peak_started_at: datetime | None,
        baseline_energy: Decimal | None,
        anomaly_score: Decimal | None,
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO daily_summaries (
                    day, total_energy, total_cost, record_count, peak_energy,
                    peak_started_at, baseline_energy, anomaly_score, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(day) DO UPDATE SET
                    total_energy = excluded.total_energy,
                    total_cost = excluded.total_cost,
                    record_count = excluded.record_count,
                    peak_energy = excluded.peak_energy,
                    peak_started_at = excluded.peak_started_at,
                    baseline_energy = excluded.baseline_energy,
                    anomaly_score = excluded.anomaly_score,
                    updated_at = excluded.updated_at
                """,
                (
                    day.isoformat(),
                    str(total_energy),
                    str(total_cost),
                    record_count,
                    _decimal_to_text(peak_energy),
                    _datetime_to_text(peak_started_at) if peak_started_at else None,
                    _decimal_to_text(baseline_energy),
                    _decimal_to_text(anomaly_score),
                    _datetime_to_text(datetime.now().astimezone()),
                ),
            )

    def list_daily_summaries(self, start_date: date, end_date: date) -> list[dict[str, object]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT day, total_energy, total_cost, record_count, peak_energy,
                       peak_started_at, baseline_energy, anomaly_score, updated_at
                FROM daily_summaries
                WHERE day >= ? AND day <= ?
                ORDER BY day
                """,
                (start_date.isoformat(), end_date.isoformat()),
            ).fetchall()
        return [
            {
                "day": date.fromisoformat(row["day"]),
                "total_energy": Decimal(row["total_energy"]),
                "total_cost": Decimal(row["total_cost"]),
                "record_count": row["record_count"],
                "peak_energy": _decimal_from_text(row["peak_energy"]),
                "peak_started_at": _datetime_from_text(row["peak_started_at"])
                if row["peak_started_at"]
                else None,
                "baseline_energy": _decimal_from_text(row["baseline_energy"]),
                "anomaly_score": _decimal_from_text(row["anomaly_score"]),
                "updated_at": _datetime_from_text(row["updated_at"]),
            }
            for row in rows
        ]

    def upsert_pending_openid(self, openid_hmac: str) -> int:
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO wechat_allowlist (openid_hmac, created_at)
                VALUES (?, ?)
                ON CONFLICT(openid_hmac) DO NOTHING
                """,
                (openid_hmac, _datetime_to_text(datetime.now().astimezone())),
            )
            row = connection.execute(
                "SELECT id FROM wechat_allowlist WHERE openid_hmac = ?", (openid_hmac,)
            ).fetchone()
        return int(row["id"])

    def count_pending_openids(self) -> int:
        with self.connection() as connection:
            return int(
                connection.execute("SELECT COUNT(*) FROM wechat_allowlist WHERE enabled = 0").fetchone()[0]
            )

    def list_pending_openids(self) -> list[tuple[int, datetime]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT id, created_at FROM wechat_allowlist
                WHERE enabled = 0
                ORDER BY id
                """
            ).fetchall()
        return [(int(row["id"]), _datetime_from_text(row["created_at"])) for row in rows]

    def set_openid_enabled(self, request_id: int, enabled: bool) -> bool:
        with self.connection() as connection:
            cursor = connection.execute(
                "UPDATE wechat_allowlist SET enabled = ? WHERE id = ? AND enabled != ?",
                (int(enabled), request_id, int(enabled)),
            )
        return cursor.rowcount == 1

    def is_openid_allowed(self, openid_hmac: str) -> bool:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM wechat_allowlist WHERE openid_hmac = ? AND enabled = 1",
                (openid_hmac,),
            ).fetchone()
        return row is not None

    def save_authorized_openid(
        self, openid_hmac: str, openid_ciphertext: str
    ) -> bool:
        with self.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE wechat_allowlist
                SET openid_ciphertext = ?
                WHERE openid_hmac = ? AND enabled = 1
                """,
                (openid_ciphertext, openid_hmac),
            )
        return cursor.rowcount == 1

    def list_reminder_recipients(self) -> list[tuple[str, str]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT openid_hmac, openid_ciphertext
                FROM wechat_allowlist
                WHERE enabled = 1 AND openid_ciphertext IS NOT NULL
                ORDER BY id
                """
            ).fetchall()
        return [
            (str(row["openid_hmac"]), str(row["openid_ciphertext"]))
            for row in rows
        ]

    def reminder_was_sent(self, day: date, openid_hmac: str) -> bool:
        """Return whether this recipient has already received this day's reminder."""
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM daily_reminder_deliveries
                WHERE day = ? AND openid_hmac = ?
                """,
                (day.isoformat(), openid_hmac),
            ).fetchone()
        return row is not None

    def record_reminder_sent(
        self, day: date, openid_hmac: str, sent_at: datetime
    ) -> None:
        """Persist a successful delivery so retries cannot duplicate it."""
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO daily_reminder_deliveries (day, openid_hmac, sent_at)
                VALUES (?, ?, ?)
                ON CONFLICT(day, openid_hmac) DO NOTHING
                """,
                (day.isoformat(), openid_hmac, _datetime_to_text(sent_at)),
            )

    def create_oauth_nonce(
        self,
        nonce_digest: str,
        *,
        created_at: datetime,
        expires_at: datetime,
    ) -> None:
        created_at_text = _utc_datetime_to_text(created_at)
        expires_at_text = _utc_datetime_to_text(expires_at)
        with self.connection() as connection:
            connection.execute(
                "DELETE FROM oauth_nonces WHERE expires_at < ?",
                (created_at_text,),
            )
            connection.execute(
                """
                INSERT INTO oauth_nonces (nonce_digest, expires_at)
                VALUES (?, ?)
                """,
                (nonce_digest, expires_at_text),
            )

    def consume_oauth_nonce(
        self, nonce_digest: str, *, consumed_at: datetime
    ) -> bool:
        with self.connection() as connection:
            cursor = connection.execute(
                """
                DELETE FROM oauth_nonces
                WHERE nonce_digest = ? AND expires_at >= ?
                """,
                (nonce_digest, _utc_datetime_to_text(consumed_at)),
            )
            if cursor.rowcount == 0:
                connection.execute(
                    "DELETE FROM oauth_nonces WHERE nonce_digest = ?",
                    (nonce_digest,),
                )
        return cursor.rowcount == 1

    @contextmanager
    def connection(
        self,
        database_path: Path | None = None,
        *,
        configure: bool = True,
        read_only: bool = False,
    ) -> Iterator[sqlite3.Connection]:
        """Own one SQLite transaction and always close its connection."""
        if read_only:
            path = Path(database_path or self.database_path).resolve()
            connection = sqlite3.connect(
                f"{path.as_uri()}?mode=ro",
                uri=True,
            )
        elif database_path is None:
            connection = self._open_connection()
        else:
            connection = sqlite3.connect(database_path)
        try:
            connection.row_factory = sqlite3.Row
            if configure:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA foreign_keys=ON")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _open_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        return connection

    @staticmethod
    def _record_from_row(row: sqlite3.Row) -> ElectricityRecord:
        return ElectricityRecord(
            unique_key=row["unique_key"],
            upstream_id=row["upstream_id"],
            room_name=row["room_name"],
            device_name=row["device_name"],
            occurred_at=_datetime_from_text(row["occurred_at"]),
            energy=Decimal(row["energy"]),
            money=Decimal(row["money"]),
            rate=_decimal_from_text(row["rate"]),
            balance=_decimal_from_text(row["balance"]),
        )

    @staticmethod
    def _upsert_records(
        connection: sqlite3.Connection, records: Iterable[ElectricityRecord]
    ) -> tuple[int, int]:
        inserted = 0
        updated = 0
        for record in records:
            values = _record_values(record)
            existing = connection.execute(
                """
                SELECT upstream_id, room_name, device_name, occurred_at, energy, money, rate, balance
                FROM electricity_records WHERE unique_key = ?
                """,
                (record.unique_key,),
            ).fetchone()
            if existing is not None and tuple(existing) == values[1:]:
                continue

            now = _datetime_to_text(datetime.now().astimezone())
            connection.execute(
                """
                INSERT INTO electricity_records (
                    unique_key, upstream_id, room_name, device_name, occurred_at,
                    energy, money, rate, balance, first_seen_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(unique_key) DO UPDATE SET
                    upstream_id = excluded.upstream_id,
                    room_name = excluded.room_name,
                    device_name = excluded.device_name,
                    occurred_at = excluded.occurred_at,
                    energy = excluded.energy,
                    money = excluded.money,
                    rate = excluded.rate,
                    balance = excluded.balance,
                    updated_at = excluded.updated_at
                """,
                (*values, now, now),
            )
            if existing is None:
                inserted += 1
            else:
                updated += 1
        return inserted, updated


def _record_values(record: ElectricityRecord) -> tuple[str | None, ...]:
    return (
        record.unique_key,
        record.upstream_id,
        record.room_name,
        record.device_name,
        _datetime_to_text(record.occurred_at),
        str(record.energy),
        str(record.money),
        _decimal_to_text(record.rate),
        _decimal_to_text(record.balance),
    )


def _datetime_to_text(value: datetime) -> str:
    return value.isoformat()


def _utc_datetime_to_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("OAuth nonce datetimes must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _datetime_from_text(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _decimal_to_text(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _decimal_from_text(value: str | None) -> Decimal | None:
    return Decimal(value) if value is not None else None
