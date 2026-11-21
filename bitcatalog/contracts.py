"""Contract claims and per-responsible-party cap allocation.

A contract states the required physical sheet order (batch tags) for a book,
which batches are approved, and a clause table.  Each clause has a number, a
kind and an amount::

    {"clause": "1", "kind": "missing", "amount": 7}
    {"clause": "2", "kind": "wrong_order", "amount": 7}
    {"clause": "3", "kind": "unapproved_substitute", "amount": 3}

Detected issue kinds:

``missing``                a required batch is absent from the book;
``wrong_order``            the required batches are all present but out of order;
``unapproved_substitute``  the book contains a batch that is not approved.

Per book only the highest applicable amount counts; equal amounts fall back to
the smaller clause number.  A book whose seal or contributing sheets involve an
unclear (unresolved contradictory) frame is withheld: unclear evidence is never
a payment basis.

Caps apply to one responsible party in one period.  Claims for that
``(responsible_party, period)`` are allocated the cap in ``(seal sequence,
book number)`` order: each book gets ``min(remaining_cap, claim_amount)``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from .physical import Book, PhysicalResult

KIND_MISSING = "missing"
KIND_WRONG_ORDER = "wrong_order"
KIND_UNAPPROVED_SUBSTITUTE = "unapproved_substitute"
ISSUE_KINDS = (KIND_MISSING, KIND_WRONG_ORDER, KIND_UNAPPROVED_SUBSTITUTE)


def _clause_sort_key(number: str) -> tuple[int, Any]:
    try:
        return (0, int(number))
    except (TypeError, ValueError):
        return (1, str(number))


@dataclass(frozen=True)
class Clause:
    clause: str
    kind: str
    amount: int

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> "Clause":
        if not isinstance(raw, Mapping):
            raise ValueError("clause must be an object")
        number = str(raw.get("clause"))
        if not number:
            raise ValueError("clause needs a number")
        kind = raw.get("kind")
        if kind not in ISSUE_KINDS:
            raise ValueError(f"unknown clause kind {kind!r}")
        amount = raw.get("amount")
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
            raise ValueError(f"clause {number} amount must be a non-negative integer")
        return cls(clause=number, kind=kind, amount=amount)


@dataclass(frozen=True)
class Contract:
    contract_id: str
    publisher: str
    responsible_party: str
    period: str
    cap: int
    required: tuple[str, ...]
    approved: tuple[str, ...]
    clauses: tuple[Clause, ...]

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> "Contract":
        if not isinstance(raw, Mapping):
            raise ValueError("contract must be an object")
        for field_name in ("contract_id", "publisher", "responsible_party", "period"):
            if not isinstance(raw.get(field_name), str) or not raw[field_name]:
                raise ValueError(f"contract needs a non-empty {field_name}")
        cap = raw.get("cap", 0)
        if isinstance(cap, bool) or not isinstance(cap, int) or cap < 0:
            raise ValueError("cap must be a non-negative integer")
        required = tuple(str(t) for t in raw.get("required", ()))
        approved = tuple(str(t) for t in raw.get("approved", required))
        clauses = tuple(Clause.parse(c) for c in raw.get("clauses", ()))
        if not clauses:
            raise ValueError("contract needs at least one clause")
        return cls(
            contract_id=raw["contract_id"],
            publisher=raw["publisher"],
            responsible_party=raw["responsible_party"],
            period=raw["period"],
            cap=cap,
            required=required,
            approved=approved,
            clauses=clauses,
        )

    @classmethod
    def load(cls, path: str | Path) -> "Contract":
        return cls.parse(json.loads(Path(path).read_text(encoding="utf-8")))

    def save(self, path: str | Path) -> str:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_json(), indent=1, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return str(target)

    def to_json(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "publisher": self.publisher,
            "responsible_party": self.responsible_party,
            "period": self.period,
            "cap": self.cap,
            "required": list(self.required),
            "approved": list(self.approved),
            "clauses": [
                {"clause": c.clause, "kind": c.kind, "amount": c.amount} for c in self.clauses
            ],
        }

    @property
    def account_id(self) -> str:
        return f"{self.responsible_party}|{self.period}"

    # -- issue detection -------------------------------------------------

    def issues_for(self, book: Book) -> set[str]:
        actual = list(book.sheet_tags)
        required = list(self.required)
        issues: set[str] = set()
        if set(required) - set(actual):
            issues.add(KIND_MISSING)
        if sorted(actual) == sorted(required) and actual != required:
            issues.add(KIND_WRONG_ORDER)
        approved = set(self.approved)
        if any(tag not in approved for tag in actual):
            issues.add(KIND_UNAPPROVED_SUBSTITUTE)
        return issues

    def select_clause(self, issues: set[str]) -> Clause | None:
        applicable = [c for c in self.clauses if c.kind in issues]
        if not applicable:
            return None
        best = max(applicable, key=lambda c: c.amount)
        tied = [c for c in applicable if c.amount == best.amount]
        return min(tied, key=lambda c: _clause_sort_key(c.clause))


@dataclass(frozen=True)
class BookClaim:
    book_id: str
    machine: str
    book: int | None
    seal_seq: int
    seal_frame_event_id: str
    contract_id: str
    account_id: str
    responsible_party: str
    period: str
    required: tuple[str, ...]
    actual: tuple[str, ...]
    product_tag: str | None
    short: bool
    issues: tuple[str, ...]
    clause: str | None
    amount: int
    withheld: bool
    payable: bool
    reason: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "book_id": self.book_id,
            "machine": self.machine,
            "book": self.book,
            "seal_seq": self.seal_seq,
            "seal_frame_event_id": self.seal_frame_event_id,
            "contract_id": self.contract_id,
            "account_id": self.account_id,
            "responsible_party": self.responsible_party,
            "period": self.period,
            "required": list(self.required),
            "actual": list(self.actual),
            "product_tag": self.product_tag,
            "short": self.short,
            "issues": list(self.issues),
            "clause": self.clause,
            "amount": self.amount,
            "withheld": self.withheld,
            "payable": self.payable,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Allocation:
    book_id: str
    account_id: str
    seal_seq: int
    book: int | None
    claim_amount: int
    allocated: int
    remaining_before: int
    remaining_after: int
    responsible_party: str = ""
    period: str = ""

    def as_json(self) -> dict[str, Any]:
        return {
            "book_id": self.book_id,
            "account_id": self.account_id,
            "seal_seq": self.seal_seq,
            "book": self.book,
            "claim_amount": self.claim_amount,
            "allocated": self.allocated,
            "remaining_before": self.remaining_before,
            "remaining_after": self.remaining_after,
            "responsible_party": self.responsible_party,
            "period": self.period,
        }


def evaluate_claims(
    result: PhysicalResult,
    contracts: Iterable[Contract],
    *,
    contract_for: Mapping[str, str] | None = None,
    default_contract: str | None = None,
) -> list[BookClaim]:
    """Assess every sealed book against its contract.

    ``contract_for`` maps ``book_id`` to a ``contract_id``; books not mapped use
    ``default_contract`` (or the first contract when only one is given).
    """

    contracts = list(contracts)
    by_id = {c.contract_id: c for c in contracts}
    contract_for = dict(contract_for or {})
    if default_contract is None and len(contracts) == 1:
        default_contract = contracts[0].contract_id

    # Unclear slots can shift the table cursor; poison only books on that
    # machine/table whose seal comes at or after the earliest unclear slot.
    unclear_floor: dict[tuple[str, str], int] = {}
    for machine, seq, table in result.unclear_seqs:
        key = (machine, str(table))
        unclear_floor[key] = min(unclear_floor.get(key, seq), seq)

    claims: list[BookClaim] = []
    for book in result.books:
        contract_id = contract_for.get(book.book_id, default_contract)
        if contract_id is None or contract_id not in by_id:
            raise ValueError(f"no contract for book {book.book_id!r}")
        contract = by_id[contract_id]

        contributing = set(book.sheet_frames) | {book.seal_frame_event_id}
        poisoned = any(not result.frame_is_clear(fid) for fid in contributing)
        floor = unclear_floor.get((book.machine, book.table))
        # A contradiction at or before this seal could have moved the cursor
        # and changed which batch this book received.
        if floor is not None and book.seal_seq >= floor:
            poisoned = True

        issues = contract.issues_for(book)
        clause = contract.select_clause(issues)
        amount = clause.amount if clause else 0
        withheld = poisoned
        payable = (not withheld) and amount > 0
        reason = None
        if withheld:
            reason = "unresolved contradictory frames affect this book"
        claims.append(
            BookClaim(
                book_id=book.book_id,
                machine=book.machine,
                book=book.book,
                seal_seq=book.seal_seq,
                seal_frame_event_id=book.seal_frame_event_id,
                contract_id=contract.contract_id,
                account_id=contract.account_id,
                responsible_party=contract.responsible_party,
                period=contract.period,
                required=contract.required,
                actual=book.sheet_tags,
                product_tag=book.product_tag,
                short=book.short,
                issues=tuple(sorted(issues)),
                clause=clause.clause if clause else None,
                amount=amount,
                withheld=withheld,
                payable=payable,
                reason=reason,
            )
        )
    return claims


def allocate_caps(claims: Iterable[BookClaim], contracts: Iterable[Contract]) -> list[Allocation]:
    """Allocate each account's cap in ``(seal seq, book number)`` order."""

    caps = {c.account_id: c.cap for c in contracts}
    grouped: dict[str, list[BookClaim]] = {}
    for claim in claims:
        if not claim.payable:
            continue
        grouped.setdefault(claim.account_id, []).append(claim)

    out: list[Allocation] = []
    for account_id, group in grouped.items():
        cap = caps.get(account_id)
        if cap is None:
            raise ValueError(f"no contract defines cap for account {account_id!r}")
        group.sort(
            key=lambda c: (c.seal_seq, c.book if c.book is not None else c.seal_seq, c.book_id)
        )
        remaining = cap
        for claim in group:
            before = remaining
            allocated = min(remaining, claim.amount)
            remaining -= allocated
            out.append(
                Allocation(
                    book_id=claim.book_id,
                    account_id=account_id,
                    seal_seq=claim.seal_seq,
                    book=claim.book,
                    claim_amount=claim.amount,
                    allocated=allocated,
                    remaining_before=before,
                    remaining_after=remaining,
                    responsible_party=claim.responsible_party,
                    period=claim.period,
                )
            )
    out.sort(key=lambda a: (a.account_id, a.seal_seq, a.book if a.book is not None else a.seal_seq, a.book_id))
    return out


def target_obligations(
    claims: Iterable[BookClaim], contracts: Iterable[Contract]
) -> dict[str, dict[str, int]]:
    """``account_id -> {book_id: allocated target}`` for zero-claim books excluded."""

    result: dict[str, dict[str, int]] = {}
    for allocation in allocate_caps(claims, contracts):
        if allocation.allocated:
            result.setdefault(allocation.account_id, {})[allocation.book_id] = allocation.allocated
    return result


def claims_and_allocations(
    result: Any, contracts: Iterable[Contract], **kwargs: Any
) -> tuple[list[BookClaim], list[Allocation], dict[str, dict[str, int]]]:
    """Convenience: evaluate, allocate and derive targets in one pass."""

    contracts = list(contracts)
    claims = evaluate_claims(result, contracts, **kwargs)
    allocations = allocate_caps(claims, contracts)
    targets: dict[str, dict[str, int]] = {}
    for allocation in allocations:
        if allocation.allocated:
            targets.setdefault(allocation.account_id, {})[allocation.book_id] = allocation.allocated
    return claims, allocations, targets
