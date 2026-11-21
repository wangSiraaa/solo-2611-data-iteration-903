"""Reproducible worked examples for the bindery claims and cashier flow.

Run ``python3.11 tools/demo_claims.py``.  Everything is deterministic and
printed step by step: physical reconstructions, contract claims, cap
allocation, bank instructions, receipts and the cashier's safe-to-spend
numbers before and after a refund confirmation.

Scenarios
---------

1. Table P1/P2: ``pickup, pull_last, pickup, seal`` -> P2 (payable) versus the
   same later readings with the first frame being ``empty_punch`` -> P1 (not
   payable: P1 is correct per contract).
2. Books A and B: A seals first; B was already paid 6; new physical evidence
   makes A claim 7 and B claim 6 under a cap of 10.  Target allocation is
   A=7, B=3.  While B's refund of 3 is unconfirmed at most 4 more is safe.
3. Two cashier start states: (a) a manual 4 to A already sent with no receipt,
   (b) nothing sent.  Shows when A's first tranche lands and the eventual
   number of fee-bearing payments.
4. Two cashiers resume the same unknown instruction concurrently, then reopen
   and reconcile a late confirmation.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bitcatalog.bank import BankDouble, Faults, BankDisconnected  # noqa: E402
from bitcatalog.catalog import build_product  # noqa: E402
from bitcatalog.contracts import (  # noqa: E402
    Contract,
    allocate_caps,
    evaluate_claims,
    target_obligations,
)
from bitcatalog.physical import Load, PhysicalLedger  # noqa: E402
from bitcatalog.readout import ReadoutDefinition  # noqa: E402
from bitcatalog.treasury import Treasury, safe_spendable  # noqa: E402

BINDERY = {
    "bus": "bindery",
    "frame_id": 10,
    "payload_bytes": 4,
    "selector": {"name": "act", "start": 0, "width": 3, "order": "little"},
    "signals": [
        {"name": "table", "start": 3, "width": 8, "order": "little", "signed": False,
         "factor": "1", "offset": "0"},
        {"name": "seq", "start": 11, "width": 8, "order": "little", "signed": False,
         "factor": "1", "offset": "0"},
        {"name": "book", "start": 19, "width": 8, "order": "little", "signed": False,
         "factor": "1", "offset": "0"},
    ],
}

ACTIONS = {"0": "pickup", "1": "empty_punch", "2": "pull_last", "3": "seal"}


def _setup():
    product = build_product([BINDERY])
    definition = ReadoutDefinition.parse({
        "product_sha256": product.fingerprint,
        "bus": "bindery",
        "frame_id": 10,
        "actions": ACTIONS,
        "table_signal": "table",
        "machine_seq_signal": "seq",
        "book_signal": "book",
        "selector_name": "act",
    })
    return product, definition.compile(product)


def _frame(codec, frame_event_id, selector, seq, *, book=0, table=1, arrival=None):
    payload = codec.message.encode(
        {"table": table, "seq": seq, "book": book}, selector=selector
    ).payload
    return codec.decode(
        payload,
        frame_event_id=frame_event_id,
        occurred_at_ns=seq if arrival is None else arrival,
    )


def scenario_table() -> None:
    print("\n=== 1. material table P1/P2 ===")
    _, codec = _setup()
    loads = [Load("1", 0, "P1"), Load("1", 1, "P2")]

    real = [
        _frame(codec, "f0", 0, 1),  # pickup
        _frame(codec, "f1", 2, 2),  # pull_last
        _frame(codec, "f2", 0, 3),  # pickup
        _frame(codec, "f3", 3, 4, book=1),  # seal
    ]
    result = PhysicalLedger(loads).reconstruct(real)
    book = result.books[0]
    print(f"real pickup -> sheet_tags={book.sheet_tags} product={book.product_tag} short={book.short}")

    contract = Contract.parse({
        "contract_id": "C", "publisher": "pub", "responsible_party": "bindery",
        "period": "2026-11", "cap": 7,
        "required": ["P1"], "approved": ["P1"],
        "clauses": [{"clause": "1", "kind": "missing", "amount": 7},
                    {"clause": "2", "kind": "wrong_order", "amount": 7},
                    {"clause": "3", "kind": "unapproved_substitute", "amount": 3}],
    })
    claims = evaluate_claims(result, [contract])
    print(f"claim for P2 book: issues={claims[0].issues} amount={claims[0].amount} payable={claims[0].payable}")

    empty = [
        _frame(codec, "g0", 1, 1),  # empty punch
        _frame(codec, "g1", 2, 2),  # pull_last (nothing to pull)
        _frame(codec, "g2", 0, 3),  # pickup
        _frame(codec, "g3", 3, 4, book=1),  # seal
    ]
    result2 = PhysicalLedger(loads).reconstruct(empty)
    book2 = result2.books[0]
    claims2 = evaluate_claims(result2, [contract])
    print(f"empty punch -> sheet_tags={book2.sheet_tags} product={book2.product_tag} short={book2.short}")
    print(f"claim: issues={claims2[0].issues} amount={claims2[0].amount} payable={claims2[0].payable}")


def scenario_caps_and_cash(tmpdir: str) -> None:
    print("\n=== 2. A/B cap allocation and cash flow ===")
    contract = Contract.parse({
        "contract_id": "C", "publisher": "pub", "responsible_party": "party",
        "period": "2026-11", "cap": 10,
        "required": ["P1"], "approved": ["P1", "P2"],
        "clauses": [{"clause": "1", "kind": "missing", "amount": 7},
                    {"clause": "2", "kind": "wrong_order", "amount": 6},
                    {"clause": "3", "kind": "unapproved_substitute", "amount": 2}],
    })
    # A claim 7 (missing), B claim 6 (wrong order) -- built directly.
    from bitcatalog.contracts import BookClaim

    claims = [
        BookClaim("A", "m", 1, 1, "s1", "C", contract.account_id, "party", "2026-11",
                  ("P1",), ("P2",), "P2", False, ("missing",), "1", 7, False, True),
        BookClaim("B", "m", 2, 2, "s2", "C", contract.account_id, "party", "2026-11",
                  ("P1",), ("P2", "P1"), "P1", False, ("wrong_order",), "2", 6, False, True),
    ]
    allocs = allocate_caps(claims, [contract])
    print("allocations:", [(a.book_id, a.claim_amount, a.allocated) for a in allocs])
    targets = target_obligations(claims, [contract])[contract.account_id]
    print("targets:", targets)

    path = os.path.join(tmpdir, "ab.db")
    with BankDouble(path) as bank:
        treasury = Treasury(bank)
        # Earlier target: only B=6.  B was paid and confirmed.
        r = treasury.pay(account_id=contract.account_id, book_id="B", amount=6)
        bank.confirm(r["instruction_id"], "settled")
        print("B already paid+confirmed 6")

        # Dry run shows the safe amount while B's refund receipt is outstanding.
        plan = treasury.reconcile(account_id=contract.account_id, obligations=targets, send=False)
        print("dry-run (no send):", [(d.book_id, d.action, d.amount) for d in plan])
        print("safe to pay before refund confirms:",
              safe_spendable(treasury.account_view(contract.account_id, targets)))

        decisions = treasury.reconcile(account_id=contract.account_id, obligations=targets)
        print("reconcile after new evidence:",
              [(d.book_id, d.action, d.amount, d.bank_outcome) for d in decisions])
        view = treasury.account_view(contract.account_id, targets)
        print("safe_extra while B refund unconfirmed:", safe_spendable(view))
        print("A paid so far:", view["paid_by_book"].get("A", 0),
              "B net:", view["paid_by_book"].get("B", 0))

        again = treasury.reconcile(account_id=contract.account_id, obligations=targets)
        print("second reconcile (refund pending):",
              [(d.book_id, d.action, d.amount) for d in again])

        # Confirm A's first tranche and then B's refund.
        for d in decisions:
            if d.instruction_id and d.action == "pay":
                treasury.confirm(d.instruction_id, outcome="settled")
        refunds = [i for i in bank.list_instructions(account_id=contract.account_id)
                   if i["kind"] == "refund"]
        for ref in refunds:
            treasury.confirm(ref["instruction_id"], outcome="settled")
        print("B refund confirmed 3")
        print("safe to pay after refund confirms:",
              safe_spendable(treasury.account_view(contract.account_id, targets)))
        final = treasury.reconcile(account_id=contract.account_id, obligations=targets)
        print("reconcile after refund:", [(d.book_id, d.action, d.amount, d.bank_outcome) for d in final])
        for d in final:
            if d.instruction_id:
                treasury.confirm(d.instruction_id, "settled")
        view = treasury.account_view(contract.account_id, targets)
        print("final net paid:", view["net_paid"], "by book:", view["paid_by_book"])
        print("fee-bearing payments:", sum(1 for i in bank.list_instructions(
            account_id=contract.account_id) if i["kind"] == "payment" and i["state"] == "settled"))


def scenario_two_cashier_starts(tmpdir: str) -> None:
    print("\n=== 3. A's first tranche under the same A/B cap-10 condition ===")
    contract = Contract.parse({
        "contract_id": "C", "publisher": "pub", "responsible_party": "party",
        "period": "2026-11", "cap": 10,
        "required": ["P1"], "approved": ["P1", "P2"],
        "clauses": [{"clause": "1", "kind": "missing", "amount": 7},
                    {"clause": "2", "kind": "wrong_order", "amount": 6}],
    })
    account = contract.account_id
    # Targets A=7, B=3; B was already paid 6 and confirmed.
    targets = {"A": 7, "B": 3}
    fee = 1

    for label, manual_four in (("manual 4 in flight", True), ("nothing sent", False)):
        path = os.path.join(tmpdir, f"start-{label.replace(' ', '_')}.db")
        with BankDouble(path) as bank:
            treasury = Treasury(bank, fee_per_payment=fee)
            b_paid = treasury.pay(account_id=account, book_id="B", amount=6)
            treasury.confirm(b_paid["instruction_id"], outcome="settled")
            if manual_four:
                # A cashier sent 4 manually and never got the receipt.  Adopt it
                # by its original id so it counts as pending, not as paid.
                treasury.adopt("manual-A-4", kind="payment", account_id=account,
                               book_id="A", amount=4)

            view0 = treasury.account_view(account, targets)
            print(f"\n[{label}] safe before any new send: {safe_spendable(view0)}")
            first = treasury.reconcile(account_id=account, obligations=targets)
            print(f"[{label}] first reconcile:",
                  [(d.book_id, d.action, d.amount, d.bank_outcome) for d in first])
            a_first = view0["pending_by_book"].get("A", 0) if manual_four else \
                sum(d.amount for d in first if d.book_id == "A" and d.action == "pay")
            print(f"[{label}] A first tranche = {a_first} "
                  f"({'already in flight' if manual_four else 'sent now, on the first reconcile'})")

            # Confirm what was sent, but NOT B's refund yet.
            if manual_four:
                treasury.confirm("manual-A-4", outcome="settled")
            for d in first:
                if d.instruction_id and d.action == "pay":
                    treasury.confirm(d.instruction_id, outcome="settled")

            view1 = treasury.account_view(account, targets)
            print(f"[{label}] safe before B refund confirms: {safe_spendable(view1)}")
            # Confirm B's refund of 3, then top A up to 7.
            refunds = [i for i in bank.list_instructions(account_id=account)
                       if i["kind"] == "refund"]
            for ref in refunds:
                treasury.confirm(ref["instruction_id"], outcome="settled")
            view2 = treasury.account_view(account, targets)
            print(f"[{label}] safe after B refund confirms: {safe_spendable(view2)}")
            second = treasury.reconcile(account_id=account, obligations=targets)
            print(f"[{label}] top-up:", [(d.book_id, d.action, d.amount, d.bank_outcome)
                                         for d in second])
            for d in second:
                if d.instruction_id:
                    treasury.confirm(d.instruction_id, outcome="settled")

            final = treasury.account_view(account, targets)
            payments = [i for i in bank.list_instructions(account_id=account)
                        if i["kind"] == "payment"]
            print(f"[{label}] A total={final['paid_by_book'].get('A', 0)}, "
                  f"net paid={final['net_paid']}, total fee-bearing payments={len(payments)}, "
                  f"fees borne by publisher={final['fee_total']}")


def scenario_concurrent_resume(tmpdir: str) -> None:
    print("\n=== 4. two cashiers resume one unknown instruction; reopen + late confirmation ===")
    import threading

    path = os.path.join(tmpdir, "resume.db")
    with BankDouble(path, Faults(disconnect_after_accept=True)) as bank:
        from bitcatalog.bank import Instruction

        unknown = Instruction("R1", "payment", "party|2026-11", "A", 4)
        try:
            bank.submit(unknown)
        except BankDisconnected:
            print("cashier 1 lost the ack; instruction may or may not exist")

    outcomes = []

    def cashier(i):
        with BankDouble(path) as bank:
            from bitcatalog.bank import Instruction, verify_instruction

            ins = Instruction("R1", "payment", "party|2026-11", "A", 4)
            report = verify_instruction(bank, ins)
            if report["registered"]:
                bank.confirm("R1", "settled")
                outcomes.append(("found", report["state"]))
            else:
                outcomes.append(("retry", bank.submit(ins).outcome))

    threads = [threading.Thread(target=cashier, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print("concurrent outcomes:", outcomes)

    # Reopen and reconcile a late confirmation (must not double pay).
    with BankDouble(path) as bank:
        print("after reopen:", [(r["instruction_id"], r["state"]) for r in bank.list_instructions()])
        late = bank.confirm("R1", "settled")
        print("late confirmation:", late.outcome, late.state)
        print("payments on record:", len([i for i in bank.list_instructions()
                                          if i["kind"] == "payment"]))


def scenario_reconcile(tmpdir: str) -> None:
    print("\n=== 5. explain today's difference from yesterday's records ===")
    from bitcatalog.reconcile import Inventory, Snapshot, explain_difference

    contract = Contract.parse({
        "contract_id": "C", "publisher": "pub", "responsible_party": "party",
        "period": "2026-11", "cap": 10,
        "required": ["P1"], "approved": ["P1"],
        "clauses": [{"clause": "1", "kind": "missing", "amount": 7}],
    })
    account = contract.account_id
    path = os.path.join(tmpdir, "reconcile.db")

    def snap(label, bank, treasury, targets, inventory):
        return Snapshot.capture(
            label=label, taken_at=label,
            inventory=Inventory.of(inventory),
            contracts=[contract], claims=[],
            targets={account: targets},
            instructions=bank.list_instructions(account_id=account),
            money={account: treasury.account_view(account, targets)},
        )

    with BankDouble(path) as bank:
        treasury = Treasury(bank)
        # Yesterday: only B known, paid 6; table held P1,P2.
        r = treasury.pay(account_id=account, book_id="B", amount=6)
        treasury.confirm(r["instruction_id"], "settled")
        yesterday = snap("yesterday", bank, treasury, {"B": 6}, {"1": ["P1", "P2"]})

        # Today a late frame shows the table actually held P1,P3 and a new book
        # A exists; targets become A=7, B=3.
        today = snap("today", bank, treasury, {"A": 7, "B": 3}, {"1": ["P1", "P3"]})
        diff = explain_difference(yesterday, today)
        print("target changes:", diff["target_changes"])
        print("inventory changes:", diff["inventory_changes"])
        print("money changes:", diff["money"])
        saved = today.save(os.path.join(tmpdir, "today.json"))
        reloaded = Snapshot.load(saved)
        print("reload reproduces explanation:", explain_difference(yesterday, reloaded) == diff)


def main() -> int:
    scenario_table()
    with tempfile.TemporaryDirectory() as tmp:
        scenario_caps_and_cash(tmp)
        scenario_two_cashier_starts(tmp)
        scenario_concurrent_resume(tmp)
        scenario_reconcile(tmp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
