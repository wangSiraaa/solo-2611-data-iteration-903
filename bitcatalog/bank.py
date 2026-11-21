"""Idempotent local bank double.

The bank settles *instructions*.  Every instruction carries a stable
``instruction_id`` chosen by the cashier.  The bank may accept several rounds
of payment/refund instructions per day, may lose the connection after taking
an instruction (confirmation then arrives the next day), and may repeat a
receipt.

Guarantees implemented here
---------------------------

* The same id with the same content is registered and settled **once**; repeat
  deliveries return the existing receipt (``duplicate``).
* The same id with different content is rejected (``conflict``) and changes
  nothing.
* A registered instruction keeps its *original* status and receipt; an id that
  is definitely unknown can be re-delivered under the same id.
* Confirmation is idempotent: repeating it never settles a second time.
* Concurrent cashiers are serialised by SQLite and a UNIQUE id; two threads
  racing the same id produce exactly one registration.

Payment means money leaving the publisher to a payee.  Refund means money
returning from a payee.  The direction is carried by ``kind``; amounts are
always positive integers.

``BankDouble`` adds the fault injections needed to reproduce field incidents:
``disconnect_after_accept`` (registered but the caller never sees the ack) and
``disconnect_before_accept`` (nothing registered, safe to retry with the same
id), plus ``duplicate_receipt``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

KIND_PAYMENT = "payment"
KIND_REFUND = "refund"
KINDS = (KIND_PAYMENT, KIND_REFUND)

STATE_REGISTERED = "registered"
STATE_SETTLED = "settled"
STATE_FAILED = "failed"
STATE_REJECTED = "rejected"


class BankError(Exception):
    """Base class for local bank failures."""


class BankConflict(BankError):
    """The same instruction id arrived with different content."""

    def __init__(self, instruction_id: str, existing: "Instruction", incoming: "Instruction"):
        super().__init__(f"instruction {instruction_id!r} already registered with different content")
        self.existing = existing
        self.incoming = incoming


class BankDisconnected(BankError):
    """The caller lost the connection; ``accepted`` says whether it was taken."""

    def __init__(self, instruction_id: str, *, accepted: bool):
        super().__init__(
            f"connection lost {'after' if accepted else 'before'} accepting {instruction_id!r}"
        )
        self.instruction_id = instruction_id
        self.accepted = accepted


@dataclass(frozen=True)
class Instruction:
    instruction_id: str
    kind: str
    account_id: str
    book_id: str
    amount: int
    counterparty: str = ""

    def __post_init__(self) -> None:
        if not self.instruction_id:
            raise ValueError("instruction needs a non-empty instruction_id")
        if self.kind not in KINDS:
            raise ValueError(f"instruction kind must be one of {KINDS}")
        if not isinstance(self.amount, int) or isinstance(self.amount, bool) or self.amount <= 0:
            raise ValueError("instruction amount must be a positive integer")
        if not self.account_id or not self.book_id:
            raise ValueError("instruction needs account_id and book_id")

    def content(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "account_id": self.account_id,
            "book_id": self.book_id,
            "amount": self.amount,
            "counterparty": self.counterparty,
        }

    @property
    def content_hash(self) -> str:
        blob = json.dumps(self.content(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()

    def to_json(self) -> dict[str, Any]:
        return {"instruction_id": self.instruction_id, **self.content(),
                "content_hash": self.content_hash}


@dataclass(frozen=True)
class BankResult:
    outcome: str  # accepted / duplicate / conflict / idempotent
    instruction_id: str
    state: str
    content_hash: str
    receipt: dict[str, Any]

    @property
    def accepted(self) -> bool:
        return self.outcome in ("accepted", "idempotent")

    @property
    def ok(self) -> bool:
        return self.outcome != "conflict"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS instructions (
    instruction_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('payment','refund')),
    account_id TEXT NOT NULL,
    book_id TEXT NOT NULL,
    amount INTEGER NOT NULL CHECK (amount > 0),
    counterparty TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('registered','settled','failed')),
    accepted_at TEXT NOT NULL,
    confirmed_at TEXT,
    confirmations INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settlements (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    instruction_id TEXT NOT NULL UNIQUE REFERENCES instructions(instruction_id),
    kind TEXT NOT NULL,
    account_id TEXT NOT NULL,
    book_id TEXT NOT NULL,
    amount INTEGER NOT NULL,
    settled_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_instr_account ON instructions(account_id, state);
"""


def _now() -> str:
    import datetime as _dt

    return _dt.datetime.now(_dt.timezone.utc).isoformat()


class BankLedger:
    """SQLite-backed instruction store; safe for concurrent connections."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=30000")
        # Two connections opening a fresh file can race on the WAL switch and
        # schema creation; retry briefly instead of surfacing a transient lock.
        self._retry(lambda: self._conn.execute("PRAGMA journal_mode=WAL"))
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._retry(lambda: self._conn.executescript(_SCHEMA))

    @staticmethod
    def _retry(fn: Any, attempts: int = 20) -> Any:
        import time

        last: Exception | None = None
        for _ in range(attempts):
            try:
                return fn()
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise
                last = exc
                time.sleep(0.01)
        assert last is not None
        raise last

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "BankLedger":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- reads -----------------------------------------------------------

    def lookup(self, instruction_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM instructions WHERE instruction_id = ?", (instruction_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def registered(self, instruction_id: str) -> bool:
        return self.lookup(instruction_id) is not None

    def receipt(self, instruction_id: str) -> dict[str, Any]:
        row = self.lookup(instruction_id)
        if row is None:
            raise KeyError(f"instruction {instruction_id!r} is not registered")
        return {
            "instruction": {
                "instruction_id": row["instruction_id"],
                "kind": row["kind"],
                "account_id": row["account_id"],
                "book_id": row["book_id"],
                "amount": row["amount"],
                "counterparty": row["counterparty"],
                "content_hash": row["content_hash"],
            },
            "state": row["state"],
            "accepted_at": row["accepted_at"],
            "confirmed_at": row["confirmed_at"],
            "confirmations": row["confirmations"],
        }

    def list_instructions(self, *, account_id: str | None = None,
                          state: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM instructions"
        clauses = []
        args: list[Any] = []
        if account_id is not None:
            clauses.append("account_id = ?")
            args.append(account_id)
        if state is not None:
            clauses.append("state = ?")
            args.append(state)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY accepted_at, instruction_id"
        return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    # -- writes ----------------------------------------------------------

    def submit(self, instruction: Instruction) -> BankResult:
        """Register an instruction or return the existing one idempotently."""

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM instructions WHERE instruction_id = ?",
                    (instruction.instruction_id,),
                ).fetchone()
                if row is None:
                    self._conn.execute(
                        "INSERT INTO instructions(instruction_id, kind, account_id, book_id, amount,"
                        " counterparty, content_hash, state, accepted_at, confirmations)"
                        " VALUES (?,?,?,?,?,?,?,?,?,0)",
                        (
                            instruction.instruction_id,
                            instruction.kind,
                            instruction.account_id,
                            instruction.book_id,
                            instruction.amount,
                            instruction.counterparty,
                            instruction.content_hash,
                            STATE_REGISTERED,
                            _now(),
                        ),
                    )
                    outcome = "accepted"
                    state = STATE_REGISTERED
                    content_hash = instruction.content_hash
                else:
                    existing = _instruction_from_row(row)
                    if existing.content_hash != instruction.content_hash:
                        self._conn.execute("ROLLBACK")
                        raise BankConflict(instruction.instruction_id, existing, instruction)
                    outcome = "duplicate"
                    state = row["state"]
                    content_hash = row["content_hash"]
                self._conn.execute("COMMIT")
            except BankConflict:
                raise
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return BankResult(
            outcome=outcome,
            instruction_id=instruction.instruction_id,
            state=state,
            content_hash=content_hash,
            receipt=self.receipt(instruction.instruction_id),
        )

    def confirm(self, instruction_id: str, outcome: str = STATE_SETTLED) -> BankResult:
        """Confirm a registered instruction; idempotent and single-settling."""

        if outcome not in (STATE_SETTLED, STATE_FAILED):
            raise ValueError("confirmation outcome must be 'settled' or 'failed'")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM instructions WHERE instruction_id = ?", (instruction_id,)
                ).fetchone()
                if row is None:
                    self._conn.execute("ROLLBACK")
                    raise KeyError(f"instruction {instruction_id!r} is not registered")
                current = row["state"]
                if current == STATE_REGISTERED:
                    self._conn.execute(
                        "UPDATE instructions SET state = ?, confirmed_at = ?, confirmations = confirmations + 1"
                        " WHERE instruction_id = ? AND state = 'registered'",
                        (outcome, _now(), instruction_id),
                    )
                    if outcome == STATE_SETTLED:
                        self._conn.execute(
                            "INSERT OR IGNORE INTO settlements(instruction_id, kind, account_id, book_id,"
                            " amount, settled_at) VALUES (?,?,?,?,?,?)",
                            (
                                instruction_id,
                                row["kind"],
                                row["account_id"],
                                row["book_id"],
                                row["amount"],
                                _now(),
                            ),
                        )
                    result_outcome = "accepted"
                    final_state = outcome
                elif current == outcome:
                    # repeated receipt for an already-confirmed instruction
                    self._conn.execute(
                        "UPDATE instructions SET confirmations = confirmations + 1"
                        " WHERE instruction_id = ?",
                        (instruction_id,),
                    )
                    result_outcome = "idempotent"
                    final_state = current
                else:
                    self._conn.execute("ROLLBACK")
                    raise BankConflict(
                        instruction_id,
                        _instruction_from_row(row),
                        _instruction_from_row(row),
                    )
                self._conn.execute("COMMIT")
            except BankConflict:
                raise
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return BankResult(
            outcome=result_outcome,
            instruction_id=instruction_id,
            state=final_state,
            content_hash=row["content_hash"],
            receipt=self.receipt(instruction_id),
        )

    def settlements(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM settlements ORDER BY seq"
        ).fetchall()]


def _instruction_from_row(row: sqlite3.Row) -> Instruction:
    return Instruction(
        instruction_id=row["instruction_id"],
        kind=row["kind"],
        account_id=row["account_id"],
        book_id=row["book_id"],
        amount=row["amount"],
        counterparty=row["counterparty"],
    )


@dataclass
class Faults:
    """Reproducible field incidents for one bank client."""

    disconnect_after_accept: bool = False
    disconnect_before_accept: bool = False
    duplicate_receipt: bool = False


class BankDouble:
    """A bank client with switchable fault injection over one ledger file."""

    def __init__(self, path: str | Path, faults: Faults | None = None):
        self.ledger = BankLedger(path)
        self.faults = faults or Faults()

    def close(self) -> None:
        self.ledger.close()

    def __enter__(self) -> "BankDouble":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def submit(self, instruction: Instruction) -> BankResult:
        if self.faults.disconnect_before_accept:
            raise BankDisconnected(instruction.instruction_id, accepted=False)
        result = self.ledger.submit(instruction)
        if self.faults.disconnect_after_accept:
            raise BankDisconnected(instruction.instruction_id, accepted=True)
        return result

    def confirm(self, instruction_id: str, outcome: str = STATE_SETTLED) -> BankResult:
        result = self.ledger.confirm(instruction_id, outcome)
        if self.faults.duplicate_receipt:
            # A second identical receipt must be harmless; return the retained
            # original state rather than a second settlement.
            again = self.ledger.confirm(instruction_id, outcome)
            assert again.state == result.state
        return result

    def lookup(self, instruction_id: str) -> dict[str, Any] | None:
        return self.ledger.lookup(instruction_id)

    def receipt(self, instruction_id: str) -> dict[str, Any]:
        return self.ledger.receipt(instruction_id)

    def list_instructions(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self.ledger.list_instructions(**kwargs)


def verify_instruction(bank: BankDouble, instruction: Instruction) -> dict[str, Any]:
    """Can a cashier verify by id?  Returns a tri-state report."""

    row = bank.lookup(instruction.instruction_id)
    if row is None:
        return {"instruction_id": instruction.instruction_id, "registered": False,
                "retry_safe": True}
    if row["content_hash"] != instruction.content_hash:
        return {
            "instruction_id": instruction.instruction_id,
            "registered": True,
            "state": row["state"],
            "retry_safe": False,
            "conflict": True,
            "receipt": bank.receipt(instruction.instruction_id),
        }
    return {
        "instruction_id": instruction.instruction_id,
        "registered": True,
        "state": row["state"],
        "retry_safe": False,
        "receipt": bank.receipt(instruction.instruction_id),
    }
