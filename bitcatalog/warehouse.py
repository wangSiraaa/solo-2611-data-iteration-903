"""Bus observation warehouse backed by SQLite.

Every ingested frame is bound to one immutable catalog product (identified by
the product's SHA-256 over its canonical definitions).  A frame is rejected as
a whole when its hex body is malformed, when the message is unknown to the
product, or when the decoded body length differs from ``payload_bytes``.
Rejections are retained with an error code and, where meaningful, an offset
into the hex text / payload.

Successful frames keep the original bytes, the selector value and exactly the
signals that were active.  Inactive signals have no observation row at all --
they are neither NULL values nor zeroes -- so they can never enter an
average's denominator.

Aggregation returns exact :class:`fractions.Fraction` sums and means plus the
identity of every contributing source frame, for the half-open window
``[start, end)``.

Storage layout
--------------

Text identifiers are normalised once (``products.id``, ``signals.id``) and the
4M-row observation table stores only integers, with the window key
``(product_id, signal_id, occurred_at_ns, frame_pk)`` as a ``WITHOUT ROWID``
primary key.  Values are stored as exact numerator/denominator pairs; they
fall back to decimal text only in the (rare) case where a definition's rational
does not fit SQLite's signed 64-bit INTEGER.  Raw payload bytes are always kept
on the frame row, so any accepted frame can be re-derived from its hex body.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import sqlite3
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from .catalog import (
    CatalogProduct,
    DecodeError,
    fraction_text,
)

STATUS_OK = "ok"
STATUS_REJECTED = "rejected"

_UTC = _dt.timezone.utc
_INT64_MAX = (1 << 63) - 1


class FrameError(ValueError):
    """A frame event is malformed before any catalog interpretation."""

    def __init__(self, message: str, *, position: int | None = None, code: str = "frame_error"):
        super().__init__(message)
        self.position = position
        self.code = code


# ---------------------------------------------------------------------------
# input shapes
# ---------------------------------------------------------------------------


def parse_occurred_ns(value: Any) -> int:
    """Exact event time in integer nanoseconds since the Unix epoch (UTC).

    ISO 8601 input keeps all nine fractional digits; ``datetime`` alone would
    truncate to microseconds, which would silently merge distinct frames at
    nanosecond resolution.
    """

    if isinstance(value, bool):
        raise ValueError("occurred_at must not be a bool")
    if isinstance(value, int):
        return value
    if isinstance(value, _dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=_UTC)
        delta = value - _dt.datetime(1970, 1, 1, tzinfo=_UTC)
        return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("occurred_at is empty")
        try:
            return int(text)
        except ValueError:
            pass
        parsed = _parse_iso_ns(text)
        if parsed is None:
            raise ValueError(f"occurred_at {value!r} is neither integer ns nor ISO 8601")
        return parsed
    raise ValueError(f"unsupported occurred_at type {type(value).__name__}")


_ISO_RE = re.compile(
    r"^(?P<y>\d{4})-(?P<mo>\d{2})-(?P<d>\d{2})[T ]"
    r"(?P<h>\d{2}):(?P<mi>\d{2}):(?P<s>\d{2})"
    r"(?:\.(?P<frac>\d{1,9}))?"
    r"(?P<tz>Z|[+-]\d{2}:?\d{2})?$"
)


def _parse_iso_ns(text: str) -> int | None:
    match = _ISO_RE.match(text)
    if match is None:
        return None
    y, mo, d = int(match["y"]), int(match["mo"]), int(match["d"])
    h, mi, s = int(match["h"]), int(match["mi"]), int(match["s"])
    frac = match["frac"] or ""
    nanos = int(frac.ljust(9, "0")) if frac else 0
    try:
        days = (_dt.date(y, mo, d) - _dt.date(1970, 1, 1)).days
    except ValueError:
        return None
    seconds = days * 86400 + h * 3600 + mi * 60 + s
    tz = match["tz"]
    if tz and tz != "Z":
        sign = 1 if tz[0] == "+" else -1
        body = tz[1:].replace(":", "")
        seconds -= sign * (int(body[:2]) * 3600 + int(body[2:]) * 60)
    return seconds * 1_000_000_000 + nanos


def format_ns(ns: int) -> str:
    """UTC ISO-8601 text with nanosecond precision.

    Day arithmetic is done in pure Python (Howard Hinnant's days-from-civil
    inverse) instead of calling ``fromtimestamp`` per frame; at 100k events
    that difference dominates ingestion time.
    """

    seconds, rem = divmod(ns, 1_000_000_000)
    days, sod = divmod(seconds, 86400)
    year, month, day = _civil_from_days(days)
    hour, sod = divmod(sod, 3600)
    minute, second = divmod(sod, 60)
    return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}.{rem:09d}Z"


def _civil_from_days(days: int) -> tuple[int, int, int]:
    z = days + 719468
    era = (z if z >= 0 else z - 146096) // 146097
    doe = z - era * 146097
    yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
    y = yoe + era * 400
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    d = doy - (153 * mp + 2) // 5 + 1
    m = mp + 3 if mp < 10 else mp - 9
    return (y + (1 if m <= 2 else 0), m, d)


def _parse_hex(payload_hex: Any) -> tuple[bytes | None, int | None, str | None]:
    """Return ``(payload, bad_position, error)``; position is a nibble index."""

    if isinstance(payload_hex, (bytes, bytearray, memoryview)):
        return bytes(payload_hex), None, None
    if not isinstance(payload_hex, str):
        return None, 0, "payload must be hex text or bytes"
    text = payload_hex.strip()
    if text.startswith(("0x", "0X")):
        text = text[2:]
    if len(text) % 2:
        return None, len(text), "hex body has an odd number of nibbles"
    for i, ch in enumerate(text):
        if ch not in "0123456789abcdefABCDEF":
            return None, i, f"invalid hex nibble {ch!r} at offset {i}"
    return bytes.fromhex(text), None, None


@dataclass(frozen=True)
class FrameEvent:
    frame_event_id: str
    bus: str | None
    frame_id: int
    occurred_at: Any
    payload_hex: Any

    @classmethod
    def coerce(cls, raw: "FrameEvent | Mapping[str, Any]") -> "FrameEvent":
        if isinstance(raw, FrameEvent):
            return raw
        if not isinstance(raw, Mapping):
            raise TypeError("frame event must be FrameEvent or mapping")
        if "occurred_at" in raw:
            occurred_at = raw["occurred_at"]
        elif "occurred_at_ns" in raw:
            occurred_at = raw["occurred_at_ns"]
        else:
            raise FrameError("frame event needs occurred_at or occurred_at_ns")
        return cls(
            frame_event_id=str(raw["frame_event_id"]),
            bus=raw.get("bus"),
            frame_id=int(raw["frame_id"]),
            occurred_at=occurred_at,
            payload_hex=raw["payload_hex"],
        )


@dataclass(frozen=True)
class FrameIdentity:
    frame_event_id: str
    bus: str
    frame_id: int
    occurred_at_ns: int
    occurred_at: str
    payload_hex: str
    selector: int | None


@dataclass(frozen=True)
class SignalAggregate:
    product_sha: str
    signal: str
    window_start_ns: int
    window_end_ns: int
    count: int
    total: Fraction
    mean: Fraction
    frames: tuple[FrameIdentity, ...] = ()

    def total_text(self) -> str:
        return fraction_text(self.total)

    def mean_text(self) -> str:
        return fraction_text(self.mean)

    def as_json(self) -> dict[str, Any]:
        return {
            "product_sha256": self.product_sha,
            "signal": self.signal,
            "window": [self.window_start_ns, self.window_end_ns],
            "count": self.count,
            "sum": self.total_text(),
            "mean": self.mean_text(),
            "frames": [
                {
                    "frame_event_id": f.frame_event_id,
                    "bus": f.bus,
                    "frame_id": f.frame_id,
                    "occurred_at_ns": f.occurred_at_ns,
                    "occurred_at": f.occurred_at,
                    "payload_hex": f.payload_hex,
                    "selector": f.selector,
                }
                for f in self.frames
            ],
        }


@dataclass(frozen=True)
class IngestStats:
    product_sha: str
    total: int = 0
    accepted: int = 0
    rejected: int = 0
    replaced: int = 0
    rejections: tuple[dict[str, Any], ...] = ()
    batch_size: int = 0

    def as_json(self) -> dict[str, Any]:
        return {
            "product_sha256": self.product_sha,
            "total": self.total,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "replaced": self.replaced,
            "batch_size": self.batch_size,
            "rejections": list(self.rejections),
        }


@dataclass(frozen=True)
class StoredFrame:
    frame_event_id: str
    bus: str
    frame_id: int
    occurred_at_ns: int
    occurred_at: str
    payload: bytes
    payload_hex: str
    status: str
    selector: int | None
    error_code: str | None
    error_position: int | None
    error_message: str | None
    signals: Mapping[str, tuple[int, Fraction]] = field(default_factory=dict)


@dataclass(frozen=True)
class StoredProduct:
    sha256: str
    protocol: str
    message_count: int
    created_at: str
    definitions: dict[str, Any]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE,
    protocol TEXT NOT NULL,
    message_count INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    definitions_json TEXT NOT NULL,
    manifest_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id),
    bus TEXT NOT NULL,
    frame_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    UNIQUE (product_id, bus, frame_id, name)
);

CREATE TABLE IF NOT EXISTS frames (
    pk INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id),
    frame_event_id TEXT NOT NULL,
    bus TEXT NOT NULL,
    frame_id INTEGER NOT NULL,
    occurred_at_ns INTEGER NOT NULL,
    occurred_at TEXT NOT NULL,
    payload BLOB,
    payload_hex TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ok','rejected')),
    selector INTEGER,
    error_code TEXT,
    error_position INTEGER,
    error_message TEXT,
    UNIQUE (product_id, frame_event_id)
);

CREATE TABLE IF NOT EXISTS observations (
    product_id INTEGER NOT NULL,
    signal_id INTEGER NOT NULL,
    occurred_at_ns INTEGER NOT NULL,
    frame_pk INTEGER NOT NULL REFERENCES frames(pk) ON DELETE CASCADE,
    raw INTEGER NOT NULL,
    value_num,
    value_den,
    PRIMARY KEY (product_id, signal_id, occurred_at_ns, frame_pk)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS ix_frames_window
    ON frames(product_id, occurred_at_ns, frame_event_id);
CREATE INDEX IF NOT EXISTS ix_frames_status
    ON frames(product_id, status, occurred_at_ns);
CREATE INDEX IF NOT EXISTS ix_frames_msg
    ON frames(product_id, bus, frame_id, occurred_at_ns);
CREATE INDEX IF NOT EXISTS ix_obs_frame
    ON observations(frame_pk);
"""


class ObservationWarehouse:
    """SQLite-backed store of frames interpreted against catalog products."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        # 128 MiB page cache: the observation insert is dominated by b-tree
        # writes, and the default 2 MiB cache thrashes at millions of rows.
        self._conn.execute("PRAGMA cache_size=-131072")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._products: dict[str, CatalogProduct] = {}
        self._product_ids: dict[str, int] = {}
        self._signal_ids: dict[tuple[str, str, int, str], int] = {}
        self._next_frame_pk: dict[int, int] = {}

    # -- lifecycle -------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ObservationWarehouse":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def reopen(self) -> "ObservationWarehouse":
        self.close()
        return ObservationWarehouse(self.path)

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    # -- products --------------------------------------------------------

    def register_product(self, product: CatalogProduct) -> str:
        manifest = product.manifest(embed_content=False)
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT id FROM products WHERE sha256 = ?", (product.fingerprint,)
            ).fetchone()
            if row is None:
                cursor = self._conn.execute(
                    "INSERT INTO products(sha256, protocol, message_count, created_at, definitions_json, manifest_json)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        product.fingerprint,
                        product.protocol,
                        len(product.messages),
                        _dt.datetime.now(_UTC).isoformat(),
                        json.dumps(product.content, sort_keys=True, ensure_ascii=False),
                        json.dumps(manifest, sort_keys=True, ensure_ascii=False),
                    ),
                )
                product_id = cursor.lastrowid
            else:
                product_id = row["id"]
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        self._products[product.fingerprint] = product
        self._product_ids[product.fingerprint] = product_id
        return product.fingerprint

    def _product_id(self, product: CatalogProduct | str | int) -> int:
        if isinstance(product, int) and not isinstance(product, bool):
            return product
        sha = product.fingerprint if isinstance(product, CatalogProduct) else product
        cached = self._product_ids.get(sha)
        if cached is not None:
            return cached
        row = self._conn.execute("SELECT id FROM products WHERE sha256 = ?", (sha,)).fetchone()
        if row is None:
            if isinstance(product, CatalogProduct):
                self.register_product(product)
                return self._product_ids[sha]
            raise KeyError(f"unknown product {sha}")
        self._product_ids[sha] = row["id"]
        return row["id"]

    def _signal_id(self, product_id: int, bus: str, frame_id: int, name: str) -> int:
        cached = self._signal_ids.get((str(product_id), bus, frame_id, name))
        if cached is not None:
            return cached
        row = self._conn.execute(
            "SELECT id FROM signals WHERE product_id = ? AND bus = ? AND frame_id = ? AND name = ?",
            (product_id, bus, frame_id, name),
        ).fetchone()
        if row is None:
            raise KeyError(
                f"signal {name!r} of {bus}/{frame_id} not registered for product id {product_id}"
            )
        self._signal_ids[(str(product_id), bus, frame_id, name)] = row["id"]
        return row["id"]

    def product(self, sha256: str) -> CatalogProduct:
        cached = self._products.get(sha256)
        if cached is not None:
            return cached
        row = self._conn.execute("SELECT definitions_json FROM products WHERE sha256 = ?", (sha256,)).fetchone()
        if row is None:
            raise KeyError(f"unknown product {sha256}")
        content = json.loads(row["definitions_json"])
        import hashlib

        blob = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        actual = hashlib.sha256(blob).hexdigest()
        if actual != sha256:
            raise ValueError(f"stored product {sha256} fails its content hash (computed {actual})")
        from .catalog import CatalogProduct as _CP, _compile_content

        compiled = _compile_content(content)
        product = _CP(fingerprint=sha256, messages=compiled, content=content)
        self._products[sha256] = product
        return product

    def products(self) -> list[StoredProduct]:
        rows = self._conn.execute(
            "SELECT sha256, protocol, message_count, created_at, definitions_json"
            " FROM products ORDER BY created_at, sha256"
        ).fetchall()
        return [
            StoredProduct(
                sha256=r["sha256"],
                protocol=r["protocol"],
                message_count=r["message_count"],
                created_at=r["created_at"],
                definitions=json.loads(r["definitions_json"]),
            )
            for r in rows
        ]

    # -- ingestion -------------------------------------------------------

    def ingest(
        self,
        product: CatalogProduct | str,
        events: Iterable[FrameEvent | Mapping[str, Any]],
        *,
        batch_size: int = 1000,
        on_duplicate: str = "replace",
    ) -> IngestStats:
        """Interpret events against one product and persist them.

        ``batch_size`` only controls transaction/memory granularity; results do
        not depend on it.  ``on_duplicate`` is ``replace`` (default), ``ignore``
        or ``error`` for repeated ``(product, frame_event_id)`` pairs.
        """

        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        if on_duplicate not in ("replace", "ignore", "error"):
            raise ValueError("on_duplicate must be replace/ignore/error")
        prod = self.product(product) if isinstance(product, str) else product
        self.register_product(prod)
        sha = prod.fingerprint
        product_id = self._product_id(prod)
        self._ensure_signals(prod, product_id)

        stats_total = 0
        accepted = 0
        rejected = 0
        replaced = 0
        rejections: list[dict[str, Any]] = []

        frame_buffer: list[tuple[Any, ...]] = []
        obs_buffer: list[tuple[Any, ...]] = []

        if product_id not in self._next_frame_pk:
            row = self._conn.execute("SELECT COALESCE(MAX(pk), 0) FROM frames").fetchone()
            self._next_frame_pk[product_id] = int(row[0]) + 1
        next_pk = self._next_frame_pk[product_id]

        def flush() -> None:
            nonlocal replaced, next_pk
            if not frame_buffer:
                return
            ids = [row[2] for row in frame_buffer]
            existing: dict[str, int] = {}
            for chunk_start in range(0, len(ids), 400):
                chunk = ids[chunk_start : chunk_start + 400]
                placeholders = ",".join("?" * len(chunk))
                rows = self._conn.execute(
                    "SELECT frame_event_id, pk FROM frames WHERE product_id = ?"
                    f" AND frame_event_id IN ({placeholders})",
                    (product_id, *chunk),
                ).fetchall()
                existing.update((r["frame_event_id"], r["pk"]) for r in rows)
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if existing:
                    if on_duplicate == "error":
                        raise sqlite3.IntegrityError(
                            f"duplicate frame_event_id {sorted(existing)[0]!r} for product {sha}"
                        )
                    if on_duplicate == "ignore":
                        frame_buffer[:] = [row for row in frame_buffer if row[2] not in existing]
                        obs_buffer[:] = [row for row in obs_buffer if row[3] not in existing]
                    else:
                        old_pks = sorted(existing.values())
                        for chunk_start in range(0, len(old_pks), 400):
                            chunk = old_pks[chunk_start : chunk_start + 400]
                            placeholders = ",".join("?" * len(chunk))
                            self._conn.execute(
                                f"DELETE FROM frames WHERE pk IN ({placeholders})", chunk
                            )
                        replaced += len(existing)
                if frame_buffer:
                    self._conn.executemany(
                        "INSERT INTO frames(pk, product_id, frame_event_id, bus, frame_id,"
                        " occurred_at_ns, occurred_at, payload, payload_hex, status, selector,"
                        " error_code, error_position, error_message)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        frame_buffer,
                    )
                    self._conn.executemany(
                        "INSERT INTO observations(product_id, signal_id, occurred_at_ns, frame_pk,"
                        " raw, value_num, value_den) VALUES (?,?,?,?,?,?,?)",
                        obs_buffer,
                    )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            self._next_frame_pk[product_id] = next_pk
            frame_buffer.clear()
            obs_buffer.clear()

        for raw_event in events:
            event = FrameEvent.coerce(raw_event)
            stats_total += 1
            frame_row, obs = self._interpret(prod, product_id, event, next_pk)
            next_pk += 1
            frame_buffer.append(frame_row)
            obs_buffer.extend(obs)
            if frame_row[9] == STATUS_OK:
                accepted += 1
            else:
                rejected += 1
                if len(rejections) < 50:
                    rejections.append(
                        {
                            "frame_event_id": event.frame_event_id,
                            "code": frame_row[11],
                            "position": frame_row[12],
                            "message": frame_row[13],
                        }
                    )
            if len(frame_buffer) >= batch_size:
                flush()
        flush()
        return IngestStats(
            product_sha=sha,
            total=stats_total,
            accepted=accepted,
            rejected=rejected,
            replaced=replaced,
            rejections=tuple(rejections),
            batch_size=batch_size,
        )

    def _ensure_signals(self, product: CatalogProduct, product_id: int) -> None:
        refs = [(m.bus, m.frame_id, s.name) for m in product.messages for s in m.signals]
        if not refs:
            return
        if all((str(product_id), *ref) in self._signal_ids for ref in refs):
            return
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.executemany(
                "INSERT OR IGNORE INTO signals(product_id, bus, frame_id, name) VALUES (?, ?, ?, ?)",
                [(product_id, bus, frame_id, name) for bus, frame_id, name in refs],
            )
            rows = self._conn.execute(
                "SELECT id, bus, frame_id, name FROM signals WHERE product_id = ?", (product_id,)
            ).fetchall()
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        for r in rows:
            self._signal_ids[(str(product_id), r["bus"], r["frame_id"], r["name"])] = r["id"]

    def _interpret(
        self, product: CatalogProduct, product_id: int, event: FrameEvent, frame_pk: int
    ) -> tuple[tuple[Any, ...], list[tuple[Any, ...]]]:
        """Return ``(frame_row, observation_rows)`` for one event."""

        def frame_row(
            *,
            occurred_ns: int,
            payload: bytes | None,
            status: str,
            selector: int | None,
            code: str | None,
            position: int | None,
            message: str | None,
        ) -> tuple[Any, ...]:
            return (
                frame_pk,
                product_id,
                event.frame_event_id,
                event.bus or "",
                int(event.frame_id),
                occurred_ns,
                format_ns(occurred_ns) if occurred_ns != 0 else "",
                payload,
                (
                    payload.hex()
                    if payload is not None
                    else (event.payload_hex if isinstance(event.payload_hex, str) else "")
                ),
                status,
                selector,
                code,
                position,
                message,
            )

        try:
            ns = parse_occurred_ns(event.occurred_at)
        except ValueError as exc:
            return (
                frame_row(
                    occurred_ns=0, payload=None, status=STATUS_REJECTED, selector=None,
                    code="bad_time", position=None, message=str(exc),
                ),
                [],
            )
        payload, bad_pos, bad_msg = _parse_hex(event.payload_hex)
        if payload is None:
            return (
                frame_row(
                    occurred_ns=ns, payload=None, status=STATUS_REJECTED, selector=None,
                    code="bad_encoding", position=bad_pos, message=bad_msg,
                ),
                [],
            )
        try:
            msg = (
                product.message(event.bus, event.frame_id)
                if event.bus is not None
                else product.message_by_frame_id(event.frame_id)
            )
        except DecodeError as exc:
            return (
                frame_row(
                    occurred_ns=ns, payload=payload, status=STATUS_REJECTED, selector=None,
                    code=exc.code, position=exc.position, message=str(exc),
                ),
                [],
            )
        if len(payload) != msg.payload_bytes:
            return (
                frame_row(
                    occurred_ns=ns, payload=payload, status=STATUS_REJECTED, selector=None,
                    code="bad_length", position=msg.payload_bytes,
                    message=f"payload length {len(payload)} != {msg.payload_bytes}",
                ),
                [],
            )
        try:
            selector, observations = msg.decode_observations(payload)
        except DecodeError as exc:
            return (
                frame_row(
                    occurred_ns=ns, payload=payload, status=STATUS_REJECTED, selector=None,
                    code=exc.code, position=exc.position, message=str(exc),
                ),
                [],
            )
        row = frame_row(
            occurred_ns=ns, payload=payload, status=STATUS_OK,
            selector=selector, code=None, position=None, message=None,
        )
        obs = []
        for name, raw, num, den in observations:
            signal_id = self._signal_ids[(str(product_id), msg.bus, msg.frame_id, name)]
            if -_INT64_MAX - 1 <= num <= _INT64_MAX and den <= _INT64_MAX:
                stored_num: Any = num
                stored_den: Any = den
            else:
                stored_num = str(num)
                stored_den = str(den)
            obs.append((product_id, signal_id, ns, frame_pk, raw, stored_num, stored_den))
        return row, obs

    # -- queries ---------------------------------------------------------

    def query_window(
        self,
        product: CatalogProduct | str,
        signal: str,
        start_ns: int,
        end_ns: int,
        *,
        include_frames: bool = True,
        bus: str | None = None,
        frame_id: int | None = None,
    ) -> SignalAggregate:
        """Exact count/sum/mean of ``signal`` over ``[start_ns, end_ns)``."""

        if isinstance(product, CatalogProduct):
            self.register_product(product)
            sha = product.fingerprint
        else:
            sha = product
        if end_ns < start_ns:
            raise ValueError("window end must be >= start")
        if not 0 <= start_ns <= _INT64_MAX or not 0 <= end_ns <= _INT64_MAX:
            raise ValueError("window bounds must fit signed 64-bit nanoseconds")
        product_id = self._product_id(sha)
        candidates = self._conn.execute(
            "SELECT id, bus, frame_id FROM signals WHERE product_id = ? AND name = ?",
            (product_id, signal),
        ).fetchall()
        if not candidates:
            return SignalAggregate(sha, signal, start_ns, end_ns, 0, Fraction(0), Fraction(0), ())
        if bus is not None:
            candidates = [c for c in candidates if c["bus"] == bus]
        if frame_id is not None:
            candidates = [c for c in candidates if c["frame_id"] == frame_id]
        if not candidates:
            return SignalAggregate(sha, signal, start_ns, end_ns, 0, Fraction(0), Fraction(0), ())
        if len(candidates) > 1:
            where = ", ".join(sorted(f"{c['bus']}/{c['frame_id']}" for c in candidates))
            raise ValueError(
                f"signal {signal!r} is defined by several messages ({where}); pass bus and frame_id"
            )
        signal_id = candidates[0]["id"]
        sql = (
            "SELECT o.frame_pk, o.raw, o.value_num, o.value_den,"
            "       f.frame_event_id, f.bus, f.frame_id, f.occurred_at_ns, f.occurred_at,"
            "       f.payload_hex, f.selector"
            " FROM observations o JOIN frames f ON f.pk = o.frame_pk"
            " WHERE o.product_id = ? AND o.signal_id = ?"
            "   AND o.occurred_at_ns >= ? AND o.occurred_at_ns < ?"
            "   AND f.status = 'ok'"
        )
        args: list[Any] = [product_id, signal_id, start_ns, end_ns]
        if bus is not None:
            sql += " AND f.bus = ?"
            args.append(bus)
        if frame_id is not None:
            sql += " AND f.frame_id = ?"
            args.append(frame_id)
        sql += " ORDER BY f.occurred_at_ns, f.frame_event_id"
        rows = self._conn.execute(sql, args).fetchall()
        total = Fraction(0)
        frames: list[FrameIdentity] = []
        for r in rows:
            total += Fraction(int(r["value_num"]), int(r["value_den"]))
            if include_frames:
                frames.append(
                    FrameIdentity(
                        frame_event_id=r["frame_event_id"],
                        bus=r["bus"],
                        frame_id=r["frame_id"],
                        occurred_at_ns=r["occurred_at_ns"],
                        occurred_at=r["occurred_at"],
                        payload_hex=r["payload_hex"],
                        selector=r["selector"],
                    )
                )
        count = len(rows)
        mean = total / count if count else Fraction(0)
        return SignalAggregate(
            product_sha=sha,
            signal=signal,
            window_start_ns=start_ns,
            window_end_ns=end_ns,
            count=count,
            total=total,
            mean=mean,
            frames=tuple(frames),
        )

    def signal_names(self, product: CatalogProduct | str) -> list[str]:
        """Observed signal names; names shared by several messages are de-duplicated.

        Use :meth:`signal_refs` to get the ``(bus, frame_id, name)`` identities.
        """

        product_id = self._product_id(product)
        rows = self._conn.execute(
            "SELECT DISTINCT s.name FROM signals s"
            " JOIN observations o ON o.signal_id = s.id AND o.product_id = s.product_id"
            " WHERE s.product_id = ? ORDER BY s.name",
            (product_id,),
        ).fetchall()
        return [r["name"] for r in rows]

    def signal_refs(self, product: CatalogProduct | str) -> list[tuple[str, int, str]]:
        """Every observed signal identity as ``(bus, frame_id, name)``."""

        product_id = self._product_id(product)
        rows = self._conn.execute(
            "SELECT DISTINCT s.bus, s.frame_id, s.name FROM signals s"
            " JOIN observations o ON o.signal_id = s.id AND o.product_id = s.product_id"
            " WHERE s.product_id = ? ORDER BY s.bus, s.frame_id, s.name",
            (product_id,),
        ).fetchall()
        return [(r["bus"], r["frame_id"], r["name"]) for r in rows]

    def frame(self, product: CatalogProduct | str, frame_event_id: str) -> StoredFrame:
        product_id = self._product_id(product)
        r = self._conn.execute(
            "SELECT * FROM frames WHERE product_id = ? AND frame_event_id = ?",
            (product_id, frame_event_id),
        ).fetchone()
        if r is None:
            raise KeyError(f"no frame {frame_event_id!r} for product {product}")
        obs = self._conn.execute(
            "SELECT s.name AS name, o.raw, o.value_num, o.value_den FROM observations o"
            " JOIN signals s ON s.id = o.signal_id"
            " WHERE o.product_id = ? AND o.frame_pk = ? ORDER BY s.name",
            (product_id, r["pk"]),
        ).fetchall()
        signals = {
            o["name"]: (o["raw"], Fraction(int(o["value_num"]), int(o["value_den"]))) for o in obs
        }
        return StoredFrame(
            frame_event_id=r["frame_event_id"],
            bus=r["bus"],
            frame_id=r["frame_id"],
            occurred_at_ns=r["occurred_at_ns"],
            occurred_at=r["occurred_at"],
            payload=r["payload"] if r["payload"] is not None else b"",
            payload_hex=r["payload_hex"],
            status=r["status"],
            selector=r["selector"],
            error_code=r["error_code"],
            error_position=r["error_position"],
            error_message=r["error_message"],
            signals=signals,
        )

    def frames(
        self,
        product: CatalogProduct | str,
        *,
        status: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> Iterator[StoredFrame]:
        product_id = self._product_id(product)
        sql = "SELECT frame_event_id FROM frames WHERE product_id = ?"
        args: list[Any] = [product_id]
        if status is not None:
            sql += " AND status = ?"
            args.append(status)
        sql += " ORDER BY occurred_at_ns, frame_event_id"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            args.extend([limit, offset])
        ids = [r["frame_event_id"] for r in self._conn.execute(sql, args).fetchall()]
        for fid in ids:
            yield self.frame(product_id, fid)

    def counts(self, product: CatalogProduct | str) -> dict[str, int]:
        product_id = self._product_id(product)
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM frames WHERE product_id = ? GROUP BY status",
            (product_id,),
        ).fetchall()
        out = {STATUS_OK: 0, STATUS_REJECTED: 0}
        for r in rows:
            out[r["status"]] = r["n"]
        out["total"] = out[STATUS_OK] + out[STATUS_REJECTED]
        out["observations"] = self._conn.execute(
            "SELECT COUNT(*) FROM observations WHERE product_id = ?", (product_id,)
        ).fetchone()[0]
        return out

    def decode_raw(self, product: CatalogProduct | str, payload: bytes, *, bus: str, frame_id: int):
        prod = self.product(product) if isinstance(product, str) else product
        return prod.message(bus, frame_id).decode(payload)


def frame_from_hex(
    product: CatalogProduct,
    payload_hex: str,
    *,
    bus: str,
    frame_id: int,
) -> dict[str, tuple[int, Fraction]]:
    """Rebuild a stored frame's active signals from its original hex body.

    Useful for audits: the warehouse rows must equal this for any accepted
    frame, because the product is immutable and the body is retained verbatim.
    """

    payload = bytes.fromhex(payload_hex)
    decoded = product.message(bus, frame_id).decode(payload)
    return {s.name: (s.raw, s.value) for s in decoded.signals}
