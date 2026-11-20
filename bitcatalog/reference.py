"""Independent bit-by-bit reference model for cross-checks.

This module deliberately shares no bit arithmetic with :mod:`bitcatalog.catalog`.
Signals are described once as an explicit list of physical positions, and the
whole model works by walking that list one bit at a time.  Tests use it to
verify the compiled mask codec, the overlap proof, signed boundaries and exact
rational arithmetic.

Physical positions use the contract's little global numbering: global bit
``k`` is bit ``k % 8`` of payload byte ``k // 8`` (0 = least significant).

* little field:               positions ``start, start+1, ...`` low-to-high
* big field at (byte, bit):   walk ``bit, bit+1, ...`` inside the byte as
  physical positions ``byte*8 + (7 - offset)`` (most significant first);
  the value's bits are consumed high-to-low as positions advance.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class RefSignal:
    name: str
    positions: tuple[int, ...]  # physical global bits, order written below
    significance: tuple[int, ...]  # for each entry of positions, 2**k weight
    signed: bool
    factor: Fraction
    offset: Fraction
    branches: tuple[int, ...] = ()

    @property
    def width(self) -> int:
        return len(self.positions)

    @property
    def sign_position(self) -> int:
        return self.positions[self.significance.index(1 << (self.width - 1))]

    def raw_from_bits(self, bits: Sequence[int]) -> int:
        value = 0
        for pos, weight in zip(self.positions, self.significance):
            if bits[pos]:
                value += weight
        if self.signed and bits[self.sign_position]:
            value -= 1 << self.width
        return value

    def value_from_bits(self, bits: Sequence[int]) -> Fraction:
        return self.raw_from_bits(bits) * self.factor + self.offset


def ref_signal_from_def(raw: Mapping[str, Any], payload_bytes: int) -> RefSignal:
    order = raw["order"]
    width = raw["width"]
    start = raw["start"]
    positions: list[int] = []
    if order == "little":
        for k in range(width):
            g = start + k
            if g >= payload_bytes * 8:
                raise ValueError("little overrun")
            positions.append(g)
        significance = [1 << k for k in range(width)]
    elif order == "big":
        if not isinstance(start, (list, tuple)) or len(start) != 2:
            raise ValueError("big start must be [byte, bit]")
        byte0, bit0 = start
        for p in range(width):
            off = bit0 + p
            byte = byte0 + off // 8
            inner = off % 8
            if byte >= payload_bytes:
                raise ValueError("big overrun")
            positions.append(byte * 8 + (7 - inner))
        significance = [1 << (width - 1 - p) for p in range(width)]
    else:
        raise ValueError(f"bad order {order!r}")
    return RefSignal(
        name=raw["name"],
        positions=tuple(positions),
        significance=tuple(significance),
        signed=bool(raw.get("signed", False)),
        factor=Fraction(str(raw.get("factor", "1"))),
        offset=Fraction(str(raw.get("offset", "0"))),
        branches=tuple(raw.get("branches", ())),
    )


def ref_decode(
    message: Mapping[str, Any], payload: bytes, selector_override: int | None = None
) -> tuple[int | None, dict[str, int], dict[str, Fraction]]:
    """Return ``(selector, {name: raw}, {name: value})`` using only bit lists."""

    payload_bytes = message["payload_bytes"]
    if len(payload) != payload_bytes:
        raise ValueError(f"length {len(payload)} != {payload_bytes}")
    bits = [(payload[i // 8] >> (i % 8)) & 1 for i in range(payload_bytes * 8)]

    sel_raw = message.get("selector")
    selector: int | None = None
    if sel_raw is not None:
        sel_sig = ref_signal_from_def({**sel_raw, "signed": False, "factor": "1", "offset": "0"}, payload_bytes)
        selector = sel_sig.raw_from_bits(bits)
    elif selector_override is not None:
        raise ValueError("message has no selector")

    raws: dict[str, int] = {}
    values: dict[str, Fraction] = {}
    for raw in message["signals"]:
        sig = ref_signal_from_def(raw, payload_bytes)
        active = not sig.branches or selector in sig.branches
        if not active:
            continue
        raws[sig.name] = sig.raw_from_bits(bits)
        values[sig.name] = sig.value_from_bits(bits)
    return selector, raws, values


def ref_bits_for_raw(message: Mapping[str, Any], raw_values: Mapping[str, int], selector: int | None) -> list[int]:
    """Return a physical bit list (little numbering) produced by the raw values.

    This is the reference *encoder*: it computes value bits from the raw
    integer and writes each one to its physical position.  Unassigned bits are
    left at zero by construction.
    """

    payload_bytes = message["payload_bytes"]
    bits = [0] * (payload_bytes * 8)
    sel_raw = message.get("selector")
    selector_name = None
    if sel_raw is not None:
        selector_name = sel_raw["name"]
        sel_sig = ref_signal_from_def({**sel_raw, "signed": False, "factor": "1", "offset": "0"}, payload_bytes)
        if selector is None:
            raise ValueError("selector required")
        _write_bits(bits, sel_sig, selector)
    if selector_name is not None and selector_name in raw_values:
        raise ValueError("do not supply selector as a signal raw")
    for raw in message["signals"]:
        sig = ref_signal_from_def(raw, payload_bytes)
        active = not sig.branches or selector in sig.branches
        if active:
            if sig.name not in raw_values:
                raise ValueError(f"missing raw {sig.name!r}")
            _write_bits(bits, sig, raw_values[sig.name])
        elif sig.name in raw_values:
            raise ValueError(f"inactive signal {sig.name!r} supplied")
    return bits


def _write_bits(bits: list[int], sig: RefSignal, raw: int) -> None:
    width = sig.width
    minimum = -(1 << (width - 1)) if sig.signed else 0
    maximum = (1 << (width - 1)) - 1 if sig.signed else (1 << width) - 1
    if not (minimum <= raw <= maximum):
        raise ValueError(f"raw {raw} outside [{minimum},{maximum}] for {sig.name!r}")
    pattern = raw & ((1 << width) - 1)
    for pos, weight in zip(sig.positions, sig.significance):
        bit = 1 if pattern & weight else 0
        if bits[pos] and bit:
            raise ValueError(f"physical bit {pos} written twice")
        bits[pos] = bit


def bits_to_bytes(bits: Sequence[int], payload_bytes: int) -> bytes:
    out = bytearray(payload_bytes)
    for i, bit in enumerate(bits):
        if bit:
            out[i // 8] |= 1 << (i % 8)
    return bytes(out)


def ref_encode(message: Mapping[str, Any], raw_values: Mapping[str, int], selector: int | None) -> bytes:
    bits = ref_bits_for_raw(message, raw_values, selector)
    return bits_to_bytes(bits, message["payload_bytes"])


def ref_active(message: Mapping[str, Any], payload: bytes) -> dict[str, int]:
    _, raws, _ = ref_decode(message, payload)
    return raws


def ref_overlap_pairs(message: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Pairs of simultaneously-active signals whose physical bits overlap."""

    payload_bytes = message["payload_bytes"]
    sigs = [ref_signal_from_def(raw, payload_bytes) for raw in message["signals"]]
    out: list[tuple[str, str]] = []
    for i, a in enumerate(sigs):
        for b in sigs[i + 1 :]:
            exclusive = bool(a.branches) and bool(b.branches) and not (set(a.branches) & set(b.branches))
            if exclusive:
                continue
            if set(a.positions) & set(b.positions):
                out.append((a.name, b.name))
    return out


def ref_unassigned_bits(message: Mapping[str, Any]) -> set[int]:
    payload_bytes = message["payload_bytes"]
    occupied: set[int] = set()
    for raw in message["signals"]:
        occupied.update(ref_signal_from_def(raw, payload_bytes).positions)
    if message.get("selector") is not None:
        occupied.update(
            ref_signal_from_def(
                {**message["selector"], "signed": False, "factor": "1", "offset": "0"}, payload_bytes
            ).positions
        )
    return set(range(payload_bytes * 8)) - occupied
