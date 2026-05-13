# fp-l-tether

> **A working Python tether implementation for the Sigma fp L on macOS — no Capture One required.**
> Sigma fp L のテザー撮影を macOS で動かす Python 実装（Capture One 不要、月額不要）。

[![Status: Phase 0-C clear](https://img.shields.io/badge/status-Phase%200--C%20clear-success)]()
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue)]()
[![macOS](https://img.shields.io/badge/macOS-12+-lightgrey)]()
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow)](LICENSE)

---

## What this is

A native Python + libusb implementation of Sigma's vendor-specific PTP
protocol for the **Sigma fp L**, targeting macOS. The end-goal is a small
menubar app that drops each shot into a Lightroom Classic Auto Import
folder, but the core breakthrough is the reverse-engineered capture
sequence itself.

**Verified working as of 2026-05-12**: 5 consecutive shots, all unique
26 MB JPEG files, ~5 MB/s sustained transfer, no clone-return bug
(libgphoto2 Issue #882).

---

## Why this project exists

Tethered shooting with the Sigma fp L on macOS has been a known pain
point since the camera shipped in 2021:

| Alternative | Why it doesn't work |
|---|---|
| Capture One Pro | Paid subscription, fp L "settings reset" bug |
| Lightroom Classic native tether | No Sigma support |
| libgphoto2 / gphoto2 CLI | `camera_init` double-free aborts immediately on fp L |
| Darktable | Wraps libgphoto2; same crash |
| Sigma Camera Control SDK (2020) | Built before fp L launched; sample app returns `0xA081 NOTINITIALIZED` on fp L |
| sigma-ptpy | fp only, fp L untested, libusb permissions issues on macOS |
| Smart Shooter / Cascable / Sofortbild | No Sigma support at all |
| SD-card workflow | Arca-Swiss plate blocks the SD door |
| Mass Storage mode | Camera locks while mounted, can't shoot |

This project documents and implements the *actually-working* path.

---

## The five reverse-engineered insights that made it work

These are the load-bearing facts; everything else falls out from them.
Documented in [docs/PHASE0_LOG.md](docs/PHASE0_LOG.md) and
[fp_l_tether/camera/usb_bridge.py](fp_l_tether/camera/usb_bridge.py).

1. **`GetCaptureStatus(p1=N)` returns the status of *slot N*, not a global
   state.** Polling slot 0 forever (as libgphoto2 does) only works for
   the first shot because that's where `image_db_head` starts. Subsequent
   shots land in slots 1, 2, 3… — you must poll the slot pointed to by
   `pre_status.image_db_head`.

2. **`SetDataGroup3 (0x9018)` enables PC capture mode.** Sending the
   22-byte payload
   `03 00 80 02 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 85`
   (FieldPresent1=0x03, DestinationToSave=0x80) during init is essential.
   Extracted from libgphoto2's reverse-engineered fp trace at
   `cameras/sigma-fp.txt`.

3. **`Snap` mode = 2 (NON_AF_CAPTURE).** libgphoto2 hard-codes mode=1
   which only fires the first shot. The Sigma SDK headers and the fp
   trace agree on mode=2. Wire bytes: `02 02 01 05`.

4. **`GetCaptureStatus` wire response is 8 bytes, not 7.**
   Layout: `[0x06 length, imageid, db_head, db_tail, status_lo, status_hi, dest, chk]`.
   libgphoto2's parser has an off-by-one between `status_hi` and `dest`.

5. **`brew install gphoto2` aborts immediately on fp L.** The
   `ptp_sigma_fp_9035` helper frees its data buffer internally, then
   `camera_init` frees it again → `___BUG_IN_CLIENT_OF_LIBMALLOC_POINTER_BEING_FREED_WAS_NOT_ALLOCATED`.
   This is a libgphoto2 bug, not an fp L incompatibility.

---

## Requirements

- macOS 12 (Monterey) or later (tested on macOS 26.3.1 Apple Silicon)
- Python 3.11+
- [Homebrew](https://brew.sh) + `libusb`: `brew install libusb`
- Sigma fp L in **Camera Control** USB mode (Menu → System → USB mode)
- USB-C cable, directly to Mac (avoid hubs for tether reliability)
- `sudo` for libusb kernel driver detach

Optional:
- Adobe Lightroom Classic (for the Auto Import folder workflow)

---

## Quick start

### Install

```bash
git clone https://github.com/<your-username>/fp-l-tether.git
cd fp-l-tether
python3 -m venv venv
source venv/bin/activate
pip install -e .
```

### Connect the camera

1. Power **OFF** the fp L
2. CINE/STILL switch → **STILL**
3. Power **ON**
4. MENU → System → **USB mode → Camera Control**
5. Power **OFF**
6. Plug USB-C into Mac
7. Power **ON**

### Test the full capture cycle (Phase 0-C)

```bash
# Take 5 shots into ./captures/
sudo venv/bin/python scripts/phase0_capture_v2.py --shots 5 --gap 2

# Expected: SUCCESS — all 5 shot(s) captured & saved
```

If macOS's `ptpcamerad` is holding the camera, kill it before running:

```bash
sudo killall ptpcamerad
```

---

## Project structure

```
fp_l_tether/                         # Python package
  camera/
    ptp_codes.py                     # Sigma opcodes + dataclasses + wire parsers
    usb_bridge.py                    # libusb PTP transport + sigma_capture_one()
    ic_bridge.py                     # ImageCaptureCore enumeration (limited)
  transfer/, lightroom/, ui/, …      # Phase 1 (in progress)

scripts/
  phase0_smoke_test.py               # USB enumeration sanity check
  phase0_session_test.py             # PTP session open + ConfigApi probe
  phase0_capture_v2.py               # Full capture + download cycle (WORKING)
  phase0_diag_post_capture.py        # Diagnostic for camera state introspection

tests/unit/                          # Pure-Python tests (no camera needed)
docs/                                # SETUP, LIGHTROOM, PHASE0_LOG, TROUBLESHOOTING
DESIGN_PLAN.md                       # Architecture & rationale
CLAUDE.md                            # Context for AI-assisted continuation
```

---

## Status

| Phase | Status |
|---|---|
| **0-A** Camera enumeration | ✅ Clear |
| **0-B** PTP session + ConfigApi | ✅ Clear |
| **0-C** Snap → poll → download → clear | ✅ Clear (5/5 shots @ 5 MB/s) |
| **1.0** CLI + Lightroom Auto Import | 🚧 In progress |
| **1.1** Menubar UI (rumps) | ⏳ Planned |
| **1.2** Hot-plug / reconnect handling | ⏳ Planned |
| **2.0** Live view (0x902b) + DNG support | ⏳ Future |

---

## Contributing

This is a personal project, but PRs and issues are welcome — especially
from other fp / fp L owners. If you've successfully tethered the fp L on
Linux or Windows, your protocol notes would be invaluable.

If you have a working USB packet capture of Capture One Pro talking to
an fp L, please open an issue. That would let us confirm or refute the
"slot N polling" finding and potentially reveal Live View and AF control.

---

## Acknowledgements

This work would not have been possible without:

- **libgphoto2** — both as a reference implementation
  (`camlibs/ptp2/ptp.c` `ptp_sigma_fp_*`) and as a counter-example
  showing what *doesn't* work for the fp L specifically. Special thanks
  to the contributor who reverse-engineered and committed
  `cameras/sigma-fp.txt` — that single file unlocked the entire
  protocol.
- **The `sigma-ptpy` project** (makanikai) for the early fp groundwork.
- **The Sigma Camera Control SDK** (2020-07) for documenting opcode
  numbers and struct shapes, even where its sample app no longer runs.

---

## License

[MIT](LICENSE).

"SIGMA", "fp", and "fp L" are trademarks of SIGMA Corporation. This
project is not affiliated with or endorsed by SIGMA Corporation. The PTP
wire format details documented here were obtained through clean-room
reverse engineering of public USB traffic, in line with the
interoperability provisions of 17 U.S.C. § 1201(f) and EU Directive
2009/24/EC Article 6.
