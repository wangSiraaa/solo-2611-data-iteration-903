"""Contract claims, cap allocation, bank idempotency and cashier safety."""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bitcatalog.bank import (  # noqa: E402
    BankConflict,
    BankDisconnected,
    BankDouble,
    BankLedger,
    Faults,
    Instruction,
    verify_instruction,
)
from bitcatalog.contracts import (  # noqa: E402
    BookClaim,
    Contract,
    allocate_caps,
    evaluate_claims,
    target_obligations,
)
from bitcatalog.physical import Book, PhysicalResult  # noqa: E402
from bitcatalog.treasury import Treasury, safe_spendable  # noqa: E402


def contract(**overrides):
    base = {
        "contract_id": "C",
        "publisher": "pub",
        "responsible_party": "party",
        "period": "2026-11",
        "cap": 10,
        "required": ["P1"],
        "approved": ["P1", "P2"],
        "clauses": [
            {"clause": "1", "kind": "missing", "amount": 7},
            {"clause": "2", "kind": "wrong_order", "amount": 6},
            {"clause": "3", "kind": "unapproved_substitute", "amount": 2},
        ],
    }
    base.update(overrides)
    return Contract.parse(base)


def book(book_id, tags, *, seq=1, book_no=1, short=False):
    return Book(
        book_id=book_id, machine="m", book=book_no, table="1",
        sheet_tags=tuple(tags), sheet_frames=tuple(f"f-{book_id}-{i}" for i in range(len(tags))),
        product_tag=tags[-1] if tags else None, sealed_at_ns=seq, seal_seq=seq,
        seal_frame_event_id=f"seal-{book_id}", short=short,
    )


def claim(book_id, amount, kind, *, seq=1, book_no=1, clause="1", account="party|2026-11",
          required=("P1",)):
    return BookClaim(
        book_id=book_id, machine="m", book=book_no, seal_seq=seq,
        seal_frame_event_id=f"seal-{book_id}", contract_id="C", account_id=account,
        responsible_party="party", period="2026-11", required=tuple(required),
        actual=("P2",), product_tag="P2", short=False, issues=(kind,), clause=clause,
        amount=amount, withheld=False, payable=amount > 0,
    )


class ClaimDetection(unittest.TestCase):
    def test_missing_and_substitute(self):
        c = contract(approved=["P1"])
        b = book("A", ["P2"])
        self.assertEqual(c.issues_for(b), {"missing", "unapproved_substitute"})
        self.assertEqual(c.select_clause({"missing", "unapproved_substitute"}).amount, 7)

    def test_wrong_order(self):
        c = contract(required=["P1", "P2"], approved=["P1", "P2"])
        b = book("B", ["P2", "P1"])
        self.assertEqual(c.issues_for(b), {"wrong_order"})
        self.assertEqual(c.select_clause({"wrong_order"}).clause, "2")

    def test_highest_amount_and_smallest_clause_on_tie(self):
        c = Contract.parse({
            "contract_id": "C", "publisher": "p", "responsible_party": "r", "period": "x",
            "cap": 100, "required": ["P1"], "approved": ["P1"],
            "clauses": [
                {"clause": "9", "kind": "missing", "amount": 5},
                {"clause": "3", "kind": "wrong_order", "amount": 5},
                {"clause": "1", "kind": "unapproved_substitute", "amount": 4},
            ],
        })
        self.assertEqual(c.select_clause({"missing", "wrong_order", "unapproved_substitute"}).clause, "3")

    def test_correct_book_has_no_claim(self):
        c = contract()
        self.assertEqual(c.issues_for(book("C", ["P1"])), set())
        self.assertIsNone(c.select_clause(set()))

    def test_cap_allocation_order_and_partial(self):
        c = contract(cap=10)
        claims = [claim("B", 6, "wrong_order", seq=2, book_no=2, clause="2"),
                  claim("A", 7, "missing", seq=1, book_no=1)]
        allocs = allocate_caps(claims, [c])
        self.assertEqual([(a.book_id, a.allocated) for a in allocs], [("A", 7), ("B", 3)])
        self.assertEqual(target_obligations(claims, [c])[c.account_id], {"A": 7, "B": 3})

    def test_unclear_is_withheld(self):
        c = contract()
        unresolved = PhysicalResult(books=[], events=[], next_cursor={}, waste={},
                                    unclear_seqs=[("m", 1, 1)])
        b = book("A", ["P2"])
        # A book whose seal is at/after the unclear slot on the same table.
        claim_row = evaluate_claims(
            PhysicalResult(books=[b], events=[], next_cursor={}, waste={},
                           unclear_seqs=[("m", 1, 1)]),
            [c],
        )[0]
        self.assertTrue(claim_row.withheld)
        self.assertFalse(claim_row.payable)


class BankIdempotency(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "bank.db"
        self.bank = BankDouble(self.path)

    def tearDown(self):
        self.bank.close()
        self.tmp.cleanup()

    def test_same_id_same_content_settles_once(self):
        ins = Instruction("I1", "payment", "acct", "A", 4)
        self.assertEqual(self.bank.submit(ins).outcome, "accepted")
        self.assertEqual(self.bank.submit(ins).outcome, "duplicate")
        self.bank.confirm("I1")
        again = self.bank.confirm("I1")
        self.assertEqual(again.outcome, "idempotent")
        self.assertEqual(len(self.bank.ledger.settlements()), 1)

    def test_same_id_different_content_rejected(self):
        self.bank.submit(Instruction("I2", "payment", "acct", "A", 4))
        with self.assertRaises(BankConflict):
            self.bank.submit(Instruction("I2", "payment", "acct", "A", 5))
        self.assertEqual(self.bank.lookup("I2")["amount"], 4)

    def test_disconnect_after_accept_is_recoverable(self):
        bd = BankDouble(self.path, Faults(disconnect_after_accept=True))
        ins = Instruction("I3", "payment", "acct", "A", 4)
        with self.assertRaises(BankDisconnected):
            bd.submit(ins)
        bd.faults.disconnect_after_accept = False
        report = verify_instruction(bd, ins)
        self.assertTrue(report["registered"])
        self.assertEqual(bd.submit(ins).outcome, "duplicate")

    def test_disconnect_before_accept_is_safe_to_retry(self):
        bd = BankDouble(self.path, Faults(disconnect_before_accept=True))
        ins = Instruction("I4", "payment", "acct", "A", 4)
        with self.assertRaises(BankDisconnected):
            bd.submit(ins)
        self.assertIsNone(bd.lookup("I4"))
        bd.faults.disconnect_before_accept = False
        self.assertEqual(bd.submit(ins).outcome, "accepted")

    def test_duplicate_receipt_is_harmless(self):
        bd = BankDouble(self.path, Faults(duplicate_receipt=True))
        ins = Instruction("I5", "payment", "acct", "A", 4)
        bd.submit(ins)
        result = bd.confirm("I5")
        self.assertEqual(result.state, "settled")
        self.assertEqual(len(bd.ledger.settlements()), 1)

    def test_concurrent_same_instruction_settles_once(self):
        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name) / "c.db"
        outcomes = []

        def worker():
            ledger = BankLedger(path)
            try:
                outcomes.append(ledger.submit(Instruction("J1", "payment", "a", "B", 3)).outcome)
                ledger.confirm("J1")
            finally:
                ledger.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        with BankLedger(path) as ledger:
            self.assertEqual(len(ledger.list_instructions()), 1)
            self.assertEqual(len(ledger.settlements()), 1)
        self.assertEqual(sorted(outcomes), ["accepted", "duplicate"])
        tmp.cleanup()


class CashierSafety(unittest.TestCase):
    ACCOUNT = "party|2026-11"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bank = BankDouble(Path(self.tmp.name) / "cash.db")
        self.treasury = Treasury(self.bank)

    def tearDown(self):
        self.bank.close()
        self.tmp.cleanup()

    def test_ab_refund_flow_matches_worked_example(self):
        # B was already paid 6 under the old target.
        r = self.treasury.pay(account_id=self.ACCOUNT, book_id="B", amount=6)
        self.treasury.confirm(r["instruction_id"])
        targets = {"A": 7, "B": 3}

        plan = self.treasury.reconcile(account_id=self.ACCOUNT, obligations=targets, send=False)
        self.assertEqual([(d.book_id, d.action, d.amount) for d in plan],
                         [("A", "pay", 4), ("B", "refund", 3)])
        self.assertEqual(safe_spendable(self.treasury.account_view(self.ACCOUNT, targets)), 4)

        decisions = self.treasury.reconcile(account_id=self.ACCOUNT, obligations=targets)
        self.assertEqual([(d.book_id, d.action, d.amount) for d in decisions],
                         [("A", "pay", 4), ("B", "refund", 3)])
        # The 4 to A is committed; only the (still unconfirmed) refund of 3
        # remains, and it must not be spent yet.
        view = self.treasury.account_view(self.ACCOUNT, targets)
        self.assertEqual(safe_spendable(view), 0)

        # A repeat reconcile before any receipt moves no money.
        repeat = self.treasury.reconcile(account_id=self.ACCOUNT, obligations=targets)
        self.assertTrue(all(d.action == "none" for d in repeat))

        # Confirm A's 4 and B's refund of 3; A's remaining 3 becomes safe.
        for d in decisions:
            if d.action == "pay":
                self.treasury.confirm(d.instruction_id)
        refunds = [i for i in self.bank.list_instructions(account_id=self.ACCOUNT)
                   if i["kind"] == "refund"]
        for ref in refunds:
            self.treasury.confirm(ref["instruction_id"])
        self.assertEqual(safe_spendable(self.treasury.account_view(self.ACCOUNT, targets)), 3)

        final = self.treasury.reconcile(account_id=self.ACCOUNT, obligations=targets)
        self.assertEqual([(d.book_id, d.action, d.amount) for d in final], [("A", "pay", 3)])
        for d in final:
            self.treasury.confirm(d.instruction_id)
        view = self.treasury.account_view(self.ACCOUNT, targets)
        self.assertEqual(view["paid_by_book"], {"A": 7, "B": 3})
        self.assertEqual(view["net_paid"], 10)

    def test_manual_in_flight_counts_before_receipt(self):
        targets = {"A": 7}
        self.treasury.adopt("manual-A-4", kind="payment", account_id=self.ACCOUNT,
                            book_id="A", amount=4)
        # Manual 4 is pending, so only 3 more is issued now.
        decisions = self.treasury.reconcile(account_id=self.ACCOUNT, obligations=targets)
        self.assertEqual([(d.book_id, d.action, d.amount) for d in decisions], [("A", "pay", 3)])
        # No receipt yet: still only 4 committed + 3 pending = 7; nothing more.
        again = self.treasury.reconcile(account_id=self.ACCOUNT, obligations=targets)
        self.assertTrue(all(d.action == "none" for d in again))
        # Manual receipt clears; total reached with exactly 2 fee-bearing sends.
        self.treasury.confirm("manual-A-4")
        self.treasury.confirm(decisions[0].instruction_id)
        view = self.treasury.account_view(self.ACCOUNT, targets)
        self.assertEqual(view["paid_by_book"]["A"], 7)
        payments = [i for i in self.bank.list_instructions(account_id=self.ACCOUNT)
                    if i["kind"] == "payment"]
        self.assertEqual(len(payments), 2)

    def test_nothing_sent_can_pay_full_target_once(self):
        targets = {"A": 7}
        decisions = self.treasury.reconcile(account_id=self.ACCOUNT, obligations=targets)
        self.assertEqual([(d.book_id, d.action, d.amount) for d in decisions], [("A", "pay", 7)])
        self.treasury.confirm(decisions[0].instruction_id)
        payments = [i for i in self.bank.list_instructions(account_id=self.ACCOUNT)
                    if i["kind"] == "payment"]
        self.assertEqual(len(payments), 1)

    def test_cannot_spend_refund_before_confirmation(self):
        targets = {"A": 7, "B": 3}
        r = self.treasury.pay(account_id=self.ACCOUNT, book_id="B", amount=6)
        self.treasury.confirm(r["instruction_id"])
        self.treasury.reconcile(account_id=self.ACCOUNT, obligations=targets)
        # Before the refund confirms, total committed == cap: nothing spendable.
        view = self.treasury.account_view(self.ACCOUNT, targets)
        self.assertEqual(safe_spendable(view), 0)
        self.assertEqual(view["refunded_confirmed"], 0)

    def test_lost_ack_during_reconcile_does_not_double_send(self):
        with BankDouble(self.bank.ledger.path, Faults(disconnect_after_accept=True)) as faulty:
            treasury = Treasury(faulty)
            decisions = treasury.reconcile(account_id=self.ACCOUNT, obligations={"A": 7})
            self.assertEqual(decisions[0].bank_outcome, "disconnected")
            self.assertTrue(decisions[0].instruction_id)
            # The instruction was actually taken, so a retry reuses it.
            faulty.faults.disconnect_after_accept = False
            again = treasury.reconcile(account_id=self.ACCOUNT, obligations={"A": 7})
            self.assertTrue(all(d.action == "none" for d in again))
            payments = [i for i in faulty.list_instructions(account_id=self.ACCOUNT)
                        if i["kind"] == "payment"]
            self.assertEqual(len(payments), 1)
            self.assertEqual(payments[0]["amount"], 7)

    def test_manual_conflict_is_reported_not_overwritten(self):
        self.treasury.adopt("m1", kind="payment", account_id=self.ACCOUNT, book_id="A", amount=4)
        report = self.treasury.adopt("m1", kind="payment", account_id=self.ACCOUNT,
                                     book_id="A", amount=5)
        self.assertEqual(report["outcome"], "conflict")
        row = self.bank.lookup("m1")
        self.assertEqual(row["amount"], 4)


class ConcurrentReconcile(unittest.TestCase):
    ACCOUNT = "party|2026-11"

    def test_two_cashiers_same_state_pay_once(self):
        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name) / "shared.db"
        targets = {"A": 7}
        results = []
        lock = threading.Lock()

        def worker():
            with BankDouble(path) as bank:
                treasury = Treasury(bank)
                try:
                    decisions = treasury.reconcile(account_id=self.ACCOUNT, obligations=targets)
                except Exception as exc:  # noqa: BLE001
                    with lock:
                        results.append(("error", type(exc).__name__))
                    return
                with lock:
                    results.append(("ok", sum(d.amount for d in decisions if d.action == "pay")))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        with BankDouble(path) as bank:
            payments = [i for i in bank.list_instructions(account_id=self.ACCOUNT)
                        if i["kind"] == "payment"]
            # Regardless of interleaving, at most 7 in total is ever sent.
            self.assertLessEqual(sum(p["amount"] for p in payments), 7)
            self.assertEqual(len({p["instruction_id"] for p in payments}), len(payments))
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
