"""Preserve camera-side user settings across PC tether handshake.

The Sigma fp / fp L's documented behaviour, confirmed by community
reports and our own observation: switching the camera
into PC capture mode overwrites several user-settable fields with
hard-coded defaults baked into the firmware's "PC mode" template.

Concretely, our ``sigma_init()`` runs four ``SetDataGroup*_pc_mode``
writes. Of those:

  - DG2 PC mode write hard-codes ``DriveMode = 0``, ``SpecialMode = 0``,
    ``FlashMode = 4`` (3-byte payload ``04 00 04`` with FP bits
    DriveMode+SpecialMode+FlashMode).
  - DG3 sets ``DestinationToSave = 0x80`` (PC+card). **This bit is
    required for PC capture to work**, so we never touch it post-init.
  - DG4 PC mode write hard-codes 3 bytes whose semantics are not yet
    reverse-engineered. Possibly the source of "settings I didn't
    expect to lose" reports.
  - DG-Movie writes 21 bytes of video-related state. Out of scope for
    still capture preservation.

Strategy
--------
Right after :meth:`USBBridge.open_session` (but BEFORE :meth:`sigma_init`
runs the PC-mode set calls), snapshot DG1+DG2 by reading the raw
groups and decoding their FieldPresent-aware payloads. After init
completes, replay the user-set fields via the existing per-field
:meth:`sigma_set_datagroup_1` / :meth:`sigma_set_datagroup_2` setters.
DG3's PC capture bit is preserved by virtue of never being touched.

Per-field restoration (rather than raw-bytes blit) means we don't
clobber any bits init legitimately needs and we get clear per-field
diagnostic logging when the camera rejects a particular combination
(e.g. trying to write ShutterSpeed while in Auto-exposure mode).

Failure handling is deliberately lenient: if the snapshot or any
restore step throws, we log a warning and continue. A camera with
partially-preserved settings is still strictly better than today's
"everything reset" baseline, and the user can re-dial any missing
field by hand.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from fp_l_tether.camera.sigma_datagroup import (
    parse_datagroup1,
    parse_datagroup2,
)

if TYPE_CHECKING:
    from fp_l_tether.camera.usb_bridge import USBBridge


# DG1 fields that represent user-dialed exposure state. ShutterSpeed,
# Aperture, ExpComp and ISOSpeed are the obvious ones; AB* covers AEB
# bracketing presets the user may have configured. ProgramShift carries
# over the user's manual P-shift offset.
DG1_PRESERVE: tuple[str, ...] = (
    "ShutterSpeed",
    "Aperture",
    "ProgramShift",
    "ISOAuto",
    "ISOSpeed",
    "ExpComp",
    "ABValue",
    "ABSetting",
)

# DG2 fields the PC-mode init explicitly clobbers (DriveMode, SpecialMode,
# FlashMode) plus the camera-state fields most users care about (WB,
# ImageQuality, Resolution, ExposureMode, AEMeteringMode, FlashSetting).
DG2_PRESERVE: tuple[str, ...] = (
    "DriveMode",
    "SpecialMode",
    "ExposureMode",
    "AEMeteringMode",
    "FlashMode",
    "FlashSetting",
    "WhiteBalance",
    "Resolution",
    "ImageQuality",
)


@dataclass
class UserSettings:
    """A snapshot of camera-side user settings, ready to replay post-init."""

    dg1: dict[str, int] = field(default_factory=dict)
    dg2: dict[str, int] = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not self.dg1 and not self.dg2

    def summary(self) -> dict[str, Any]:
        """Compact dict for structured logging."""
        return {
            "dg1_fields": sorted(self.dg1.keys()),
            "dg2_fields": sorted(self.dg2.keys()),
            **{f"dg1.{k}": v for k, v in self.dg1.items()},
            **{f"dg2.{k}": v for k, v in self.dg2.items()},
        }


def snapshot_user_settings(bridge: "USBBridge") -> UserSettings:
    """Read DG1+DG2 and extract preserve-list fields.

    Called right after ``open_session()``, BEFORE ``sigma_init()``
    writes the PC-mode template bytes. Captures the user's dialled
    settings so we can replay them once PC mode is up.

    Raises
    ------
    USBBridgeError / PTPError
        If the camera rejects the GetDataGroup read entirely. Snapshot
        is all-or-nothing for the read step — partial reads from a
        broken DG don't make sense to restore from.
    """
    raw_dg1 = bridge.sigma_get_datagroup(1)
    raw_dg2 = bridge.sigma_get_datagroup(2)
    parsed_dg1 = parse_datagroup1(raw_dg1)
    parsed_dg2 = parse_datagroup2(raw_dg2)
    return UserSettings(
        dg1={k: v for k, v in parsed_dg1.items() if k in DG1_PRESERVE},
        dg2={k: v for k, v in parsed_dg2.items() if k in DG2_PRESERVE},
    )


def restore_user_settings(
    bridge: "USBBridge",
    snap: UserSettings,
    *,
    log: Any = None,
) -> dict[str, tuple[bool, str]]:
    """Write the snapshot fields back to the camera via per-field setters.

    Each DataGroup is replayed as a single SetCamDataGroup call so the
    camera applies all the fields atomically with one checksum. Per-group
    failures are isolated (a rejected DG2 doesn't prevent DG1 from
    restoring) and logged but never raised — partial preservation is
    strictly better than none.

    Returns
    -------
    dict[str, (ok, message)]
        ``{"dg1": (True, "8 fields"), "dg2": (False, "PTP error 0x2002")}``
        for diagnostic / status emission.
    """
    out: dict[str, tuple[bool, str]] = {}

    if snap.dg1:
        try:
            bridge.sigma_set_datagroup_1(snap.dg1)
            out["dg1"] = (True, f"{len(snap.dg1)} fields restored")
            if log is not None:
                log.info("restore_dg1_ok", fields=sorted(snap.dg1.keys()))
        except Exception as e:  # noqa: BLE001
            out["dg1"] = (False, str(e))
            if log is not None:
                log.warning("restore_dg1_failed", error=str(e),
                            fields=sorted(snap.dg1.keys()))

    if snap.dg2:
        try:
            bridge.sigma_set_datagroup_2(snap.dg2)
            out["dg2"] = (True, f"{len(snap.dg2)} fields restored")
            if log is not None:
                log.info("restore_dg2_ok", fields=sorted(snap.dg2.keys()))
        except Exception as e:  # noqa: BLE001
            out["dg2"] = (False, str(e))
            if log is not None:
                log.warning("restore_dg2_failed", error=str(e),
                            fields=sorted(snap.dg2.keys()))

    return out
