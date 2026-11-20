"""Bit-field message catalog: definitions, validation, compilation, codec.

Protocol contract implemented here (protocol v1)
================================================

A catalog consists of messages.  Each message has ``bus``, ``frame_id``, a
fixed ``payload_bytes`` in 1..64 and a list of signals.  Each signal has:

* a name unique inside the message,
* a non-negative ``start``,
* ``width`` in 1..32,
* ``order`` (``little`` or ``big``),
* ``signed`` flag,
* a non-zero rational ``factor`` and a rational ``offset``.

Bit semantics
-------------

``little`` -- ``start`` is a global bit number.  Global bit ``k`` is the low
bit ``k % 8`` of payload byte ``k // 8``.  Consecutive physical bits are the
value's low-to-high bits.

``big`` -- ``start`` is ``(byte_index, bit_index)`` with ``bit_index`` counted
from the most significant bit of that byte (0 = 0x80).  Moving to the next
position goes to the next-lower bit of the same byte and then to the most
significant bit of the next byte; these consecutive positions are the value's
high-to-low bits.

A signed value is the two's-complement interpretation of the ``width`` bits.
The physical value is ``raw * factor + offset`` computed exactly with
:class:`fractions.Fraction`.

Activity
--------

An optional unsigned, unscaled ``selector`` gives the frame's branch value.
Plain signals are always active.  A branch signal lists one or more selector
values and exists only when the frame's selector equals one of them.  Any two
signals that could be active at the same time must not share a physical bit
(checked across bytes and across both orders).  Signals on disjoint branch
sets are mutually exclusive and may reuse bits.  The selector may not overlap
any signal bit.

Majority-overlap and decoding correctness are checked independently by
:mod:`bitcatalog.reference` in the test-suite; this module uses precompiled
masks and whole-integer extraction for speed.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

PROTOCOL = "bitcatalog-proto/1"
PRODUCT_FORMAT = "bitcatalog-product/1"

ORDER_LITTLE = "little"
ORDER_BIG = "big"
_MAX_BITS = 64 * 8


class CatalogError(ValueError):
    """A definition or product is rejected."""

    def __init__(self, message: str, *, code: str = "catalog_error", **detail: Any):
        super().__init__(message)
        self.code = code
        self.detail = detail


class CompileError(CatalogError):
    """A definition failed validation at product build time."""


class DecodeError(CatalogError):
    """A frame body cannot be interpreted with the selected product."""

    def __init__(self, message: str, *, code: str = "decode_error", position: int | None = None, **detail: Any):
        super().__init__(message, code=code, **detail)
        self.position = position


# ---------------------------------------------------------------------------
# rational helpers
# ---------------------------------------------------------------------------


def as_fraction(value: Any, *, field_name: str) -> Fraction:
    """Parse a JSON-ish rational without going through binary floats."""

    if isinstance(value, bool):
        raise CatalogError(f"{field_name} must be a rational, not a bool", code="bad_rational")
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, str):
        text = value.strip()
        try:
            return Fraction(text)
        except (ValueError, ZeroDivisionError) as exc:
            raise CatalogError(f"{field_name}: invalid rational {value!r}", code="bad_rational") from exc
    if isinstance(value, float):
        # Decimal text avoids exposing the binary expansion of e.g. 0.1.
        return Fraction(repr(value))
    raise CatalogError(f"{field_name}: unsupported rational type {type(value).__name__}", code="bad_rational")


def fraction_text(value: Fraction) -> str:
    """Canonical text used in product files and manifests."""

    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


def _expect_int(value: Any, field_name: str, *, minimum: int | None = None, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CatalogError(f"{field_name} must be an integer", code="bad_field", field=field_name)
    if minimum is not None and value < minimum:
        raise CatalogError(f"{field_name} must be >= {minimum}", code="bad_field", field=field_name)
    if maximum is not None and value > maximum:
        raise CatalogError(f"{field_name} must be <= {maximum}", code="bad_field", field=field_name)
    return value


def _parse_start(raw_start: Any, raw_order: Any, payload_bytes: int, *, field_name: str) -> tuple[str, int, tuple[int, int] | None]:
    """Return ``(order, start_as_global_int, big_pair_or_None)``.

    The global-int form is used for little fields.  For big fields it is only
    a convenience value (byte*8 + bit) and must not be mixed with little bit
    numbering; the pair carries the authoritative position.
    """

    order = raw_order
    pair: tuple[int, int] | None = None
    if isinstance(raw_start, Mapping):
        missing = {"byte", "bit"} - set(raw_start)
        if missing:
            raise CatalogError(f"{field_name}: start object needs byte and bit", code="bad_field")
        pair = (
            _expect_int(raw_start["byte"], f"{field_name}.byte", minimum=0),
            _expect_int(raw_start["bit"], f"{field_name}.bit", minimum=0, maximum=7),
        )
        start_value = pair[0] * 8 + pair[1]
    elif isinstance(raw_start, (list, tuple)):
        if len(raw_start) != 2:
            raise CatalogError(f"{field_name}: big start needs [byte, bit]", code="bad_field")
        pair = (
            _expect_int(raw_start[0], f"{field_name}[0]", minimum=0),
            _expect_int(raw_start[1], f"{field_name}[1]", minimum=0, maximum=7),
        )
        start_value = pair[0] * 8 + pair[1]
    elif isinstance(raw_start, int) and not isinstance(raw_start, bool):
        start_value = _expect_int(raw_start, field_name, minimum=0)
    else:
        raise CatalogError(f"{field_name}: start must be an integer or (byte, bit)", code="bad_field")

    if order is None:
        order = ORDER_LITTLE if pair is None else ORDER_BIG
    if order not in (ORDER_LITTLE, ORDER_BIG):
        raise CatalogError(f"{field_name}: order must be 'little' or 'big'", code="bad_order")
    if order == ORDER_BIG and pair is None:
        raise CatalogError(f"{field_name}: big order needs a (byte, bit) start", code="bad_start")
    if order == ORDER_LITTLE and pair is not None:
        raise CatalogError(f"{field_name}: little order needs a global bit start", code="bad_start")
    return order, start_value, pair


def physical_bits(order: str, start: int, pair: tuple[int, int] | None, width: int, payload_bytes: int) -> list[int]:
    """Independent-of-compilation physical bit set, little global numbering.

    Used by the overlap proof.  Big placement follows the contract exactly:
    consecutive positions walk down one byte's bits and then resume at the
    most significant bit of the next byte.
    """

    bits: list[int] = []
    if order == ORDER_LITTLE:
        for k in range(width):
            g = start + k
            if g >= payload_bytes * 8:
                raise CatalogError(f"field overruns payload at bit {g}", code="overrun")
            bits.append(g)
    else:
        assert pair is not None
        byte0, bit0 = pair
        for p in range(width):
            off = bit0 + p
            byte = byte0 + off // 8
            bit_in_byte = off % 8
            if byte >= payload_bytes:
                raise CatalogError("field overruns payload", code="overrun")
            bits.append(byte * 8 + (7 - bit_in_byte))
    return bits


# ---------------------------------------------------------------------------
# definitions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SignalDef:
    name: str
    start: int
    width: int
    order: str
    signed: bool
    factor: Fraction
    offset: Fraction
    branches: tuple[int, ...] = ()
    pair: tuple[int, int] | None = field(default=None, compare=False, repr=False)

    @classmethod
    def parse(cls, raw: Mapping[str, Any], *, payload_bytes: int, muxed: bool) -> "SignalDef":
        if not isinstance(raw, Mapping):
            raise CatalogError("signal definition must be an object", code="bad_signal")
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            raise CatalogError("signal needs a non-empty name", code="bad_name")
        order, start, pair = _parse_start(raw.get("start"), raw.get("order"), payload_bytes, field_name=f"signal {name!r}.start")
        width = _expect_int(raw.get("width"), f"signal {name!r}.width", minimum=1, maximum=32)
        if start + width > _MAX_BITS:
            raise CatalogError(f"signal {name!r} overruns the 64-byte maximum", code="overrun")
        # Full payload-bound check (also validates big fields).
        physical_bits(order, start, pair, width, payload_bytes)

        signed = raw.get("signed", False)
        if not isinstance(signed, bool):
            raise CatalogError(f"signal {name!r}.signed must be boolean", code="bad_signal")

        factor = as_fraction(raw.get("factor", 1), field_name=f"signal {name!r}.factor")
        if factor == 0:
            raise CatalogError(f"signal {name!r}.factor must be non-zero", code="bad_factor")
        offset = as_fraction(raw.get("offset", 0), field_name=f"signal {name!r}.offset")

        branches: tuple[int, ...] = ()
        if muxed and "branches" in raw:
            raw_branches = raw.get("branches", ())
            if not isinstance(raw_branches, (list, tuple)) or not raw_branches:
                raise CatalogError(
                    f"signal {name!r} branches must be a non-empty list",
                    code="bad_branch",
                )
            seen: list[int] = []
            for value in raw_branches:
                v = _expect_int(value, f"signal {name!r}.branches", minimum=0)
                if v not in seen:
                    seen.append(v)
            branches = tuple(seen)
        elif not muxed and "branches" in raw:
            raise CatalogError(f"signal {name!r} has branches but the message has no selector", code="bad_branch")
        return cls(
            name=name,
            start=start,
            width=width,
            order=order,
            signed=signed,
            factor=factor,
            offset=offset,
            branches=branches,
            pair=pair,
        )

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "start": [self.pair[0], self.pair[1]] if self.order == ORDER_BIG else self.start,
            "width": self.width,
            "order": self.order,
            "signed": self.signed,
            "factor": fraction_text(self.factor),
            "offset": fraction_text(self.offset),
        }
        if self.branches:
            out["branches"] = list(self.branches)
        return out


@dataclass(frozen=True)
class SelectorDef:
    name: str
    start: int
    width: int
    order: str
    pair: tuple[int, int] | None = field(default=None, compare=False, repr=False)

    @classmethod
    def parse(cls, raw: Mapping[str, Any], *, payload_bytes: int) -> "SelectorDef":
        if not isinstance(raw, Mapping):
            raise CatalogError("selector must be an object", code="bad_selector")
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            raise CatalogError("selector needs a non-empty name", code="bad_name")
        order, start, pair = _parse_start(raw.get("start"), raw.get("order"), payload_bytes, field_name=f"selector {name!r}.start")
        width = _expect_int(raw.get("width"), f"selector {name!r}.width", minimum=1, maximum=32)
        physical_bits(order, start, pair, width, payload_bytes)
        if as_fraction(raw.get("factor", 1), field_name="selector.factor") != 1:
            raise CatalogError("selector must be unscaled (factor 1)", code="bad_selector")
        if as_fraction(raw.get("offset", 0), field_name="selector.offset") != 0:
            raise CatalogError("selector must be unscaled (offset 0)", code="bad_selector")
        if raw.get("signed", False):
            raise CatalogError("selector must be unsigned", code="bad_selector")
        return cls(name=name, start=start, width=width, order=order, pair=pair)

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "start": [self.pair[0], self.pair[1]] if self.order == ORDER_BIG else self.start,
            "width": self.width,
            "order": self.order,
            "signed": False,
            "factor": "1",
            "offset": "0",
        }


@dataclass(frozen=True)
class MessageDef:
    bus: str
    frame_id: int
    payload_bytes: int
    signals: tuple[SignalDef, ...]
    selector: SelectorDef | None = None

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> "MessageDef":
        if not isinstance(raw, Mapping):
            raise CatalogError("message definition must be an object", code="bad_message")
        bus = raw.get("bus")
        if not isinstance(bus, str) or not bus:
            raise CatalogError("message needs a non-empty bus", code="bad_bus")
        frame_id = _expect_int(raw.get("frame_id"), "frame_id", minimum=0)
        payload_bytes = _expect_int(raw.get("payload_bytes"), "payload_bytes", minimum=1, maximum=64)
        raw_signals = raw.get("signals")
        if not isinstance(raw_signals, (list, tuple)) or not raw_signals:
            raise CatalogError("message needs at least one signal", code="bad_message")
        raw_selector = raw.get("selector")
        selector = SelectorDef.parse(raw_selector, payload_bytes=payload_bytes) if raw_selector is not None else None
        signals = tuple(SignalDef.parse(sig, payload_bytes=payload_bytes, muxed=selector is not None) for sig in raw_signals)
        msg = cls(bus=bus, frame_id=frame_id, payload_bytes=payload_bytes, signals=signals, selector=selector)
        names = [s.name for s in signals]
        if len(set(names)) != len(names):
            duplicate = next(n for n in names if names.count(n) > 1)
            raise CatalogError(f"duplicate signal name {duplicate!r}", code="duplicate_name")
        if selector is not None and selector.name in names:
            raise CatalogError("selector name clashes with a signal name", code="duplicate_name")
        return msg

    def to_json(self) -> dict[str, Any]:
        return {
            "bus": self.bus,
            "frame_id": self.frame_id,
            "payload_bytes": self.payload_bytes,
            "selector": self.selector.to_json() if self.selector else None,
            "signals": [s.to_json() for s in self.signals],
        }

    def canonical_key(self) -> tuple[str, int]:
        return (self.bus, self.frame_id)

    def validate(self) -> None:
        """Reject overlapping simultaneous activity and selector overlap."""

        if self.selector is not None:
            sel_bits = set(physical_bits(self.selector.order, self.selector.start, self.selector.pair, self.selector.width, self.payload_bytes))
        else:
            sel_bits = set()

        bit_sets: dict[str, set[int]] = {}
        branch_sets: dict[str, frozenset[int] | None] = {}
        for sig in self.signals:
            bits = set(physical_bits(sig.order, sig.start, sig.pair, sig.width, self.payload_bytes))
            bit_sets[sig.name] = bits
            branch_sets[sig.name] = frozenset(sig.branches) if sig.branches else None
            if self.selector is not None:
                clash = bits & sel_bits
                if clash:
                    bit = min(clash)
                    raise CompileError(
                        f"selector {self.selector.name!r} overlaps signal {sig.name!r} at physical bit {bit} "
                        f"(byte {bit // 8}, bit {bit % 8})",
                        code="selector_overlap",
                        signal=sig.name,
                        bit=bit,
                    )
            elif sig.branches:
                raise CompileError("branch signal without selector", code="bad_branch")

        def exclusive(name_a: str, name_b: str) -> bool:
            ba, bb = branch_sets[name_a], branch_sets[name_b]
            if ba is None or bb is None:
                return False
            return not (ba & bb)

        names = [s.name for s in self.signals]
        # Expensive pairwise proof runs on the given scale in well under a
        # second; it is exact for subset branch sets as well.
        for i, name_a in enumerate(names):
            for name_b in names[i + 1 :]:
                if exclusive(name_a, name_b):
                    continue
                clash = bit_sets[name_a] & bit_sets[name_b]
                if clash:
                    bit = min(clash)
                    raise CompileError(
                        f"signals {name_a!r} and {name_b!r} can be active together and overlap at "
                        f"physical bit {bit} (byte {bit // 8}, bit {bit % 8})",
                        code="signal_overlap",
                        signals=(name_a, name_b),
                        bit=bit,
                    )


# ---------------------------------------------------------------------------
# compilation
# ---------------------------------------------------------------------------


def _signed_value(raw: int, width: int) -> int:
    if raw & (1 << (width - 1)):
        return raw - (1 << width)
    return raw


@dataclass(frozen=True)
class CompiledSignal:
    name: str
    start: int
    width: int
    order: str
    signed: bool
    factor: Fraction
    offset: Fraction
    branches: tuple[int, ...]
    mask: int
    sign_bit: int
    raw_min: int
    raw_max: int
    bit_offset: int  # big: shift inside big-endian integer; little: start
    segments: tuple[tuple[int, int, int, int], ...]  # encode: (byte, clear, right, left)
    start_pair: tuple[int, int] | None = None
    _value_cache: dict[int, Fraction] = field(default_factory=dict, compare=False, repr=False)

    @property
    def always(self) -> bool:
        return not self.branches

    def extract_raw(self, payload: bytes, le_int: int | None = None, be_int: int | None = None) -> int:
        if self.order == ORDER_LITTLE:
            if le_int is None:
                le_int = int.from_bytes(payload, "little")
            return (le_int >> self.start) & self.mask
        if be_int is None:
            be_int = int.from_bytes(payload, "big")
        return (be_int >> self.bit_offset) & self.mask

    def raw_to_value(self, raw: int) -> int:
        return _signed_value(raw, self.width) if self.signed else raw

    def value(self, pattern: int) -> Fraction:
        cached = self._value_cache.get(pattern)
        if cached is None:
            cached = self.raw_to_value(pattern) * self.factor + self.offset
            # Small-width fields repeat raw patterns heavily, so this cache is
            # what keeps 100k-frame aggregation out of Fraction.__add__.
            if len(self._value_cache) < 1 << 16:
                self._value_cache[pattern] = cached
        return cached

    def phys_min(self) -> Fraction:
        return min(self.raw_min * self.factor + self.offset, self.raw_max * self.factor + self.offset)

    def phys_max(self) -> Fraction:
        return max(self.raw_min * self.factor + self.offset, self.raw_max * self.factor + self.offset)

    def apply_raw(self, payload: bytearray, raw: int) -> None:
        v = raw & self.mask
        for byte, clear, right, left in self.segments:
            seg = ((v >> right) << left) & (clear ^ 0xFF)
            payload[byte] = (payload[byte] & clear) | seg


def _build_segments(sig: SignalDef, payload_bytes: int) -> tuple[tuple[int, int, int, int], ...]:
    """Segments for patching already-aligned contiguous byte chunks.

    Each entry is ``(byte_index, clear_mask_in_byte, right_shift, left_shift)``
    so a value segment is ``((v >> right) << left) & ~clear``.
    """

    segs: list[tuple[int, int, int, int]] = []
    if sig.order == ORDER_LITTLE:
        bit = sig.start
        remaining = sig.width
        shift = 0
        while remaining:
            byte = bit // 8
            off = bit % 8
            take = min(8 - off, remaining)
            seg_mask = ((1 << take) - 1) << off
            segs.append((byte, (~seg_mask) & 0xFF, shift, off))
            shift += take
            remaining -= take
            bit += take
    else:
        assert sig.pair is not None
        byte0, bit0 = sig.pair
        # Walk the big window in physical order.  Physical order is the
        # value's high-to-low order, so the right shift starts at width - take.
        off = 0
        while off < sig.width:
            byte_off, bit_in_byte = divmod(bit0 + off, 8)
            take = min(8 - bit_in_byte, sig.width - off)
            byte = byte0 + byte_off
            seg_mask = ((1 << take) - 1) << (8 - bit_in_byte - take)
            shift = sig.width - off - take
            segs.append((byte, (~seg_mask) & 0xFF, shift, 8 - bit_in_byte - take))
            off += take
    return tuple(segs)


def _compile_signal(sig: SignalDef, payload_bytes: int) -> CompiledSignal:
    mask = (1 << sig.width) - 1
    if sig.order == ORDER_BIG:
        assert sig.pair is not None
        total = payload_bytes * 8
        bit_offset = total - (sig.pair[0] * 8 + sig.pair[1]) - sig.width
        if bit_offset < 0:
            raise CompileError(f"signal {sig.name!r} overruns payload", code="overrun")
    else:
        bit_offset = sig.start
    if sig.signed:
        raw_min, raw_max = -(1 << (sig.width - 1)), (1 << (sig.width - 1)) - 1
    else:
        raw_min, raw_max = 0, mask
    return CompiledSignal(
        name=sig.name,
        start=sig.start,
        width=sig.width,
        order=sig.order,
        signed=sig.signed,
        factor=sig.factor,
        offset=sig.offset,
        branches=sig.branches,
        mask=mask,
        sign_bit=1 << (sig.width - 1),
        raw_min=raw_min,
        raw_max=raw_max,
        bit_offset=bit_offset,
        segments=_build_segments(sig, payload_bytes),
        start_pair=sig.pair,
    )


@dataclass(frozen=True)
class CompiledSelector:
    name: str
    start: int
    width: int
    order: str
    mask: int
    bit_offset: int

    def read(self, payload: bytes, le_int: int | None = None, be_int: int | None = None) -> int:
        if self.order == ORDER_LITTLE:
            if le_int is None:
                le_int = int.from_bytes(payload, "little")
            return (le_int >> self.start) & self.mask
        if be_int is None:
            be_int = int.from_bytes(payload, "big")
        return (be_int >> self.bit_offset) & self.mask

    def apply(self, payload: bytearray, value: int) -> None:
        mask = self.mask
        v = value & mask
        if self.order == ORDER_LITTLE:
            for shift in range(0, self.width, 8):
                byte = (self.start + shift) // 8
                off = (self.start + shift) % 8
                take = min(8 - off, self.width - shift)
                clear = (~(((1 << take) - 1) << off)) & 0xFF
                payload[byte] = (payload[byte] & clear) | (((v >> shift) & ((1 << take) - 1)) << off)
        else:
            be = int.from_bytes(payload, "big")
            be = (be & ~(mask << self.bit_offset)) | (v << self.bit_offset)
            payload[:] = be.to_bytes(len(payload), "big")


@dataclass
class CompiledMessage:
    bus: str
    frame_id: int
    payload_bytes: int
    signals: tuple[CompiledSignal, ...]
    selector: CompiledSelector | None
    always_indices: tuple[int, ...]
    # selector value -> tuple(indices sorted in definition order)
    branch_map: dict[int, tuple[int, ...]]
    unassigned_mask: int

    @property
    def key(self) -> tuple[str, int]:
        return (self.bus, self.frame_id)

    def active_indices(self, payload: bytes, le_int: int | None = None, be_int: int | None = None) -> tuple[int, ...]:
        if self.selector is None:
            return self.always_indices
        value = self.selector.read(payload, le_int, be_int)
        return self.branch_map.get(value, self.always_indices)

    def decode(self, payload: bytes) -> "DecodedFrame":
        if len(payload) != self.payload_bytes:
            raise DecodeError(
                f"payload length {len(payload)} != {self.payload_bytes} for {self.key}",
                code="bad_length",
                message_key=self.key,
                expected=self.payload_bytes,
                actual=len(payload),
            )
        le_int = int.from_bytes(payload, "little")
        be_int = int.from_bytes(payload, "big")
        selector = self.selector.read(payload, le_int, be_int) if self.selector else None
        out = []
        for idx in self.active_indices(payload, le_int, be_int):
            sig = self.signals[idx]
            pattern = sig.extract_raw(payload, le_int, be_int)
            out.append(
                DecodedSignal(
                    name=sig.name,
                    raw=sig.raw_to_value(pattern),
                    value=sig.value(pattern),
                    branches=sig.branches,
                )
            )
        return DecodedFrame(
            bus=self.bus,
            frame_id=self.frame_id,
            payload=payload,
            selector=selector,
            signals=tuple(out),
            unassigned_mask=self.unassigned_mask,
        )

    def decode_observations(self, payload: bytes) -> tuple[int | None, tuple[str, int, Fraction, Fraction], ...]:
        """Fast decode for bulk storage: ``(selector, ((name, raw, num, den), ...))``."""

        if len(payload) != self.payload_bytes:
            raise DecodeError(
                f"payload length {len(payload)} != {self.payload_bytes} for {self.key}",
                code="bad_length",
                message_key=self.key,
                expected=self.payload_bytes,
                actual=len(payload),
            )
        le_int = int.from_bytes(payload, "little")
        be_int = int.from_bytes(payload, "big")
        selector = self.selector.read(payload, le_int, be_int) if self.selector else None
        out = []
        for idx in self.active_indices(payload, le_int, be_int):
            sig = self.signals[idx]
            pattern = sig.extract_raw(payload, le_int, be_int)
            value = sig.value(pattern)
            out.append((sig.name, sig.raw_to_value(pattern), value.numerator, value.denominator))
        return selector, tuple(out)

    def encode(self, raws: Mapping[str, int], selector: int | None = None) -> "EncodedFrame":
        body, sel_value, active_names = self.encode_body(raws, selector=selector)
        le_int = int.from_bytes(body, "little")
        be_int = int.from_bytes(body, "big")
        return EncodedFrame(
            bus=self.bus,
            frame_id=self.frame_id,
            payload=body,
            payload_hex=body.hex(),
            selector=sel_value,
            signals=tuple(
                DecodedSignal(
                    name=self.signals[idx].name,
                    raw=raws[self.signals[idx].name],
                    value=self.signals[idx].value(self.signals[idx].extract_raw(body, le_int, be_int)),
                    branches=self.signals[idx].branches,
                )
                for idx in active_names
            ),
            unassigned_mask=self.unassigned_mask,
        )

    def encode_body(
        self, raws: Mapping[str, int], selector: int | None = None
    ) -> tuple[bytes, int | None, tuple[int, ...]]:
        """Encode raw values, returning ``(payload, selector, active indices)``.

        This is the hot path for bulk synthesis and ingestion: no Fraction
        objects are built.  It performs the same validation and the same
        extraction-path round-trip check as :meth:`encode`.
        """

        signals_by_name = {s.name: s for s in self.signals}
        index_by_name = {s.name: i for i, s in enumerate(self.signals)}
        unknown = set(raws) - set(signals_by_name)
        if unknown:
            raise DecodeError(f"unknown signal(s) {sorted(unknown)!r}", code="unknown_signal", message_key=self.key)
        if self.selector is None:
            if selector is not None:
                raise DecodeError("message has no selector", code="bad_selector")
            active_names = list(signals_by_name)
            sel_value = None
        else:
            if selector is None:
                raise DecodeError("selector value required", code="missing_selector", message_key=self.key)
            sel_value = selector
            if not isinstance(sel_value, int) or sel_value < 0:
                raise DecodeError("selector must be a non-negative integer", code="bad_selector")
            if sel_value > self.selector.mask:
                raise DecodeError(
                    f"selector {sel_value} does not fit width {self.selector.width}",
                    code="selector_out_of_range",
                    position=0,
                )
            active_names = [self.signals[i].name for i in self.branch_map.get(sel_value, self.always_indices)]
        active_set = set(active_names)
        missing = active_set - set(raws)
        if missing:
            raise DecodeError(
                f"missing raw value(s) for active signals {sorted(missing)!r}",
                code="missing_signal",
                message_key=self.key,
            )
        inactive_supplied = set(raws) - active_set
        if inactive_supplied:
            raise DecodeError(
                f"raw value supplied for inactive signal(s) {sorted(inactive_supplied)!r}",
                code="inactive_signal",
                message_key=self.key,
            )
        payload = bytearray(self.payload_bytes)
        if self.selector is not None:
            assert sel_value is not None
            self.selector.apply(payload, sel_value)
        for name in active_names:
            sig = signals_by_name[name]
            raw = raws[name]
            if not isinstance(raw, int) or isinstance(raw, bool):
                raise DecodeError(f"raw value for {name!r} must be an integer", code="bad_raw")
            if raw < sig.raw_min or raw > sig.raw_max:
                raise DecodeError(
                    f"raw value {raw} for {name!r} outside [{sig.raw_min}, {sig.raw_max}]",
                    code="raw_out_of_range",
                    message_key=self.key,
                    signal=name,
                    raw_min=sig.raw_min,
                    raw_max=sig.raw_max,
                )
            sig.apply_raw(payload, raw)
        body = bytes(payload)
        # Self-check through the compiled extraction path.
        le_int = int.from_bytes(body, "little")
        be_int = int.from_bytes(body, "big")
        for name in active_names:
            sig = signals_by_name[name]
            produced = sig.raw_to_value(sig.extract_raw(body, le_int, be_int))
            if produced != raws[name]:
                raise DecodeError(
                    f"round-trip mismatch for {name!r}: wrote {raws[name]}, read {produced}",
                    code="roundtrip",
                    message_key=self.key,
                    signal=name,
                )
        return body, sel_value, tuple(index_by_name[n] for n in active_names)


@dataclass(frozen=True)
class DecodedSignal:
    name: str
    raw: int
    value: Fraction
    branches: tuple[int, ...] = ()

    def value_text(self) -> str:
        return fraction_text(self.value)


@dataclass(frozen=True)
class DecodedFrame:
    bus: str
    frame_id: int
    payload: bytes
    selector: int | None
    signals: tuple[DecodedSignal, ...]
    unassigned_mask: int

    @property
    def payload_hex(self) -> str:
        return self.payload.hex()

    def by_name(self) -> dict[str, DecodedSignal]:
        return {s.name: s for s in self.signals}

    def values(self) -> dict[str, Fraction]:
        return {s.name: s.value for s in self.signals}


@dataclass(frozen=True)
class EncodedFrame:
    bus: str
    frame_id: int
    payload: bytes
    payload_hex: str
    selector: int | None
    signals: tuple[DecodedSignal, ...]
    unassigned_mask: int


@dataclass
class CatalogProduct:
    """An immutable, content-addressed compiled catalog."""

    fingerprint: str
    messages: tuple[CompiledMessage, ...]
    content: dict[str, Any]
    _by_key: dict[tuple[str, int], CompiledMessage] = field(init=False, repr=False)
    _by_frame_id: dict[int, list[CompiledMessage]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._by_key = {m.key: m for m in self.messages}
        self._by_frame_id = {}
        for m in self.messages:
            self._by_frame_id.setdefault(m.frame_id, []).append(m)

    @property
    def protocol(self) -> str:
        return self.content["protocol"]

    def message(self, bus: str, frame_id: int) -> CompiledMessage:
        try:
            return self._by_key[(bus, frame_id)]
        except KeyError:
            raise DecodeError(f"unknown message bus={bus!r} frame_id={frame_id}", code="unknown_message") from None

    def message_by_frame_id(self, frame_id: int) -> CompiledMessage:
        matches = self._by_frame_id.get(frame_id, [])
        if not matches:
            raise DecodeError(f"unknown frame_id {frame_id}", code="unknown_message")
        if len(matches) > 1:
            raise DecodeError(
                f"frame_id {frame_id} is ambiguous across buses; pass bus",
                code="ambiguous_message",
                buses=[m.bus for m in matches],
            )
        return matches[0]

    def codec(self) -> "FrameCodec":
        return FrameCodec(self)

    def definitions(self) -> list[dict[str, Any]]:
        return self.content["messages"]

    def manifest(self, *, embed_content: bool = True) -> dict[str, Any]:
        compiled = []
        for msg in self.messages:
            compiled.append(
                {
                    "bus": msg.bus,
                    "frame_id": msg.frame_id,
                    "payload_bytes": msg.payload_bytes,
                    "always": list(msg.always_indices),
                    "branch_values": sorted(msg.branch_map),
                    "unassigned_mask": f"0x{msg.unassigned_mask:0{msg.payload_bytes * 2}x}",
                    "signals": [
                        {
                            "name": s.name,
                            "order": s.order,
                            "start": (list(s.start_pair) if s.order == ORDER_BIG else s.start),
                            "width": s.width,
                            "mask": f"0x{s.mask:x}",
                            "raw_range": [s.raw_min, s.raw_max],
                            "phys_range": [fraction_text(s.phys_min()), fraction_text(s.phys_max())],
                        }
                        for s in msg.signals
                    ],
                }
            )
        out = {
            "format": PRODUCT_FORMAT,
            "protocol": self.content["protocol"],
            "fingerprint": self.fingerprint,
            "message_count": len(self.messages),
            "content_sha256": self.fingerprint,
        }
        if embed_content:
            out["content"] = self.content
        out["compiled"] = compiled
        return out

    def save(self, path: str | os.PathLike[str]) -> str:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        manifest = self.manifest(embed_content=True)
        text = json.dumps(manifest, sort_keys=True, ensure_ascii=False, indent=1)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(text + "\n", encoding="utf-8")
        os.replace(tmp, target)
        return str(target)

    @property
    def definitions_json(self) -> str:
        return json.dumps(self.content, sort_keys=True, ensure_ascii=False)


class FrameCodec:
    """Encode and decode frames against one immutable product."""

    def __init__(self, product: CatalogProduct):
        self.product = product

    def message(self, bus: str | None, frame_id: int) -> CompiledMessage:
        if bus is None:
            return self.product.message_by_frame_id(frame_id)
        return self.product.message(bus, frame_id)

    def decode(self, payload: bytes, *, bus: str | None = None, frame_id: int) -> DecodedFrame:
        msg = self.message(bus, frame_id)
        return msg.decode(payload)

    def encode(
        self,
        raws: Mapping[str, int],
        *,
        bus: str | None = None,
        frame_id: int,
        selector: int | None = None,
    ) -> EncodedFrame:
        msg = self.message(bus, frame_id)
        return msg.encode(raws, selector=selector)


# ---------------------------------------------------------------------------
# product construction / loading
# ---------------------------------------------------------------------------


def _canonical_content(messages: Iterable[MessageDef]) -> dict[str, Any]:
    ordered = sorted(messages, key=lambda m: (m.bus, m.frame_id))
    return {"protocol": PROTOCOL, "messages": [m.to_json() for m in ordered]}


def _content_sha(content: dict[str, Any]) -> str:
    blob = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _compile_content(content: Mapping[str, Any]) -> tuple[CompiledMessage, ...]:
    if content.get("protocol") != PROTOCOL:
        raise CompileError(f"unsupported protocol {content.get('protocol')!r}", code="bad_protocol")
    raw_messages = content.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise CompileError("catalog needs at least one message", code="empty_catalog")
    seen: set[tuple[str, int]] = set()
    compiled: list[CompiledMessage] = []
    for raw in raw_messages:
        msg = MessageDef.parse(raw)
        if msg.canonical_key() in seen:
            raise CompileError(f"duplicate message key {(msg.bus, msg.frame_id)}", code="duplicate_message")
        seen.add(msg.canonical_key())
        msg.validate()
        signals = tuple(_compile_signal(s, msg.payload_bytes) for s in msg.signals)
        selector = None
        if msg.selector is not None:
            sel = msg.selector
            if sel.order == ORDER_BIG:
                assert sel.pair is not None
                bit_offset = msg.payload_bytes * 8 - (sel.pair[0] * 8 + sel.pair[1]) - sel.width
            else:
                bit_offset = sel.start
            selector = CompiledSelector(
                name=sel.name,
                start=sel.start,
                width=sel.width,
                order=sel.order,
                mask=(1 << sel.width) - 1,
                bit_offset=bit_offset,
            )
        always_indices = tuple(i for i, s in enumerate(signals) if not s.branches)
        branch_map: dict[int, tuple[int, ...]] = {}
        if selector is not None:
            values = sorted({v for s in signals for v in s.branches})
            for value in values:
                if value > selector.mask:
                    raise CompileError(
                        f"branch value {value} does not fit selector width {selector.width}",
                        code="bad_branch",
                    )
                branch_map[value] = tuple(
                    i for i, s in enumerate(signals) if not s.branches or value in s.branches
                )
        # Coverage is taken from the physical-bit proof so little and big
        # fields share one numbering and the report matches the reference.
        assigned_bits = set()
        for s in msg.signals:
            assigned_bits.update(physical_bits(s.order, s.start, s.pair, s.width, msg.payload_bytes))
        if selector is not None:
            seldef = msg.selector
            assert seldef is not None
            assigned_bits.update(physical_bits(seldef.order, seldef.start, seldef.pair, seldef.width, msg.payload_bytes))
        unassigned_mask = 0
        for bit in range(msg.payload_bytes * 8):
            if bit not in assigned_bits:
                unassigned_mask |= 1 << bit
        compiled.append(
            CompiledMessage(
                bus=msg.bus,
                frame_id=msg.frame_id,
                payload_bytes=msg.payload_bytes,
                signals=signals,
                selector=selector,
                always_indices=always_indices,
                branch_map=branch_map,
                unassigned_mask=unassigned_mask,
            )
        )
    return tuple(compiled)


def build_product(messages: Iterable[Mapping[str, Any] | MessageDef]) -> CatalogProduct:
    """Validate definitions and return an immutable, content-addressed product."""

    defs: list[MessageDef] = []
    for raw in messages:
        defs.append(raw if isinstance(raw, MessageDef) else MessageDef.parse(raw))
    content = _canonical_content(defs)
    fingerprint = _content_sha(content)
    compiled = _compile_content(content)
    return CatalogProduct(fingerprint=fingerprint, messages=compiled, content=content)


def load_product(path: str | os.PathLike[str]) -> CatalogProduct:
    """Load a product file, verifying its content hash before compiling."""

    text = Path(path).read_text(encoding="utf-8")
    try:
        manifest = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CatalogError(f"product file is not valid JSON: {exc}", code="bad_product") from exc
    if manifest.get("format") != PRODUCT_FORMAT:
        raise CatalogError(f"unsupported product format {manifest.get('format')!r}", code="bad_product")
    content = manifest.get("content")
    if not isinstance(content, dict):
        raise CatalogError("product has no embedded content", code="bad_product")
    fingerprint = _content_sha(content)
    declared = manifest.get("content_sha256") or manifest.get("fingerprint")
    if declared != fingerprint:
        raise CatalogError(
            f"product content hash mismatch: declared {declared}, computed {fingerprint}",
            code="bad_product_hash",
        )
    compiled = _compile_content(content)
    return CatalogProduct(fingerprint=fingerprint, messages=compiled, content=content)


def product_from_definitions(raw: Mapping[str, Any] | Sequence[Mapping[str, Any]]) -> CatalogProduct:
    """Build a product from either a full catalog document or a message list."""

    if isinstance(raw, Mapping):
        messages = raw.get("messages")
        if messages is None:
            raise CatalogError("catalog document needs 'messages'", code="bad_catalog")
    else:
        messages = raw
    return build_product(messages)
