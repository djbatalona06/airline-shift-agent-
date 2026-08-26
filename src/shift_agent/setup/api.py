"""First-run profile setup/picker: validates and writes a `UserConfig` profile.

No import of `webview` anywhere in this file - every method is plain Python
working against `paths`/`config`, so it is directly unit-testable with no
display, no pywebview, and no HTTP server involved. `setup/__init__.py` is a
thin shell around this that serves `index.html` and wires these methods up to
the two `api/setup/*` routes in `dashboard/server.py`.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .. import paths
from ..config import UserConfig
from ..logging_safe import scrub

log = logging.getLogger(__name__)

# Every day, 06:00-22:00. Good enough to get a first-time user through setup
# without hand-building a slots table; refining it afterwards means editing
# the YAML directly, same as any other config change today (the dashboard's
# Settings tab is read-only).
_DEFAULT_SLOTS: tuple[dict[str, str], ...] = tuple(
    {"day": day, "start": "06:00", "end": "22:00"}
    for day in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
)


def _write_atomic(path: Path, content: str) -> None:
    """Write via a temp file in the same directory, then replace.

    Mirrors `dashboard/__init__.py`'s helper of the same name. Kept local
    rather than shared - that one is private to its module too, and this is
    its only other caller.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
        os.replace(tmp, path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def _split_grades(raw: Any) -> list[str]:
    if not raw:
        return []
    return [g.strip().upper() for g in str(raw).split(",") if g.strip()]


def _build_config_dict(payload: dict[str, Any]) -> dict[str, Any]:
    """Shape a form payload into something `UserConfig.model_validate` accepts.

    `UserConfig`'s models all set `extra="forbid"`, so only known keys go in -
    an optional field is included only when the user actually gave it a value.
    """
    data: dict[str, Any] = {
        "name": str(payload.get("name", "")).strip(),
        "portal": {"adapter": str(payload.get("adapter") or "mock").strip()},
        "availability": {
            "timezone": str(payload.get("timezone", "")).strip(),
            "slots": [dict(slot) for slot in _DEFAULT_SLOTS],
        },
        "rules": {"min_rest_hours": float(payload.get("min_rest_hours") or 10)},
        "claim_mode": str(payload.get("claim_mode") or "confirm").strip(),
        "dry_run": bool(payload.get("dry_run", True)),
    }

    home_base = str(payload.get("home_base", "")).strip()
    if home_base:
        data["home_base"] = {"code": home_base}

    pursue = _split_grades(payload.get("grades_pursue"))
    notify_only = _split_grades(payload.get("grades_notify_only"))
    if pursue or notify_only:
        data["grades"] = {"pursue": pursue, "notify_only": notify_only}

    return data


class SetupAPI:
    """Backs the setup/picker page. One instance per `open_setup_window()` call."""

    def __init__(self) -> None:
        self.chosen: str | None = None
        # Set once a profile has been picked or saved, so open_setup_window()
        # knows to stop waiting even when the window itself (Edge/Chrome/
        # webbrowser fallback) gives Python no other signal that it's done.
        self.done_event = threading.Event()

    def list_profiles(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for slug in paths.list_profiles():
            path = paths.config_path(slug, create=False)
            if not path.is_file():
                continue
            try:
                config = UserConfig.load(path)
            except Exception as exc:
                # A broken profile should be visible in the picker, not
                # silently dropped from the list.
                out.append({"id": slug, "name": slug, "error": scrub(exc)})
                continue
            out.append(
                {
                    "id": slug,
                    "name": config.name,
                    "adapter": config.portal.adapter,
                    "timezone": config.availability.timezone,
                }
            )
        return out

    def save_profile(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            config = UserConfig.model_validate(_build_config_dict(payload))
            slug = paths.slugify(config.name)
            _write_atomic(
                paths.config_path(slug),
                yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False),
            )
        except ValidationError as exc:
            # Never pass exc.errors() through as-is - it can carry non-JSON-
            # safe `ctx`/`input`/`url` keys depending on the failing validator.
            return {
                "ok": False,
                "errors": [
                    {"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]}
                    for e in exc.errors()
                ],
            }
        except Exception as exc:
            # Covers, among others, an empty/unusable name: `UserConfig.name`
            # has no non-empty constraint, so that only fails inside
            # paths.slugify() - after model validation already passed.
            log.warning("setup: could not save profile: %s", exc)
            return {"ok": False, "errors": [{"field": "", "message": scrub(exc)}]}

        self.chosen = slug
        self.done_event.set()
        return {"ok": True, "profile": slug}

    def choose(self, profile_id: str) -> dict[str, Any]:
        if profile_id not in paths.list_profiles():
            return {"ok": False, "error": "unknown profile"}
        self.chosen = profile_id
        self.done_event.set()
        return {"ok": True, "profile": profile_id}
