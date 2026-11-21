"""Publishable readout definitions over an immutable catalog product.

A *readout definition* is the bindery's published interpretation of one
catalog message: which selector branches mean ``pickup`` / ``empty_punch`` /
``pull_last`` / ``seal``, and which always-active unsigned signals carry the
material table, the machine sequence number and (optionally) the book number.

The definition is content addressed independently of the catalog, but it
records the catalog product's SHA-256.  The same raw payload therefore has a
separate, permanent interpretation per published product version: a ``v2``
readout never overwrites what ``v1`` said, and both remain queryable.

Identifier fields are read as raw integer bit patterns (no factor/offset
scaling): a table id or a machine sequence number is an identifier, not a
measurement.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .catalog import CatalogError, CatalogProduct, DecodeError

READOUT_FORMAT = "bitcatalog-readout/1"

ACTION_PICKUP = "pickup"
ACTION_EMPTY_PUNCH = "empty_punch"
ACTION_PULL_LAST = "pull_last"
ACTION_SEAL = "seal"
ACTIONS = (ACTION_PICKUP, ACTION_EMPTY_PUNCH, ACTION_PULL_LAST, ACTION_SEAL)


class ReadoutError(CatalogError):
    """A readout definition or a frame interpretation is rejected."""

    def __init__(self, message: str, *, code: str = "readout_error", **detail: Any):
        super().__init__(message, code=code, **detail)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def readout_fingerprint(content: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(content).encode("utf-8")).hexdigest()


def _require_signal(message, name: str, role: str) -> Any:
    for sig in message.signals:
        if sig.name == name:
            if sig.branches:
                raise ReadoutError(
                    f"{role} signal {name!r} must be always active, not branch-only",
                    code="branch_identifier",
                )
            if sig.signed:
                raise ReadoutError(
                    f"{role} signal {name!r} must be unsigned",
                    code="signed_identifier",
                )
            return sig
    raise ReadoutError(
        f"message {message.key} has no signal {name!r} for the {role}",
        code="unknown_signal",
    )


@dataclass(frozen=True)
class ReadoutDefinition:
    """A published, content-addressed interpretation of one catalog message."""

    product_sha256: str
    bus: str
    frame_id: int
    actions: tuple[tuple[int, str], ...]
    table_signal: str
    machine_seq_signal: str
    book_signal: str | None = None
    machine_signal: str | None = None
    selector_name: str | None = None

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> "ReadoutDefinition":
        if not isinstance(raw, Mapping):
            raise ReadoutError("readout definition must be an object", code="bad_definition")
        sha = raw.get("product_sha256")
        if not isinstance(sha, str) or len(sha) != 64:
            raise ReadoutError("product_sha256 must be a 64-char hex digest", code="bad_product")
        bus = raw.get("bus")
        if not isinstance(bus, str) or not bus:
            raise ReadoutError("readout needs a non-empty bus", code="bad_bus")
        frame_id = raw.get("frame_id")
        if isinstance(frame_id, bool) or not isinstance(frame_id, int):
            raise ReadoutError("frame_id must be an integer", code="bad_frame_id")
        raw_actions = raw.get("actions")
        if not isinstance(raw_actions, Mapping) or not raw_actions:
            raise ReadoutError("actions must map selector values to action names", code="bad_actions")
        parsed: list[tuple[int, str]] = []
        seen: set[int] = set()
        for key, value in raw_actions.items():
            try:
                selector_value = int(key)
            except (TypeError, ValueError):
                raise ReadoutError(f"action key {key!r} is not an integer", code="bad_actions")
            if selector_value in seen:
                raise ReadoutError(f"duplicate action for selector {selector_value}", code="bad_actions")
            if value not in ACTIONS:
                raise ReadoutError(f"unknown action {value!r}", code="bad_actions")
            seen.add(selector_value)
            parsed.append((selector_value, value))
        table_signal = raw.get("table_signal")
        machine_seq_signal = raw.get("machine_seq_signal")
        if not isinstance(table_signal, str) or not table_signal:
            raise ReadoutError("table_signal is required", code="bad_signal")
        if not isinstance(machine_seq_signal, str) or not machine_seq_signal:
            raise ReadoutError("machine_seq_signal is required", code="bad_signal")
        book_signal = raw.get("book_signal")
        if book_signal is not None and (not isinstance(book_signal, str) or not book_signal):
            raise ReadoutError("book_signal must be a non-empty string when present", code="bad_signal")
        machine_signal = raw.get("machine_signal")
        if machine_signal is not None and (not isinstance(machine_signal, str) or not machine_signal):
            raise ReadoutError("machine_signal must be a non-empty string when present", code="bad_signal")
        selector_name = raw.get("selector_name")
        if selector_name is not None and (not isinstance(selector_name, str) or not selector_name):
            raise ReadoutError("selector_name must be a non-empty string when present", code="bad_selector")
        return cls(
            product_sha256=sha,
            bus=bus,
            frame_id=frame_id,
            actions=tuple(sorted(parsed)),
            table_signal=table_signal,
            machine_seq_signal=machine_seq_signal,
            book_signal=book_signal,
            machine_signal=machine_signal,
            selector_name=selector_name,
        )

    @property
    def action_map(self) -> dict[int, str]:
        return dict(self.actions)

    def content(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "format": READOUT_FORMAT,
            "product_sha256": self.product_sha256,
            "bus": self.bus,
            "frame_id": self.frame_id,
            "actions": {str(k): v for k, v in self.actions},
            "table_signal": self.table_signal,
            "machine_seq_signal": self.machine_seq_signal,
        }
        if self.book_signal is not None:
            out["book_signal"] = self.book_signal
        if self.machine_signal is not None:
            out["machine_signal"] = self.machine_signal
        if self.selector_name is not None:
            out["selector_name"] = self.selector_name
        return out

    @property
    def fingerprint(self) -> str:
        return readout_fingerprint(self.content())

    def save(self, path: str | os.PathLike[str]) -> str:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        manifest = {
            "format": READOUT_FORMAT,
            "fingerprint": self.fingerprint,
            "definition": self.content(),
        }
        text = json.dumps(manifest, sort_keys=True, ensure_ascii=False, indent=1)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(text + "\n", encoding="utf-8")
        os.replace(tmp, target)
        return str(target)

    def compile(self, product: CatalogProduct) -> "ReadoutCodec":
        if product.fingerprint != self.product_sha256:
            raise ReadoutError(
                f"readout is bound to product {self.product_sha256}, got {product.fingerprint}",
                code="wrong_product",
            )
        try:
            message = product.message(self.bus, self.frame_id)
        except DecodeError as exc:
            raise ReadoutError(str(exc), code=exc.code) from exc
        if message.selector is None:
            raise ReadoutError("readout message has no selector", code="no_selector")
        if self.selector_name is not None and self.selector_name != message.selector.name:
            raise ReadoutError(
                f"readout expects selector {self.selector_name!r}, message has {message.selector.name!r}",
                code="wrong_selector",
            )
        action_map = dict(self.actions)
        if any(value > message.selector.mask for value in action_map):
            raise ReadoutError("action selector value does not fit the selector width", code="bad_actions")
        _require_signal(message, self.table_signal, "table")
        _require_signal(message, self.machine_seq_signal, "machine sequence")
        if self.book_signal is not None:
            _require_signal(message, self.book_signal, "book")
        if self.machine_signal is not None:
            _require_signal(message, self.machine_signal, "machine")
        return ReadoutCodec(definition=self, message=message)


@dataclass(frozen=True)
class ReadoutFrame:
    """One frame interpreted through a published readout definition."""

    frame_event_id: str
    product_sha256: str
    bus: str
    frame_id: int
    occurred_at_ns: int
    machine: str
    machine_seq: int
    table: int
    book: int | None
    action: str | None
    selector: int
    payload_hex: str

    @property
    def semantic_key(self) -> tuple[Any, ...]:
        return (
            self.machine,
            self.machine_seq,
            self.action,
            self.table,
            self.book,
            self.payload_hex,
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "frame_event_id": self.frame_event_id,
            "product_sha256": self.product_sha256,
            "bus": self.bus,
            "frame_id": self.frame_id,
            "occurred_at_ns": self.occurred_at_ns,
            "machine": self.machine,
            "machine_seq": self.machine_seq,
            "table": self.table,
            "book": self.book,
            "action": self.action,
            "selector": self.selector,
            "payload_hex": self.payload_hex,
        }


class ReadoutCodec:
    """Decode raw payloads with one published readout definition."""

    def __init__(self, *, definition: ReadoutDefinition, message: Any):
        self.definition = definition
        self.message = message
        self._action_map = definition.action_map
        self._by_name = {s.name: s for s in message.signals}

    def _raw(self, payload: bytes, name: str) -> int:
        sig = self._by_name[name]
        return sig.raw_to_value(sig.extract_raw(payload))

    def decode(self, payload: bytes, *, frame_event_id: str, occurred_at_ns: int) -> ReadoutFrame:
        if len(payload) != self.message.payload_bytes:
            raise ReadoutError(
                f"payload length {len(payload)} != {self.message.payload_bytes}",
                code="bad_length",
                position=self.message.payload_bytes,
            )
        sel = self.message.selector.read(payload) if self.message.selector else None
        if sel is None:
            raise ReadoutError("message has no selector", code="no_selector")
        table = self._raw(payload, self.definition.table_signal)
        machine_seq = self._raw(payload, self.definition.machine_seq_signal)
        book = self._raw(payload, self.definition.book_signal) if self.definition.book_signal else None
        machine = (
            str(self._raw(payload, self.definition.machine_signal))
            if self.definition.machine_signal
            else self.definition.bus
        )
        return ReadoutFrame(
            frame_event_id=frame_event_id,
            product_sha256=self.definition.product_sha256,
            bus=self.definition.bus,
            frame_id=self.definition.frame_id,
            occurred_at_ns=occurred_at_ns,
            machine=machine,
            machine_seq=machine_seq,
            table=table,
            book=book,
            action=self._action_map.get(sel),
            selector=sel,
            payload_hex=payload.hex(),
        )


def load_readout(path: str | os.PathLike[str], *, product: CatalogProduct | None = None) -> ReadoutDefinition:
    text = Path(path).read_text(encoding="utf-8")
    try:
        manifest = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReadoutError(f"readout file is not valid JSON: {exc}", code="bad_readout") from exc
    if manifest.get("format") != READOUT_FORMAT:
        raise ReadoutError(f"unsupported readout format {manifest.get('format')!r}", code="bad_readout")
    content = manifest.get("definition")
    if not isinstance(content, Mapping):
        raise ReadoutError("readout file has no definition", code="bad_readout")
    actual = readout_fingerprint(content)
    declared = manifest.get("fingerprint")
    if declared != actual:
        raise ReadoutError(
            f"readout fingerprint mismatch: declared {declared}, computed {actual}",
            code="bad_readout_hash",
        )
    definition = ReadoutDefinition.parse(content)
    if product is not None:
        definition.compile(product)
    return definition


def publish_readout(product: CatalogProduct, raw: Mapping[str, Any], path: str | os.PathLike[str]) -> tuple[ReadoutDefinition, str]:
    """Validate a raw readout spec against ``product`` and save it."""

    content = dict(raw)
    content["product_sha256"] = product.fingerprint
    definition = ReadoutDefinition.parse(content)
    definition.compile(product)
    return definition, definition.save(path)


def decode_warehouse(warehouse: Any, definition: ReadoutDefinition) -> list[ReadoutFrame]:
    """Decode every accepted stored frame *of the definition's product version*.

    The warehouse is an :class:`~bitcatalog.warehouse.ObservationWarehouse`.
    Binding the readout to one product SHA means a late frame re-interpreted
    under ``v2`` never disturbs the ``v1`` physical result.
    """

    product = warehouse.product(definition.product_sha256)
    codec = definition.compile(product)
    frames: list[ReadoutFrame] = []
    for stored in warehouse.frames(product, status="ok"):
        if stored.bus != definition.bus or stored.frame_id != definition.frame_id:
            continue
        frames.append(
            codec.decode(
                stored.payload,
                frame_event_id=stored.frame_event_id,
                occurred_at_ns=stored.occurred_at_ns,
            )
        )
    return frames
