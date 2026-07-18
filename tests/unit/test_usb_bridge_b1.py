"""Unit tests for B1 transaction hygiene (Phase 3.17).

``_read_validated`` must discard containers from earlier aborted
transactions (foreign txid) and declare the pipe desynced after
``_STALE_READ_LIMIT`` consecutive orphans. No USB hardware involved:
``_read_container`` / ``_drain_bulk_in`` are monkeypatched.
"""

from __future__ import annotations

import pytest

# usb_bridge imports pyusb at module level. pyusb is a runtime dependency
# of the app (pure-Python wheel; libusb is only dialled at find() time),
# but skip cleanly on stripped-down environments rather than erroring.
pytest.importorskip("usb.core")

from fp_l_tether.camera.usb_bridge import (  # noqa: E402
    PTPContainer,
    PTPContainerType,
    USBBridge,
    USBBridgeError,
)


def _bridge_with_reads(monkeypatch, containers):
    """Build a device-less USBBridge whose reads pop from ``containers``."""
    bridge = object.__new__(USBBridge)
    bridge._ep_in = None  # _drain_bulk_in fast-exits on None
    seq = list(containers)

    def fake_read(timeout_ms=None):
        if not seq:
            raise AssertionError("test read past scripted containers")
        return seq.pop(0)

    monkeypatch.setattr(bridge, "_read_container", fake_read)
    return bridge


def _data(txid: int, code: int = 0x9015, payload: bytes = b"\x00") -> PTPContainer:
    return PTPContainer(
        container_type=PTPContainerType.DATA,
        code=code,
        transaction_id=txid,
        payload=payload,
    )


def _resp(txid: int, code: int = 0x2001) -> PTPContainer:
    return PTPContainer(
        container_type=PTPContainerType.RESPONSE,
        code=code,
        transaction_id=txid,
        payload=b"",
    )


def test_matching_txid_passes_through(monkeypatch) -> None:
    bridge = _bridge_with_reads(monkeypatch, [_data(42)])
    got = bridge._read_validated(42, 0x9015)
    assert got.transaction_id == 42
    assert got.is_data


def test_stale_containers_are_skipped(monkeypatch) -> None:
    """An orphaned DATA+RESPONSE pair from txid 41 must be discarded."""
    bridge = _bridge_with_reads(
        monkeypatch, [_data(41), _resp(41), _resp(42)]
    )
    got = bridge._read_validated(42, 0x9015)
    assert got.transaction_id == 42
    assert got.is_response


def test_desync_raises_after_limit(monkeypatch) -> None:
    stale = [_resp(7)] * USBBridge._STALE_READ_LIMIT
    bridge = _bridge_with_reads(monkeypatch, stale)
    drained: list[bool] = []
    with pytest.raises(USBBridgeError, match="desync"):
        bridge._read_validated(42, 0x9015)
    # All scripted stale containers were consumed (none returned).
    assert not drained  # drain happens in send_command_raw, not here


def test_foreign_opcode_on_matching_txid_is_accepted(monkeypatch) -> None:
    """txid is the strong key — a DATA code mismatch only warns."""
    bridge = _bridge_with_reads(monkeypatch, [_data(42, code=0x902B)])
    got = bridge._read_validated(42, 0x9015)
    assert got.transaction_id == 42
    assert got.code == 0x902B


def test_drain_bulk_in_without_endpoint_is_noop() -> None:
    bridge = object.__new__(USBBridge)
    bridge._ep_in = None
    assert bridge._drain_bulk_in() == 0


# ---------------------------------------------------------------------------
# Phase 3.18 (B2) — ZLP absorption in _read_container
# ---------------------------------------------------------------------------

import struct


class _FakeEP:
    wMaxPacketSize = 512

    def __init__(self, reads):
        self._reads = list(reads)

    def read(self, size, timeout=None):
        if not self._reads:
            raise AssertionError("test read past scripted data")
        return self._reads.pop(0)


def _resp_bytes(code: int = 0x2001, txid: int = 9) -> bytes:
    return struct.pack("<IHHI", 12, PTPContainerType.RESPONSE, code, txid)


def test_zlp_before_container_is_absorbed() -> None:
    """A zero-length packet from a packet-aligned previous transfer must
    not be mistaken for a dead camera (B2)."""
    bridge = object.__new__(USBBridge)
    bridge._ep_in = _FakeEP([b"", _resp_bytes()])
    c = bridge._read_container()
    assert c.is_response
    assert c.transaction_id == 9


def test_double_zero_read_still_raises() -> None:
    """Two consecutive 0-byte reads = genuine doze — same error as before."""
    bridge = object.__new__(USBBridge)
    bridge._ep_in = _FakeEP([b"", b""])
    with pytest.raises(USBBridgeError, match="too short"):
        bridge._read_container()
