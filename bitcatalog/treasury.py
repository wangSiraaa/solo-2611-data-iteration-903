"""Cashier treasury: target obligations, already-paid money, safe spend.

The cashier keeps the *reliable physical evidence* (target obligations) apart
from *money the bank has actually paid*.  A published target can rise or fall
as late evidence and cap allocation move; until a refund is confirmed the
already-paid amount is still out of the account.

Definitions used throughout
---------------------------

``target``        allocated claim for a book from the latest reliable physical
                  evidence and cap allocation.
``paid_confirmed`` money the bank confirmed as settled (payments minus refunds).
``pending``       instructions the cashier sent that the bank has taken but not
                  yet confirmed; the money may already be gone, so it counts
                  against the available balance immediately.
``safe_to_spend`` ``min(account cap, paid_confirmed + uncommitted headroom)``
                  minus ``paid_confirmed`` -- i.e. the amount that can still be
                  sent now without ever exceeding the cap or re-spending money
                  that has not visibly come back.

The invariant the cashier enforces::

    paid_confirmed + pending_payments - pending_refunds + new_payment <= target

which is exactly "never spend the refund before it is confirmed".  The worked
A/B example follows from it: with target A=7, B=6, confirmed B=6 and a pending
B refund of 3, new payments to A are capped at 4 until the refund settles.
"""

from __future__ import annotations

import datetime as _dt
import threading
from dataclasses import dataclass
from typing import Any, Mapping

from .bank import (
    KIND_PAYMENT,
    KIND_REFUND,
    STATE_REGISTERED,
    STATE_SETTLED,
    BankDisconnected,
    BankDouble,
    Instruction,
    verify_instruction,
)


class TreasuryError(Exception):
    """An instruction would violate the cashier's accounting invariant."""


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


@dataclass
class Decision:
    book_id: str
    account_id: str
    target: int
    already_paid: int
    already_refunded: int
    pending_payment: int
    pending_refund: int
    safe_extra: int
    action: str  # pay / refund / none
    amount: int
    sent: bool
    instruction_id: str | None
    bank_outcome: str | None
    note: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "book_id": self.book_id,
            "account_id": self.account_id,
            "target": self.target,
            "already_paid": self.already_paid,
            "already_refunded": self.already_refunded,
            "pending_payment": self.pending_payment,
            "pending_refund": self.pending_refund,
            "safe_extra": self.safe_extra,
            "action": self.action,
            "amount": self.amount,
            "sent": self.sent,
            "instruction_id": self.instruction_id,
            "bank_outcome": self.bank_outcome,
            "note": self.note,
        }


class Treasury:
    """Cashier-side control of one bank-based account set.

    ``obligations`` is ``account_id -> {book_id: target_amount}``.  Every
    payment/refund instruction is generated from the (book, direction) pair
    with a deterministic id so two concurrent cashiers produce the same id and
    the bank settles once.
    """

    def __init__(self, bank: BankDouble, *, fee_per_payment: int = 0):
        if fee_per_payment < 0:
            raise ValueError("fee_per_payment must be >= 0")
        self.bank = bank
        self.fee_per_payment = fee_per_payment
        # Serialise a cashier's read-decide-write within this process.  Across
        # processes the content-derived instruction id + UNIQUE bank key still
        # collapse duplicate sends to one settlement.
        self._lock = threading.RLock()

    # -- money view ------------------------------------------------------

    def account_view(self, account_id: str, obligations: Mapping[str, int] | None = None) -> dict[str, Any]:
        obligations = dict(obligations or {})
        rows = self.bank.list_instructions(account_id=account_id)
        paid = 0
        refunded = 0
        pending_payment = 0
        pending_refund = 0
        by_book_paid: dict[str, int] = {}
        by_book_pending: dict[str, int] = {}
        by_book_pending_refund: dict[str, int] = {}
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            latest[row["instruction_id"]] = {
                "instruction_id": row["instruction_id"],
                "kind": row["kind"],
                "book_id": row["book_id"],
                "amount": row["amount"],
                "state": row["state"],
            }
            if row["state"] == STATE_SETTLED:
                if row["kind"] == KIND_PAYMENT:
                    paid += row["amount"]
                    by_book_paid[row["book_id"]] = by_book_paid.get(row["book_id"], 0) + row["amount"]
                else:
                    refunded += row["amount"]
                    by_book_paid[row["book_id"]] = by_book_paid.get(row["book_id"], 0) - row["amount"]
            elif row["state"] == STATE_REGISTERED:
                if row["kind"] == KIND_PAYMENT:
                    pending_payment += row["amount"]
                    by_book_pending[row["book_id"]] = by_book_pending.get(row["book_id"], 0) + row["amount"]
                else:
                    pending_refund += row["amount"]
                    by_book_pending_refund[row["book_id"]] = by_book_pending_refund.get(row["book_id"], 0) + row["amount"]
        target = sum(obligations.values())
        net_paid = paid - refunded
        # Unconfirmed refunds are NOT available: the money has not visibly
        # come back, so it must not be spent again.  Unconfirmed payments DO
        # count against the cap because they may already have left.
        committed = net_paid + pending_payment
        safe_extra = target - committed
        fees = self.fee_per_payment * sum(1 for r in rows if r["kind"] == KIND_PAYMENT and r["state"] == STATE_SETTLED)
        return {
            "account_id": account_id,
            "target": target,
            "paid_confirmed": paid,
            "refunded_confirmed": refunded,
            "net_paid": net_paid,
            "pending_payment": pending_payment,
            "pending_refund": pending_refund,
            "committed": committed,
            "safe_extra": safe_extra,
            "fee_total": fees,
            "books": obligations,
            "paid_by_book": by_book_paid,
            "pending_by_book": by_book_pending,
            "pending_refund_by_book": by_book_pending_refund,
            "instructions": latest,
        }

    # -- id generation ---------------------------------------------------

    @staticmethod
    def instruction_id(account_id: str, book_id: str, kind: str, amount: int, round_no: int = 0) -> str:
        """Stable, content-derived id for one tranche.

        The id names ``(account, book, direction, amount)`` and an explicit
        ordinal.  Re-sending the *same* tranche (e.g. after a lost ack) reuses
        the id and content, so the bank settles once.  A genuinely distinct
        second tranche of the same amount must pass a new ``round_no``.
        """

        return f"{account_id}|{book_id}|{kind}|{amount}#{round_no}"

    def _next_round(self, account_id: str, book_id: str, kind: str) -> int:
        """Deterministic tranche ordinal from the ledger.

        Two cashiers reading the same state derive the same ordinal and amount;
        the bank's UNIQUE id then collapses them to one settlement.  A genuinely
        new top-up sees a larger count and gets a fresh ordinal.
        """

        rows = self.bank.list_instructions(account_id=account_id)
        return sum(1 for r in rows if r["book_id"] == book_id and r["kind"] == kind)

    # -- instructions ----------------------------------------------------

    def _submit(self, instruction: Instruction) -> dict[str, Any]:
        existing = verify_instruction(self.bank, instruction)
        if existing.get("registered"):
            if existing.get("conflict"):
                return {"outcome": "conflict", "instruction_id": instruction.instruction_id,
                        "registered": True, "state": existing.get("state")}
            return {"outcome": "existing", "instruction_id": instruction.instruction_id,
                    "registered": True, "state": existing.get("state")}
        try:
            result = self.bank.submit(instruction)
        except BankDisconnected as exc:
            registered = self.bank.lookup(instruction.instruction_id) is not None
            return {
                "outcome": "disconnected",
                "instruction_id": instruction.instruction_id,
                "accepted": exc.accepted,
                "registered": registered,
            }
        return {"outcome": result.outcome, "instruction_id": instruction.instruction_id,
                "registered": True, "state": result.state}

    def pay(self, *, account_id: str, book_id: str, amount: int, target: int | None = None,
            counterparty: str = "", round_no: int | None = None) -> dict[str, Any]:
        """Send a payment tranche for a book.

        With ``round_no=None`` the ordinal is derived from the ledger, so two
        cashiers in the same state generate the same id and the bank settles
        once.  Pass an explicit ordinal to make a retry byte-for-byte stable.
        """

        if amount <= 0:
            raise TreasuryError("payment amount must be positive")
        ordinal = self._next_round(account_id, book_id, KIND_PAYMENT) if round_no is None else round_no
        instruction = Instruction(
            instruction_id=self.instruction_id(account_id, book_id, KIND_PAYMENT, amount, ordinal),
            kind=KIND_PAYMENT,
            account_id=account_id,
            book_id=book_id,
            amount=amount,
            counterparty=counterparty,
        )
        return self._submit(instruction)

    def refund(self, *, account_id: str, book_id: str, amount: int,
               counterparty: str = "", round_no: int | None = None) -> dict[str, Any]:
        if amount <= 0:
            raise TreasuryError("refund amount must be positive")
        ordinal = self._next_round(account_id, book_id, KIND_REFUND) if round_no is None else round_no
        instruction = Instruction(
            instruction_id=self.instruction_id(account_id, book_id, KIND_REFUND, amount, ordinal),
            kind=KIND_REFUND,
            account_id=account_id,
            book_id=book_id,
            amount=amount,
            counterparty=counterparty,
        )
        return self._submit(instruction)

    # -- reconciliation / decision --------------------------------------

    def adopt(self, instruction_id: str, *, kind: str, account_id: str, book_id: str,
              amount: int, counterparty: str = "") -> dict[str, Any]:
        """Record a cashier's previously manual instruction so it is tracked.

        Idempotent: the same id/content returns ``existing``; a conflicting
        reuse is ``conflict``.  A manual instruction that is still unconfirmed
        then counts as pending, so the cashier will not pay the same amount
        twice before the receipt arrives.
        """

        instruction = Instruction(
            instruction_id=instruction_id,
            kind=kind,
            account_id=account_id,
            book_id=book_id,
            amount=amount,
            counterparty=counterparty,
        )
        return self._submit(instruction)

    def reconcile(self, *, account_id: str, obligations: Mapping[str, int], send: bool = True,
                  counterparty: str = "") -> list[Decision]:
        """Pay/refund each book towards its target when it is safe to do so.

        Per book, a payment equals ``target - paid - pending``; the account-level
        ``safe_extra`` additionally caps the sum of all new payments.  A pending
        (unconfirmed) refund is not spendable until it is confirmed; a pending
        payment already counts as committed.  Running reconcile twice without
        new confirmations is therefore a no-op for already-covered books, and
        concurrent cashiers hit the same instruction ids.
        """

        decisions: list[Decision] = []

        with self._lock:
            view = self.account_view(account_id, obligations)
            books = sorted(set(obligations) | set(view["paid_by_book"]))
            safe_extra = view["safe_extra"]
            for book_id in books:
                target = int(obligations.get(book_id, 0))
                paid = view["paid_by_book"].get(book_id, 0)
                pending_pay = view["pending_by_book"].get(book_id, 0)
                pending_ref = view["pending_refund_by_book"].get(book_id, 0)
                payable_now = target - paid - pending_pay

                if payable_now > 0:
                    amount = min(payable_now, safe_extra)
                    if not send:
                        decisions.append(Decision(
                            book_id=book_id, account_id=account_id, target=target,
                            already_paid=paid, already_refunded=view["refunded_confirmed"],
                            pending_payment=pending_pay, pending_refund=pending_ref,
                            safe_extra=safe_extra, action="pay", amount=amount, sent=False,
                            instruction_id=None, bank_outcome=None, note="dry run",
                        ))
                    elif amount > 0:
                        result = self.pay(
                            account_id=account_id, book_id=book_id, amount=amount,
                            target=target, counterparty=counterparty,
                        )
                        if result.get("outcome") == "accepted":
                            safe_extra -= amount
                        decisions.append(Decision(
                            book_id=book_id, account_id=account_id, target=target,
                            already_paid=paid, already_refunded=view["refunded_confirmed"],
                            pending_payment=pending_pay, pending_refund=pending_ref,
                            safe_extra=safe_extra, action="pay", amount=amount,
                            sent=result.get("outcome") in ("accepted", "existing", "duplicate"),
                            instruction_id=result.get("instruction_id"),
                            bank_outcome=result.get("outcome"),
                            note=None if result.get("registered", True) else "not registered",
                        ))
                    else:
                        decisions.append(Decision(
                            book_id=book_id, account_id=account_id, target=target,
                            already_paid=paid, already_refunded=view["refunded_confirmed"],
                            pending_payment=pending_pay, pending_refund=pending_ref,
                            safe_extra=safe_extra, action="none", amount=0, sent=False,
                            instruction_id=None, bank_outcome=None,
                            note="waiting for a confirmed refund before spending again",
                        ))
                elif payable_now < 0 and pending_ref <= 0:
                    excess = -payable_now
                    result = None
                    if send:
                        result = self.refund(
                            account_id=account_id, book_id=book_id, amount=excess,
                            counterparty=counterparty,
                        )
                    decisions.append(Decision(
                        book_id=book_id, account_id=account_id, target=target,
                        already_paid=paid, already_refunded=view["refunded_confirmed"],
                        pending_payment=pending_pay, pending_refund=pending_ref,
                        safe_extra=safe_extra, action="refund", amount=excess,
                        sent=result is not None and result.get("outcome") in ("accepted", "existing", "duplicate"),
                        instruction_id=result.get("instruction_id") if result else None,
                        bank_outcome=result.get("outcome") if result else None,
                        note=None if send else "dry run",
                    ))
                elif payable_now < 0:
                    decisions.append(Decision(
                        book_id=book_id, account_id=account_id, target=target,
                        already_paid=paid, already_refunded=view["refunded_confirmed"],
                        pending_payment=pending_pay, pending_refund=pending_ref,
                        safe_extra=safe_extra, action="none", amount=0, sent=False,
                        instruction_id=None, bank_outcome=None,
                        note="refund already pending; not spending until it confirms",
                    ))
        return decisions

    def confirm(self, instruction_id: str, outcome: str = STATE_SETTLED) -> dict[str, Any]:
        return self.bank.confirm(instruction_id, outcome).receipt


def safe_spendable(view: Mapping[str, Any]) -> int:
    """The single number the cashier may still instruct right now."""

    return max(0, int(view["safe_extra"]))
