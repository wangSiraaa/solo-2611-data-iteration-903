"""Unit and property tests for the catalog codec and overlap proof."""

from __future__ import annotations

import json
import random
import unittest
from fractions import Fraction
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bitcatalog.catalog import (  # noqa: E402
    CatalogError,
    CompileError,
    DecodeError,
    build_product,
    load_product,
)
from bitcatalog.reference import (  # noqa: E402
    ref_decode,
    ref_encode,
    ref_overlap_pairs,
    ref_unassigned_bits,
)


def little(name, start, width, *, signed=False, factor="1", offset="0", branches=None):
    sig = {
        "name": name,
        "start": start,
        "width": width,
        "order": "little",
        "signed": signed,
        "factor": factor,
        "offset": offset,
    }
    if branches is not None:
        sig["branches"] = branches
    return sig


def big(name, byte, bit, width, *, signed=False, factor="1", offset="0", branches=None):
    sig = {
        "name": name,
        "start": [byte, bit],
        "width": width,
        "order": "big",
        "signed": signed,
        "factor": factor,
        "offset": offset,
    }
    if branches is not None:
        sig["branches"] = branches
    return sig


def msg(signals, payload_bytes=8, selector=None, frame_id=1, bus="b"):
    return {
        "bus": bus,
        "frame_id": frame_id,
        "payload_bytes": payload_bytes,
        "selector": selector,
        "signals": signals,
    }


class CrossByteBitOrder(unittest.TestCase):
    def test_little_cross_byte_is_contiguous_low_to_high(self):
        definition = msg([little("v", 4, 12)])
        product = build_product([definition])
        compiled = product.messages[0]
        # Value 0xABC: low nibble C -> byte0 bits 4..7 (high nibble), then
        # 0xAB -> byte1 low-to-high.  Physical byte order is therefore C0 AB.
        frame = compiled.encode({"v": 0xABC})
        self.assertEqual(frame.payload_hex, "c0ab" + "00" * 6)
        self.assertEqual(compiled.decode(frame.payload).by_name()["v"].raw, 0xABC)

    def test_big_cross_byte_starts_at_msb(self):
        definition = msg([big("v", 0, 4, 12)])
        product = build_product([definition])
        compiled = product.messages[0]
        # 0xABC = 1010 1011 1100.  The top nibble 1010 lands in byte 0 bits
        # 3..0 (0x0A), the remaining 8 bits fill byte 1 (0xBC).
        frame = compiled.encode({"v": 0xABC})
        self.assertEqual(frame.payload[0], 0x0A)
        self.assertEqual(frame.payload[1], 0xBC)
        self.assertEqual(compiled.decode(frame.payload).by_name()["v"].raw, 0xABC)

    def test_big_full_byte_position_zero_is_msb(self):
        definition = msg([big("v", 0, 0, 8)])
        product = build_product([definition])
        compiled = product.messages[0]
        frame = compiled.encode({"v": 0x81})
        self.assertEqual(frame.payload[0], 0x81)
        self.assertEqual(compiled.decode(bytes([0x81] + [0] * 7)).by_name()["v"].raw, 0x81)
        # bit 1 of the byte is the 0x40 mask, not 0x02
        self.assertEqual(compiled.decode(bytes([0x40] + [0] * 7)).by_name()["v"].raw, 0x40)

    def test_big_width_one_lowest_bit_is_lsb_of_byte(self):
        definition = msg([big("v", 0, 7, 1)])
        product = build_product([definition])
        compiled = product.messages[0]
        self.assertEqual(compiled.decode(bytes([0x01] + [0] * 7)).by_name()["v"].raw, 1)
        self.assertEqual(compiled.decode(bytes([0x80] + [0] * 7)).by_name()["v"].raw, 0)


class SignedBoundaries(unittest.TestCase):
    def test_little_signed_minimum_cross_byte(self):
        definition = msg([little("v", 3, 13, signed=True)])
        product = build_product([definition])
        compiled = product.messages[0]
        frame = compiled.encode({"v": -4096})
        decoded = compiled.decode(frame.payload).by_name()["v"]
        self.assertEqual(decoded.raw, -4096)
        self.assertEqual(decoded.value, Fraction(-4096))

    def test_big_signed_extremes(self):
        definition = msg([big("v", 1, 3, 17, signed=True, factor="1/8", offset="-2")])
        product = build_product([definition])
        compiled = product.messages[0]
        for raw in (-(1 << 16), -1, 0, 1, (1 << 16) - 1):
            frame = compiled.encode({"v": raw})
            decoded = compiled.decode(frame.payload).by_name()["v"]
            self.assertEqual(decoded.raw, raw)
            self.assertEqual(decoded.value, Fraction(raw, 8) - 2)
            # reference agrees bit-for-bit
            sel, raws, values = ref_decode(definition, frame.payload)
            self.assertEqual(raws["v"], raw)
            self.assertEqual(values["v"], Fraction(raw, 8) - 2)

    def test_unsigned_cannot_go_negative(self):
        definition = msg([little("v", 0, 4)])
        compiled = build_product([definition]).messages[0]
        with self.assertRaises(DecodeError):
            compiled.encode({"v": -1})


class BranchReuse(unittest.TestCase):
    def setUp(self):
        self.definition = msg(
            [
                little("always", 4, 8),
                little("a", 12, 4, branches=[0]),
                little("b", 12, 4, factor="2", branches=[1]),
                little("c", 12, 8, branches=[2, 3]),
            ],
            payload_bytes=4,
            selector={"name": "sel", "start": 0, "width": 4, "order": "little"},
        )

    def test_same_bits_per_branch(self):
        product = build_product([self.definition])
        compiled = product.messages[0]
        expected = {
            0: {"always", "a"},
            1: {"always", "b"},
            2: {"always", "c"},
            3: {"always", "c"},
            4: {"always"},
        }
        for selector, names in expected.items():
            payload = compiled.encode(
                {"always": 7} | ({"a": 3} if selector == 0 else {"b": 3} if selector == 1 else {"c": 3} if selector in (2, 3) else {}),
                selector=selector,
            ).payload
            decoded = compiled.decode(payload)
            self.assertEqual({s.name for s in decoded.signals}, names)
            sel, raws, values = ref_decode(self.definition, payload)
            self.assertEqual(sel, selector)
            self.assertEqual(set(raws), names)
        inactive = compiled.branch_map[1]
        self.assertNotIn(1, inactive)

    def test_inactive_signal_rejected_on_encode(self):
        compiled = build_product([self.definition]).messages[0]
        # Branch 1 is active only for "b"; supplying "a" as well is rejected
        # (missing "b" is reported first, then supplying an inactive signal).
        with self.assertRaises(DecodeError) as ctx:
            compiled.encode({"always": 1, "a": 1}, selector=1)
        self.assertEqual(ctx.exception.code, "missing_signal")
        with self.assertRaises(DecodeError) as ctx:
            compiled.encode({"always": 1, "a": 1, "b": 1}, selector=1)
        self.assertEqual(ctx.exception.code, "inactive_signal")
        with self.assertRaises(DecodeError) as ctx:
            compiled.encode({"always": 1}, selector=0)
        self.assertEqual(ctx.exception.code, "missing_signal")

    def test_subset_branches_still_overlap_checked(self):
        bad = msg(
            [
                little("always", 4, 8),
                little("a", 12, 4, branches=[0, 1]),
                little("b", 12, 4, branches=[1]),
            ],
            payload_bytes=4,
            selector={"name": "sel", "start": 0, "width": 4, "order": "little"},
        )
        with self.assertRaises(CompileError) as ctx:
            build_product([bad])
        self.assertEqual(ctx.exception.code, "signal_overlap")


class OverlapProof(unittest.TestCase):
    def test_cross_order_overlap_detected(self):
        # little bits 0..8 (byte0 all, byte1 bit0) vs big at (0,6) width 4:
        # big positions are byte0 bits 6,5,4,3 -- inside the little field.
        definition = msg(
            [little("a", 0, 9), big("b", 0, 6, 4)],
            payload_bytes=4,
        )
        with self.assertRaises(CompileError) as ctx:
            build_product([definition])
        self.assertEqual(ctx.exception.code, "signal_overlap")
        self.assertEqual(len(ref_overlap_pairs(definition)), 1)

    def test_selector_overlap_detected(self):
        definition = msg(
            [little("a", 3, 4)],
            payload_bytes=2,
            selector={"name": "sel", "start": 0, "width": 4, "order": "little"},
        )
        with self.assertRaises(CompileError) as ctx:
            build_product([definition])
        self.assertEqual(ctx.exception.code, "selector_overlap")

    def test_big_selector_overlap_detected(self):
        definition = msg(
            [little("a", 0, 8)],
            payload_bytes=2,
            selector={"name": "sel", "start": [0, 0], "width": 8, "order": "big"},
        )
        with self.assertRaises(CompileError):
            build_product([definition])

    def test_payload_overrun_rejected(self):
        for definition in (
            msg([little("a", 60, 8)], payload_bytes=8),
            msg([big("a", 7, 3, 8)], payload_bytes=8),
            msg([little("a", 0, 33)], payload_bytes=8),
        ):
            with self.assertRaises(CatalogError):
                build_product([definition])

    def test_selector_must_be_unscaled(self):
        for selector in (
            {"name": "s", "start": 0, "width": 4, "order": "little", "factor": "2"},
            {"name": "s", "start": 0, "width": 4, "order": "little", "signed": True},
            {"name": "s", "start": 0, "width": 4, "order": "little", "offset": "1"},
        ):
            with self.assertRaises(CatalogError):
                build_product([msg([little("a", 8, 8)], payload_bytes=2, selector=selector)])

    def test_zero_factor_rejected(self):
        with self.assertRaises(CatalogError):
            build_product([msg([little("a", 0, 4, factor="0")], payload_bytes=2)])

    def test_duplicate_names_and_messages_rejected(self):
        with self.assertRaises(CatalogError):
            build_product([msg([little("a", 0, 4), little("a", 8, 4)], payload_bytes=2)])
        with self.assertRaises(CompileError):
            build_product([msg([little("a", 0, 4)]), msg([little("b", 8, 4)])])


class RationalScaling(unittest.TestCase):
    def test_exact_rational_values(self):
        definition = msg(
            [
                little("i", 0, 10, signed=True, factor="1/1000", offset="-1/3"),
                big("f", 2, 2, 13, factor="-5/7", offset="1000/9"),
            ],
            payload_bytes=5,
        )
        product = build_product([definition])
        compiled = product.messages[0]
        payload = compiled.encode({"i": -257, "f": 1234}).payload
        decoded = compiled.decode(payload).by_name()
        self.assertEqual(decoded["i"].value, Fraction(-257, 1000) - Fraction(1, 3))
        self.assertEqual(decoded["f"].value, Fraction(1234) * Fraction(-5, 7) + Fraction(1000, 9))
        _, raws, values = ref_decode(definition, payload)
        self.assertEqual(values["i"], decoded["i"].value)
        self.assertEqual(values["f"], decoded["f"].value)

    def test_float_and_decimal_text_agree_with_exact_parse(self):
        definition = msg([little("v", 0, 8, factor="0.1")], payload_bytes=2)
        compiled = build_product([definition]).messages[0]
        decoded = compiled.decode(b"\x05\x00").by_name()["v"]
        self.assertEqual(decoded.value, Fraction(1, 2))


class ReferenceAgreement(unittest.TestCase):
    """Randomized: compiled codec vs independent bit-list model."""

    def test_random_definitions_roundtrip(self):
        rng = random.Random(1234)
        for trial in range(120):
            payload_bytes = rng.randint(1, 10)
            total_bits = payload_bytes * 8
            used: set[int] = set()
            signals = []
            count = rng.randint(1, 6)
            for i in range(count):
                width = rng.randint(1, min(12, total_bits))
                order = rng.choice(["little", "big"])
                # Build a candidate, then ask the reference for its positions
                # and only keep it when bits are free.
                if order == "little":
                    start = rng.randint(0, total_bits - width)
                    cand = little(f"s{i}", start, width, signed=rng.random() < 0.4,
                                  factor=rng.choice(["1", "-3/4", "7/1000"]),
                                  offset=rng.choice(["0", "-1/2", "13"]))
                else:
                    byte = rng.randint(0, payload_bytes - 1)
                    bit = rng.randint(0, 7)
                    cand = big(f"s{i}", byte, bit, width, signed=rng.random() < 0.4,
                               factor=rng.choice(["1", "2/3", "-1/100"]),
                               offset=rng.choice(["0", "5", "-7/3"]))
                try:
                    from bitcatalog.reference import ref_signal_from_def

                    positions = set(ref_signal_from_def(cand, payload_bytes).positions)
                except ValueError:
                    continue
                if positions & used:
                    continue
                used |= positions
                signals.append(cand)
            if not signals:
                continue
            definition = msg(signals, payload_bytes=payload_bytes)
            self.assertEqual(ref_overlap_pairs(definition), [])
            self.assertEqual(ref_unassigned_bits(definition), set(range(total_bits)) - used)
            product = build_product([definition])
            compiled = product.messages[0]
            self.assertEqual(compiled.unassigned_mask, sum(1 << b for b in (set(range(total_bits)) - used)))
            raws = {}
            for sig in compiled.signals:
                raws[sig.name] = rng.randint(sig.raw_min, sig.raw_max)
            encoded = compiled.encode(raws)
            ref_payload = ref_encode(definition, raws, None)
            self.assertEqual(encoded.payload, ref_payload)
            self.assertEqual(
                compiled.unassigned_mask,
                int.from_bytes(ref_payload, "little") & 0
                | sum(1 << b for b in ref_unassigned_bits(definition)),
            )
            # unassigned physical bits are zero in the produced payload
            produced = int.from_bytes(encoded.payload, "little")
            self.assertEqual(produced & compiled.unassigned_mask, 0)
            decoded = compiled.decode(encoded.payload)
            self.assertEqual({s.name: s.raw for s in decoded.signals}, raws)
            for sig in decoded.signals:
                ref_sig = next(s for s in compiled.signals if s.name == sig.name)
                pattern = sig.raw & ((1 << ref_sig.width) - 1)
                self.assertEqual(sig.value, ref_sig.value(pattern))


class ProductFile(unittest.TestCase):
    def test_save_load_roundtrip_and_hash_guard(self):
        import tempfile

        definition = msg([little("v", 0, 12, signed=True, factor="3/10", offset="-4")], payload_bytes=4)
        product = build_product([definition])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "product.json"
            product.save(path)
            loaded = load_product(path)
            self.assertEqual(loaded.fingerprint, product.fingerprint)
            payload = product.messages[0].encode({"v": -7}).payload
            self.assertEqual(loaded.messages[0].decode(payload).by_name()["v"].value, Fraction(-21, 10) - 4)
            manifest = json.loads(path.read_text())
            manifest["content"]["messages"][0]["signals"][0]["width"] = 13
            path.write_text(json.dumps(manifest))
            with self.assertRaises(CatalogError) as ctx:
                load_product(path)
            self.assertEqual(ctx.exception.code, "bad_product_hash")

    def test_deterministic_fingerprint(self):
        a = build_product([msg([little("v", 0, 8)], payload_bytes=2)])
        b = build_product([msg([little("v", 0, 8)], payload_bytes=2)])
        self.assertEqual(a.fingerprint, b.fingerprint)


if __name__ == "__main__":
    unittest.main()
