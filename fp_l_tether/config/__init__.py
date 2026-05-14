"""Configuration loader & validator for fp-l-tether.

Reads ``config.toml`` (or falls back to built-in defaults) and validates it
with Pydantic. Designed so the app starts even if no config file exists.

Usage::

    from fp_l_tether.config import load_config

    cfg = load_config()                       # default search path
    cfg = load_config("/path/to/config.toml") # explicit
    print(cfg.lightroom.watch_folder)         # resolved Path
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Literal

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

from pydantic import BaseModel, Field, field_validator

PROJECT_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------


class CameraConfig(BaseModel):
    """Camera-side knobs."""

    serial_number: str = ""
    poll_idle_ms: int = 200
    poll_active_ms: int = 100
    chunk_size: int = 1_048_576
    clear_image_db_after_download: bool = True

    # SnapCommand CaptureMode (verified by experiment)
    #   1 = GENERAL_CAPTURE       — AF + shutter
    #   2 = NON_AF_CAPTURE        — skip AF, use current focus (default 2026-05-13)
    #   3 = AF_DRIVE_ONLY         — focus only, no shutter
    #   6 = START_CAPTURE         — begin sequence shooting
    #
    # Phase 3.9 (2026-05-13): defaulted to 2. The floating panel has a
    # separate AF button + LV-click-to-AF for focus control, so making
    # Shoot include AF was an unnecessary duplication that also caused
    # unwanted refocusing when the photographer had already locked focus.
    snap_mode: int = 2

    # USB keep-alive heartbeat. The fp L slips into an internal
    # power-saving state after ~5 min of bus idle (even in PC capture
    # mode) and starts returning 0-byte data phases for vendor get-*
    # opcodes. A periodic benign ping (sigma_get_camera_info) keeps
    # the camera awake; failures are logged but not retried since
    # the watcher's recovery path will catch any drift on the next
    # status poll.
    keep_alive_enabled: bool = True
    # Phase 3.6 (2026-05-13): bumped 60.0 → 30.0. Empirically the
    # camera entered idle ~1:47 after the last shot during the
    # endurance retest even with LV streaming at 10 fps, so a 60 s
    # heartbeat is too sparse — by the time the next ping fires the
    # camera may already be half-asleep, and the ping itself can
    # tip a half-stalled endpoint into Errno 60.
    keep_alive_interval_s: float = 30.0

    # Phase 3.6 Plan T (2026-05-13): aggressive keep-alive. When
    # enabled, the heartbeat sends SnapCommand(mode=AF_DRIVE_ONLY,
    # amount=0) instead of the passive camera_info query. Empirically
    # the shot operation is the only opcode family that resets the
    # fp L's internal doze timer (4 min of inter-shot quiet stayed
    # awake during the 0x903a probe). Side effects:
    #  - Slight AF motor sound every interval_s seconds
    #  - May briefly re-focus during static subjects
    # Disable in studio scenarios with critical focus stability.
    # Kept for backward compat — newer code should set
    # ``keepalive_strategy = "af_drive_only"`` instead.
    aggressive_keepalive: bool = False
    # When aggressive_keepalive is on, this overrides keep_alive_interval_s.
    # 50 s is below the observed doze threshold (75-107 s) with safety
    # margin, but spread out enough to minimise AF wear.
    aggressive_keepalive_interval_s: float = 50.0

    # Phase 3.7 Plan U (2026-05-13): heartbeat strategy selector.
    #
    #   "info"            — passive ``sigma_get_camera_info`` (default;
    #                       cheap, zero side effects, but doesn't prevent
    #                       doze — Phase 3.6 baseline 75-107 s).
    #   "af_drive_only"   — ``SnapCommand(mode=3, amount=1)`` AF-only
    #                       shutter (Plan T, 153 s until doze). Best
    #                       known result. Side effect: brief AF motor
    #                       whirr every interval_s.
    #   "af_point_jiggle" — ``SetCamDataGroupFocus`` (0x9032) writes the
    #                       current AF point shifted by ±1 px and then
    #                       restored. Hypothesis: a Sigma DataGroup
    #                       write counts as a "user touch" for the
    #                       doze timer the same way Snap does, without
    #                       the AF motor cost. UNTESTED 2026-05-13;
    #                       observed side effect is a single-pixel
    #                       flicker of the AF reticle.
    #
    # When ``aggressive_keepalive=True`` is set in older configs, it
    # is interpreted as ``"af_drive_only"`` for backward compatibility.
    keepalive_strategy: Literal["info", "af_drive_only", "af_point_jiggle"] = "info"
    # Pixel delta for the jiggle strategy. 1 px is invisible in normal
    # framing; >1 may be more reliable at touching the doze timer but
    # makes the AF reticle flicker more visible on LV.
    af_jiggle_delta_px: int = 1

    # Phase 3.8 (2026-05-13): preserve user-dialed camera settings
    # across the PC tether handshake. The fp / fp L's well-known
    # behaviour is that switching to PC capture mode overwrites
    # several user fields (DriveMode, SpecialMode, FlashMode, possibly
    # others) with hard-coded defaults — Capture One also has this
    # issue. We work around it by reading DG1+DG2 right after
    # open_session, running the normal sigma_init, then replaying
    # the saved fields via the per-field SetCamDataGroup setters.
    #
    # Failure is non-fatal: any individual field that the camera
    # rejects (e.g. ShutterSpeed write in Auto-exposure mode) is
    # logged and skipped — the user can re-dial that field by hand.
    #
    # Disable this if you actually want PC tether to reset settings
    # to defaults (rare, e.g. studio reproducibility scenarios).
    preserve_user_settings: bool = True

    # Burst-aware post-capture quiet window (see TetherDaemon._arm_quiet_window).
    # After a burst settles (snap queue drains), the daemon enforces
    # a no-PTP-traffic window of
    #   ``commit_window_base_s + commit_window_per_shot_s * (burst-1)``
    # to let the camera finish its internal ImageDB consolidation
    # without being pushed into a wedged endpoint by retries.
    # ``burst`` is the number of shots in the just-finished
    # contiguous run (counter resets when the window arms). A
    # contiguous run = shots downloaded back-to-back without a
    # quiet window between them, regardless of wall-clock gap.
    # Tuned against the 2026-05-13 10-shot live test (~13 s
    # commit tail observed → per-shot=1.0 picks 14 s of safety).
    commit_window_base_s: float = 5.0
    commit_window_per_shot_s: float = 1.0

    # Phase 3.7 (2026-05-13): automatic USB recovery between reconnect
    # attempts. When the bridge dies (Errno 60, 0-byte read, etc.) and
    # the daemon retries connecting, it first forces a USB-level
    # re-enumeration via IOKit's USBDeviceReEnumerate — equivalent to
    # unplugging and replugging the cable, but driven from software.
    # This eliminates the need to physically power-cycle the camera
    # for the vast majority of wedge cases. Verified on macOS Apple
    # Silicon with Sigma fp L (PID 0xC442); falls back gracefully on
    # any unexpected IOKit error.
    #
    # Turn off if you want the old "manual power cycle" behavior
    # (e.g. for diagnosing why a wedge occurred without auto-clearing
    # the evidence).
    auto_recover_on_wedge: bool = True
    # Max wait for the device to come back on the USB bus after
    # IOKit ReEnumerate. Empirically the fp L re-enumerates in <1 s,
    # so 12 s is very generous.
    usb_recovery_timeout_s: float = 12.0
    # Sleep after device reappears, before libusb tries to claim it.
    # Lets ptpcamerad re-attach (so we can detach it cleanly again)
    # and the kernel driver state machine settle.
    usb_recovery_settle_s: float = 1.5
    # How many consecutive CameraIdleError / Errno 60 hits we tolerate
    # before tearing down the session and triggering USB recovery.
    # Each attempt costs ~5 s of sleep + the USB read timeout (~5 s),
    # so this directly controls the worst-case idle → recovered time:
    #
    #   total_recovery_s ≈ idle_recovery_max * 10  +  2 (reconnect delay)
    #                                              +  2 (reenum + settle)
    #
    # Evolution of this knob:
    #   Phase 3.6 (no recovery):   12  (~125 s) — long wait for the user
    #                                            to wake the camera by
    #                                            pressing Shoot.
    #   Phase 3.7a (initial reenum): 2  (~25 s) — passive waits absorb
    #                                            occasional transient busies.
    #   Phase 3.7b (this default):   0  (~ 5 s) — empirically once we
    #                                            see Errno 60 the camera
    #                                            does NOT come back without
    #                                            a USB-level cycle, so
    #                                            the passive_wait window
    #                                            is pure wasted time.
    #                                            Skip straight to reenum.
    idle_recovery_max: int = 0


class OutputConfig(BaseModel):
    """File naming and conflict resolution."""

    root: Path = Field(default=Path("~/Pictures/Tether"))
    filename_template: str = "{session}_{shot:04d}.{ext}"
    session_template: str = "{date}_{name}"
    default_item: str = "untitled"
    auto_increment_shot: bool = True
    on_conflict: Literal["skip", "overwrite", "rename"] = "rename"

    @field_validator("root", mode="before")
    @classmethod
    def expand_root(cls, v: object) -> Path:
        if isinstance(v, str):
            return Path(os.path.expanduser(v))
        if isinstance(v, Path):
            return Path(os.path.expanduser(str(v)))
        raise TypeError(f"output.root must be str or Path, got {type(v)}")


class LightroomConfig(BaseModel):
    """Lightroom Classic integration."""

    mode: Literal["watch", "session"] = "watch"
    watch_folder: Path = Field(default=Path("~/Pictures/Tether/_watch"))

    @field_validator("watch_folder", mode="before")
    @classmethod
    def expand_watch(cls, v: object) -> Path:
        if isinstance(v, str):
            return Path(os.path.expanduser(v))
        if isinstance(v, Path):
            return Path(os.path.expanduser(str(v)))
        raise TypeError(
            f"lightroom.watch_folder must be str or Path, got {type(v)}"
        )


class UIConfig(BaseModel):
    """Floating panel preferences."""

    panel_position: Literal["top-right", "top-left", "bottom-right", "bottom-left"] = "top-right"
    panel_opacity: float = 0.95
    show_thumbnails: bool = False  # Phase 1.2
    shutter_key: str = "space"
    quit_key: str = "cmd+q"


class LiveViewConfig(BaseModel):
    """Live view streaming preferences.

    ``pause_during_snap``: when True, the daemon explicitly suspends
    the LiveViewStream thread for the duration of each snap+download
    transaction. This eliminates the first-snap busy storm (~5 s of
    PTP_RC_DeviceBusy observed when LV runs through a full capture).

    ``busy_backoff_ms`` / ``max_consecutive_busy``: defense in depth.
    If a busy still slips through, sleep for that many ms; if N busies
    happen in a row, halve the effective target_fps (recovers slowly
    after sustained success).
    """

    enabled: bool = True
    # 10 fps is the sustained ceiling for fp L (rate-tested 2026-05-13).
    # 15 fps reliably triggers PTP_RC_DeviceBusy (0x2019) and stalls
    # the bulk endpoint; 12 fps untested. Stay conservative.
    target_fps: int = 10
    # When True, suspend LV across the full snap → poll → download →
    # clear window. This eliminates the 0x2019 busy storm the camera
    # otherwise produces while committing the capture, at the cost of
    # a ~5 s LV freeze per shot (matches DNG download time at the
    # fp L's ~5 MB/s bulk-out rate). Acceptable for studio tether
    # where the user is focused on Lightroom between shots.
    pause_during_snap: bool = True
    # TODO(Phase 3.x): if a finer-grained trade-off is wanted, add
    # `pause_mode: Literal["full", "snap_only", "off"]` here and wire
    # it through to LiveViewStream + watcher. "full" covers the whole
    # commit+drain (current default; zero busies, 5s LV gap). The
    # plumbing — pause()/resume() on the stream and helper methods
    # on the daemon — is already in place; only the resume-point
    # selection in watcher.py would need to switch on the mode.
    busy_backoff_ms: int = 500
    max_consecutive_busy: int = 5
    # Number of consecutive successful frames before we promote
    # effective_fps by +1 back toward target. Lower = faster recovery
    # from a transient demotion. At 5 fps, 10 frames ≈ 2 s.
    promote_after_consecutive_ok: int = 10
    # When the daemon resumes LV after a snap+download, the camera
    # often returns one or two 0x2019 busies as its commit state
    # settles. These are NOT a rate problem — counting them toward
    # demotion drags effective_fps down inappropriately. Within this
    # window after each resume(), busies are still logged + back-off
    # is applied, but they don't tick max_consecutive_busy.
    post_snap_busy_grace_s: float = 1.5
    # The very first snap of a session frequently triggers a longer
    # busy burst even with pause/resume around the full commit
    # window. Don't let that one-off drag the session-long
    # effective_fps down — within this window from stream start,
    # busies are tolerated without demotion.
    first_storm_grace_s: float = 30.0
    # Note: the old ``resume_delay_after_capture_s`` knob was superseded
    # by the burst-aware quiet window in CameraConfig
    # (commit_window_base_s / commit_window_per_shot_s). LV resume is
    # now centrally lifted by the daemon's main loop once the quiet
    # window expires, so this LiveViewConfig field is no longer needed.


class TelemetryConfig(BaseModel):
    """Logging behavior."""

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    json_log_file: Path | None = None
    text_log_file: Path | None = None

    @field_validator("json_log_file", "text_log_file", mode="before")
    @classmethod
    def expand_log_path(cls, v: object) -> Path | None:
        if v is None or v == "":
            return None
        if isinstance(v, str):
            return Path(os.path.expanduser(v))
        if isinstance(v, Path):
            return Path(os.path.expanduser(str(v)))
        raise TypeError(f"log path must be str/Path/None, got {type(v)}")


# ---------------------------------------------------------------------------
# Root model
# ---------------------------------------------------------------------------


class AppConfig(BaseModel):
    """Top-level config."""

    camera: CameraConfig = Field(default_factory=CameraConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    lightroom: LightroomConfig = Field(default_factory=LightroomConfig)
    ui: UIConfig = Field(default_factory=UIConfig)
    liveview: LiveViewConfig = Field(default_factory=LiveViewConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


DEFAULT_CONFIG_LOCATIONS = [
    PROJECT_ROOT / "config.toml",
    Path("~/.config/fp-l-tether/config.toml").expanduser(),
]


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load and validate config from TOML, or use defaults if no file exists.

    Search order if ``path`` is None:
      1. ``./config.toml`` in the project root
      2. ``~/.config/fp-l-tether/config.toml``
      3. Built-in defaults
    """
    if path is not None:
        toml_path = Path(os.path.expanduser(str(path)))
        if not toml_path.exists():
            raise FileNotFoundError(f"config file not found: {toml_path}")
    else:
        toml_path = next(
            (p for p in DEFAULT_CONFIG_LOCATIONS if p.exists()),
            None,
        )

    if toml_path is None:
        return AppConfig()  # all defaults

    with toml_path.open("rb") as f:
        raw = tomllib.load(f)
    return AppConfig(**raw)


def print_config(cfg: AppConfig) -> None:
    """Pretty-print the resolved config (useful for debugging)."""
    import json
    print(json.dumps(cfg.model_dump(mode="json"), indent=2, ensure_ascii=False))


if __name__ == "__main__":  # pragma: no cover
    cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else None)
    print_config(cfg)
