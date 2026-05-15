"""User-tunable application preferences (Phase 3.14).

These values live in ``~/.fp-l-tether/user_settings.json`` under the
``app_prefs`` key. They override ``config.toml`` at load time. The
Preferences window edits them; no manual JSON / TOML editing required.

Camera-side settings (keepalive strategy, doze handling, USB recovery
knobs) deliberately stay TOML-only — they are research-grade and a
mis-set value can wedge the camera. The Preferences window only
exposes user-operational concerns: where photos land, what they're
named, which Lightroom hand-off mode is in use.

Every field is ``Optional`` and defaults to ``None``. ``None`` means
"defer to ``config.toml`` (or the built-in default if TOML doesn't
set it)". Only fields the user has explicitly set are persisted.
This keeps the cache file small and the override semantics easy to
reason about: a missing key = no override.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal


@dataclass
class AppPrefs:
    """User-tunable runtime preferences.

    All fields are Optional. ``None`` = "no override; use config.toml
    or built-in default". Only non-None fields are serialised to disk.
    """

    lightroom_mode: Literal["watch", "session"] | None = None
    watch_folder: str | None = None         # may contain leading ``~``
    session_root: str | None = None         # may contain leading ``~``
    default_subject: str | None = None
    filename_template: str | None = None
    on_conflict: Literal["rename", "overwrite", "skip"] | None = None
    log_dir: str | None = None              # restart required to apply
    json_log_enabled: bool | None = None    # restart required to apply

    def is_empty(self) -> bool:
        """True if every field is ``None`` (no overrides set)."""
        return all(v is None for v in asdict(self).values())

    def to_dict(self) -> dict[str, Any]:
        """Serialise non-None fields only.

        Empty / unset fields are omitted so the JSON file stays small
        and a missing key remains the canonical "no override" signal.
        """
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppPrefs":
        """Reconstruct from a previously-serialised dict.

        Unknown keys are silently dropped — keeps the loader resilient
        against future field renames or hand-edited JSON.
        """
        if not isinstance(data, dict):
            return cls()
        valid = cls.__dataclass_fields__
        return cls(**{k: v for k, v in data.items() if k in valid})

    def to_config_overrides(self) -> dict[tuple[str, str], Any]:
        """Return a flat ``(section, key) → value`` dict suitable for
        overlaying onto an :class:`AppConfig` instance.

        Path values are pre-expanded so callers don't repeat the
        ``Path.expanduser()`` boilerplate. Section names mirror the
        nested attribute names on ``AppConfig`` (``lightroom``,
        ``output``, ``telemetry``).

        Log fields (``log_dir``, ``json_log_enabled``) map to a
        ``logging`` section that the current AppConfig does not own —
        the merge step in ``config.load_config`` is defensive and
        silently skips unknown sections, so these stay in the
        on-disk cache for a future telemetry refactor without
        crashing today.
        """
        out: dict[tuple[str, str], Any] = {}
        if self.lightroom_mode is not None:
            out[("lightroom", "mode")] = self.lightroom_mode
        if self.watch_folder is not None:
            out[("lightroom", "watch_folder")] = (
                Path(self.watch_folder).expanduser()
            )
        if self.session_root is not None:
            out[("output", "root")] = Path(self.session_root).expanduser()
        if self.default_subject is not None:
            out[("output", "default_item")] = self.default_subject
        if self.filename_template is not None:
            out[("output", "filename_template")] = self.filename_template
        if self.on_conflict is not None:
            out[("output", "on_conflict")] = self.on_conflict
        if self.log_dir is not None:
            out[("logging", "log_dir")] = Path(self.log_dir).expanduser()
        if self.json_log_enabled is not None:
            out[("logging", "json_log_enabled")] = bool(self.json_log_enabled)
        return out
