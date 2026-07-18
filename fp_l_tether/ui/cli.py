"""Typer-based CLI for fp-l-tether.

Subcommands:
    start    Boot the tether daemon (optionally with the floating panel UI).
    shoot    Fire a single PC-triggered shot (requires a running daemon — TODO).
    info     Show config + camera detection without doing anything.
    test     Take N shots in a loop (smoke test).
"""

from __future__ import annotations

import signal
import sys
import time
from pathlib import Path
from typing import Optional

import typer

from fp_l_tether.config import AppConfig, load_config, print_config
from fp_l_tether.telemetry import get_logger, setup_logging
from fp_l_tether.transfer import ShotEvent, TetherDaemon

app = typer.Typer(
    name="fp-l-tether",
    help="Sigma fp L → Mac → Lightroom Classic tethered shooting bridge.",
    add_completion=False,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load(config_path: Optional[Path], verbose: bool) -> AppConfig:
    """Load config, apply CLI-level overrides, set up logging."""
    cfg = load_config(config_path)
    if verbose:
        cfg.telemetry.log_level = "DEBUG"
    setup_logging(cfg.telemetry)
    return cfg


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.command()
def info(
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="path to config.toml"),
) -> None:
    """Show resolved config + try to enumerate the camera."""
    cfg = _load(config, verbose=False)
    print("\nResolved config:")
    print_config(cfg)

    print("\nLooking for Sigma fp / fp L on USB…")
    try:
        from fp_l_tether.camera.usb_bridge import USBBridge
        bridge = USBBridge.find_sigma_fp_l()
        print(f"  ✓ Found: bus={bridge._dev.bus} addr={bridge._dev.address}")
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ {e}")


@app.command()
def start(
    session: str = typer.Option("default", "--session", "-s", help="session name"),
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    panel: bool = typer.Option(True, "--panel/--no-panel",
                                help="Show the floating tether panel"),
    af: Optional[bool] = typer.Option(
        None, "--af/--no-af",
        help="Auto-focus before each shot. Default: use config.toml's "
             "snap_mode (built-in default: no AF / current focus). "
             "--af / --no-af explicitly override the config.",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Boot the tether daemon. Camera shutter button just works™."""
    cfg = _load(config, verbose)
    # Phase 3.15 (A18): only override snap_mode when the user actually
    # passed a flag. The old ``False`` default was indistinguishable
    # from "not passed", so ``fp-l-tether start`` always forced
    # snap_mode=2 and the config.toml knob was dead.
    if af is not None:
        cfg.camera.snap_mode = 1 if af else 2  # 1=GENERAL_CAPTURE, 2=NON_AF_CAPTURE
    log = get_logger("cli")
    log.info("starting", session=session, panel=panel)

    daemon = TetherDaemon(cfg, session_name=session)

    # Shot summary printer
    def _on_shot(e: ShotEvent) -> None:
        print(
            f"  📸 #{e.shot_index:>3}  {e.size:>10,} B  "
            f"{e.elapsed_s:>5.2f}s  {e.mbps:>4.1f} MB/s  "
            f"[{e.trigger}]  → {e.saved_path.name}"
        )

    daemon.on_shot = _on_shot

    # Optional floating panel
    panel_obj = None
    if panel:
        try:
            from fp_l_tether.ui.floating_panel import FloatingTetherPanel
            panel_obj = FloatingTetherPanel(daemon=daemon, cfg=cfg)
        except ImportError as e:
            log.warning("panel_unavailable", error=str(e))
            print(f"  (floating panel disabled: {e})")
        except Exception as e:  # noqa: BLE001
            log.warning("panel_init_failed", error=str(e))
            print(f"  (floating panel failed to init: {e})")

    # Graceful Ctrl+C
    def _signal_handler(signum, frame) -> None:
        print("\n[stopping…]")
        daemon.stop()
        if panel_obj is not None:
            panel_obj.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    daemon.start()
    print(f"\n  Watching session={session!r}. Press Ctrl+C to stop.\n")

    if panel_obj is not None:
        # Run NSApp event loop on the main thread; daemon already runs in a bg thread
        panel_obj.run_forever()
    else:
        # Headless: keep main thread alive
        try:
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            pass

    daemon.stop()


@app.command()
def shoot(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Take one shot (blocking, no daemon). Useful for scripting."""
    cfg = _load(config, verbose)
    log = get_logger("cli")
    log.info("one_shot_mode")

    daemon = TetherDaemon(cfg)
    done = []

    def _on_shot(e: ShotEvent) -> None:
        done.append(e)
        print(f"  ✓ {e.saved_path}  ({e.size:,} B, {e.elapsed_s:.2f}s, {e.mbps:.1f} MB/s)")

    daemon.on_shot = _on_shot
    daemon.start()
    time.sleep(2.0)  # give init time to finish
    daemon.request_snap()

    # Wait up to 30s for the shot to come back
    for _ in range(300):
        if done:
            break
        time.sleep(0.1)
    daemon.stop()

    if not done:
        print("  ✗ no shot received within 30s")
        raise typer.Exit(code=1)


@app.command()
def inspect(
    group: str = typer.Argument(
        "all",
        help=(
            "Which DataGroup to dump: 'focus' | 'movie' | 'cansetinfo5' | "
            "'datagroup1'..'datagroup6' | 'cameraInfo' | 'all'."
        ),
    ),
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    raw: bool = typer.Option(False, "--raw", help="Also dump raw hex"),
    diff: Optional[Path] = typer.Option(
        None, "--diff",
        help="Compare against a previous --save snapshot file.",
    ),
    save: Optional[Path] = typer.Option(
        None, "--save",
        help="Save the current snapshot to this file for later --diff.",
    ),
    compare: list[Path] = typer.Option(
        [],
        "--compare",
        help=(
            "Compare N previously saved snapshots (--compare a.json "
            "--compare b.json --compare c.json ...). Does NOT talk to the "
            "camera; pure file analysis."
        ),
    ),
) -> None:
    """Dump a Sigma DataGroup IFD in human-readable form.

    Used to reverse-engineer the tag → setting mapping. Workflow:

      1. Disconnect camera, change a setting on the body (e.g. AF→MF).
      2. Reconnect.
      3. Run ``fp-l-tether inspect focus --save before.txt`` ← OK before.
      4. Change another setting.
      5. Run ``fp-l-tether inspect focus --diff before.txt`` ← see what changed.
    """
    import json
    cfg = _load(config, verbose)
    log = get_logger("inspect")

    from fp_l_tether.camera.sigma_ifd import parse_ifd
    from fp_l_tether.camera.usb_bridge import USBBridge

    # Mapping group-name → (label, bridge method)
    groups: dict[str, tuple[str, str]] = {
        "cameraInfo":   ("CameraInfo (0x9035)",         "sigma_get_camera_info"),
        "camconfig":    ("GetCamConfig (0x9010)",       "sigma_get_cam_config"),
        "camstatus2":   ("GetCamStatus2 (0x902C)",      "sigma_get_cam_status_2"),
        "cansetinfo5":  ("GetCamCanSetInfo5 (0x9030)",  "sigma_get_cam_can_set_info_5"),
        "focus":        ("GetCamDataGroupFocus (0x9031)", "sigma_get_cam_datagroup_focus"),
        "movie":        ("GetCamDataGroupMovie (0x9033)", "sigma_get_cam_datagroup_movie"),
        "datagroup1":   ("GetDataGroup1 (0x9012)",      None),  # uses sigma_get_datagroup(1)
        "datagroup2":   ("GetDataGroup2 (0x9013)",      None),
        "datagroup3":   ("GetDataGroup3 (0x9014)",      None),
        "datagroup4":   ("GetDataGroup4 (0x9023)",      None),
        "datagroup5":   ("GetDataGroup5 (0x9027)",      None),
        "datagroup6":   ("GetDataGroup6 (0x9029)",      None),
    }

    targets: list[str]
    if group == "all":
        targets = list(groups.keys())
    elif group in groups:
        targets = [group]
    else:
        print(f"  ✗ unknown group {group!r}. Choices: {', '.join(groups)} | all")
        raise typer.Exit(code=2)

    # ----- --compare mode: pure file analysis, no camera I/O ---------
    if compare:
        _compare_snapshots(compare, targets)
        raise typer.Exit(code=0)

    bridge = USBBridge.find_sigma_fp_l()
    bridge.open()
    bridge.open_session()
    log.info("session_opened")

    snapshot: dict[str, dict] = {}

    try:
        bridge.sigma_init()
        log.info("init_complete")

        print()
        for name in targets:
            label, method = groups[name]
            print(f"━━━ {label} ━━━")
            try:
                if method is not None:
                    data = getattr(bridge, method)()
                else:
                    g = int(name[-1])
                    data = bridge.sigma_get_datagroup(g)
            except Exception as e:  # noqa: BLE001
                print(f"  ✗ {e}")
                continue

            if raw:
                print(f"  raw ({len(data)} bytes):")
                for i in range(0, len(data), 16):
                    chunk = data[i : i + 16]
                    hex_str = " ".join(f"{b:02x}" for b in chunk)
                    ascii_str = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
                    print(f"    {i:04x}  {hex_str:<48s}  {ascii_str}")

            try:
                result = parse_ifd(data)
                print(result.format(indent="  "))
                snapshot[name] = {
                    "raw_hex": data.hex(),
                    "entries": [
                        {
                            "tag": f"0x{e.tag:04X}",
                            "type": e.type_name,
                            "count": e.count,
                            "value": (
                                e.value.hex(" ") if isinstance(e.value, bytes)
                                else e.value
                            ),
                        }
                        for e in result.entries
                    ],
                }
            except Exception as e:  # noqa: BLE001
                print(f"  ⚠ not an IFD: {e}")
                snapshot[name] = {"raw_hex": data.hex(), "note": f"not an IFD: {e}"}
            print()

    finally:
        try:
            bridge.close_session()
        except Exception:  # noqa: BLE001
            pass
        bridge.close()

    # --save  → write JSON for later diff
    if save is not None:
        save.parent.mkdir(parents=True, exist_ok=True)
        save.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False))
        print(f"  ✓ saved snapshot → {save}")

    # --diff  → load previous snapshot and print changed values
    if diff is not None:
        if not diff.exists():
            print(f"  ✗ diff file not found: {diff}")
            raise typer.Exit(code=2)
        prev = json.loads(diff.read_text())
        print("━━━ DIFF (changed entries) ━━━")
        for g_name, snap in snapshot.items():
            if g_name not in prev:
                print(f"  {g_name}: NEW (was absent in {diff.name})")
                continue
            prev_by_tag = {e["tag"]: e for e in prev[g_name].get("entries", [])}
            new_by_tag = {e["tag"]: e for e in snap.get("entries", [])}
            changed_tags = []
            for tag, ne in new_by_tag.items():
                pe = prev_by_tag.get(tag)
                if pe is None:
                    changed_tags.append((tag, "(new)", ne["value"]))
                elif pe["value"] != ne["value"]:
                    changed_tags.append((tag, pe["value"], ne["value"]))
            for tag, before_, after_ in changed_tags:
                print(f"  {g_name:12s} {tag}  {before_!r:>30s}  →  {after_!r}")


@app.command("set-af-point")
def set_af_point(
    x: int = typer.Argument(..., help="AF point X coordinate. Range: 96..928."),
    y: int = typer.Argument(..., help="AF point Y coordinate. Range: 85..597."),
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Move the Single-AF point via SetCamDataGroupFocus (0x9032).

    Requires Focus Area = Single on the camera body (set via the menu before
    connecting). The camera's GetCamDataGroupFocus opcode does NOT reflect
    the written value (static cache), so verify by looking at the LCD or by
    capturing a frame and checking the EXIF.

    Preset coordinates (from GetCamCanSetInfo5 tag 0x0265 = Y_min, Y_max,
    X_min, X_max = 85, 597, 96, 928). UI takes (X, Y); the wire byte order
    (Y, X) is handled internally.

      center        512 340
      top-left       96  85
      top-right     928  85
      bottom-left    96 597
      bottom-right  928 597
    """
    _load(config, verbose)
    log = get_logger("set-af-point")

    from fp_l_tether.camera.usb_bridge import USBBridge

    bridge = USBBridge.find_sigma_fp_l()
    bridge.open()
    bridge.open_session()
    log.info("session_opened")
    try:
        bridge.sigma_init()
        log.info("init_complete")
        bridge.sigma_set_cam_datagroup_focus(x, y)
        print(f"  ✓ sent SetCamDataGroupFocus(x={x}, y={y})")
        print("    → check the camera LCD: the AF frame should be at this position.")
    finally:
        try:
            bridge.close_session()
        except Exception:  # noqa: BLE001
            pass
        bridge.close()


@app.command()
def status(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Print current SS / ISO / Aperture / WB read from the camera."""
    _load(config, verbose)

    from fp_l_tether.camera.sigma_datagroup import read_exposure
    from fp_l_tether.camera.usb_bridge import USBBridge

    bridge = USBBridge.find_sigma_fp_l()
    bridge.open()
    bridge.open_session()
    try:
        bridge.sigma_init()
        exposure = read_exposure(bridge)
        print(f"  {exposure.short()}")
        print(
            f"    raw: SS=0x{exposure.shutter_raw:02X}  "
            f"Av=0x{exposure.aperture_raw:02X}  "
            f"ISO=0x{exposure.iso_raw:02X}  "
            f"WB=0x{exposure.wb_raw:02X}"
        )
    finally:
        try:
            bridge.close_session()
        except Exception:  # noqa: BLE001
            pass
        bridge.close()


def _compare_snapshots(files: list[Path], target_groups: list[str]) -> None:
    """N-way snapshot comparison for the AF-point probe and similar tasks.

    For each requested group, builds a table of tag → value-per-file and
    prints only the rows where the value differs across at least two
    snapshots. Stable tags (same value everywhere) are suppressed to keep
    the signal in view.

    Also prints a side-by-side raw-hex view of the underlying payload
    so that fixed-struct DataGroups (DG1/2/4/5/6, which aren't IFDs and
    don't appear via tags) still show their byte-level diff.
    """
    import json

    if len(files) < 2:
        print(f"  ✗ --compare needs at least 2 files (got {len(files)})")
        raise typer.Exit(code=2)

    snapshots: list[tuple[Path, dict]] = []
    for f in files:
        if not f.exists():
            print(f"  ✗ not found: {f}")
            raise typer.Exit(code=2)
        try:
            snapshots.append((f, json.loads(f.read_text())))
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {f}: {e}")
            raise typer.Exit(code=2)

    # Compact short names for the column header
    labels = [f.stem for f, _ in snapshots]
    label_w = max(12, max(len(x) for x in labels) + 2)

    for g_name in target_groups:
        if not any(g_name in snap for _, snap in snapshots):
            continue  # group not present in any of these files

        print(f"━━━ {g_name} ━━━")

        # --- tag-by-tag (IFD groups: focus, movie, cansetinfo5) -----
        # Build {tag → [value_per_file]}; missing entries shown as ∅.
        tag_values: dict[str, list[str]] = {}
        for _, snap in snapshots:
            entries = snap.get(g_name, {}).get("entries", []) or []
            seen = {e["tag"]: e for e in entries}
            for tag in list(seen) + [t for t in tag_values if t not in seen]:
                tag_values.setdefault(tag, [])
            for tag in tag_values:
                if tag in seen:
                    tag_values[tag].append(_short_val(seen[tag]["value"]))
                else:
                    tag_values[tag].append("∅")

        if tag_values:
            # Header
            header = "tag       " + "".join(f"{lbl:<{label_w}s}" for lbl in labels)
            print(f"  {header}")
            # Only rows where at least 2 values differ
            for tag in sorted(tag_values):
                vals = tag_values[tag]
                if len(set(vals)) == 1:
                    continue
                row = f"{tag:9s} " + "".join(f"{v:<{label_w}s}" for v in vals)
                print(f"  {row}")

        # --- raw-hex byte-by-byte (fixed-struct DataGroups) ---------
        # If we have raw_hex for any file, do a per-byte comparison too.
        raws = []
        for f, snap in snapshots:
            hx = snap.get(g_name, {}).get("raw_hex")
            raws.append(bytes.fromhex(hx) if hx else None)

        if any(r is not None for r in raws):
            min_len = min((len(r) for r in raws if r is not None), default=0)
            changed_offsets = []
            for off in range(min_len):
                col = [r[off] if r is not None else None for r in raws]
                col_present = [c for c in col if c is not None]
                if len(set(col_present)) > 1:
                    changed_offsets.append(off)
            if changed_offsets:
                print(f"  raw byte diffs:")
                hdr = "off  " + "".join(f"{lbl:<{label_w}s}" for lbl in labels)
                print(f"    {hdr}")
                for off in changed_offsets:
                    cells = []
                    for r in raws:
                        cells.append(
                            f"{r[off]:02x}" if (r is not None and off < len(r))
                            else "--"
                        )
                    row = f"{off:04x} " + "".join(f"{c:<{label_w}s}" for c in cells)
                    print(f"    {row}")
        print()


def _short_val(v) -> str:  # type: ignore[no-untyped-def]
    """Render a snapshot value compactly for table columns."""
    if isinstance(v, list):
        s = ",".join(str(x) for x in v)
    else:
        s = str(v)
    if len(s) > 16:
        s = s[:13] + "…"
    return s


@app.command()
def test(
    shots: int = typer.Option(3, "--shots", "-n", help="number of shots to take"),
    gap: float = typer.Option(2.0, "--gap", help="seconds between shots"),
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """PC-triggered burst test (no camera-button interaction)."""
    cfg = _load(config, verbose)
    log = get_logger("cli")
    log.info("burst_test", shots=shots, gap=gap)

    daemon = TetherDaemon(cfg)
    received: list[ShotEvent] = []

    def _on_shot(e: ShotEvent) -> None:
        received.append(e)
        print(f"  ✓ #{e.shot_index} {e.saved_path.name} ({e.size:,} B, {e.elapsed_s:.2f}s)")

    daemon.on_shot = _on_shot
    daemon.start()
    time.sleep(3.0)  # let init complete

    for _ in range(shots):
        daemon.request_snap()
        time.sleep(gap)

    # Wait for last shot to land
    time.sleep(5.0)
    daemon.stop()

    print(f"\nReceived {len(received)}/{shots} shots")
    if len(received) < shots:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
