"""What the assistant is allowed to do.

The list is short on purpose. Everything here either reads state the dashboard
already shows, or proposes a config change that a human has to press Apply on.
There is no claim tool, no portal tool, and no way to reach the keychain — so
the worst outcome of a prompt injection hidden in a shift title is a wrong
answer, not a shift picked up in the wrong city.

Read tools delegate to `dashboard/data.py` rather than issuing their own SQL, so
the chat and the cards on screen can never disagree about what happened.
"""

from __future__ import annotations

import difflib
import io
import logging
import secrets as _stdlib_secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from ..config import UserConfig
from ..dashboard.data import build_settings, build_shifts, build_status
from ..store import Store

log = logging.getLogger(__name__)

# A proposal is only good for the page that requested it. Restarting the server
# drops every pending change, which is the behaviour we want: a diff nobody
# accepted while the app was running should not be waiting when it comes back.
_MAX_PENDING = 8


@dataclass(frozen=True)
class Proposal:
    change_id: str
    diff: str
    summary: str
    config_path: Path
    new_yaml: str


class ToolError(RuntimeError):
    """A tool failure worth showing the model so it can try something else."""


SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "get_status",
        "description": (
            "Current agent status: whether it is paused and why, dry-run and claim mode, "
            "and what the last poll cycle saw. Use this first when asked how things are going."
        ),
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "list_shifts",
        "description": (
            "Shifts the agent has seen, newest first. Filter by verdict to answer questions "
            "like which ones were skipped for rest, or which were actually picked up. "
            "Verdicts include: match, outside_availability, excluded_date, conflicts_assigned, "
            "insufficient_rest, exceeds_weekly_cap, too_soon, grade_notify_only, "
            "max_attempts_reached, wrong_base, not_premium."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "verdict": {"type": "string", "description": "Optional verdict to filter by."},
                "claimed_only": {"type": "boolean", "description": "Only shifts that were claimed."},
                "limit": {"type": "integer", "description": "Maximum to return (default 20)."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "explain_shift",
        "description": (
            "Everything known about one shift by its id, including the exact verdict, the "
            "detail text explaining it, and how many claim attempts have failed."
        ),
        "parameters": {
            "type": "object",
            "properties": {"shift_id": {"type": "string"}},
            "required": ["shift_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_config",
        "description": (
            "The rules currently in force: availability windows, rest minimums, home base, "
            "grades pursued, poll interval, quiet hours. Contains no credentials."
        ),
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "propose_config_change",
        "description": (
            "Propose an edit to the configuration file. This does NOT apply anything: it "
            "validates the change and returns a diff for the user to approve. Pass only the "
            "sections you want changed, as a nested object mirroring the config file. "
            "Always tell the user what you proposed and that they must press Apply."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "patch": {
                    "type": "object",
                    "description": "Nested partial config, e.g. {\"rules\": {\"min_rest_hours\": 12}}",
                },
                "summary": {
                    "type": "string",
                    "description": "One plain sentence describing the change.",
                },
            },
            "required": ["patch", "summary"],
            "additionalProperties": False,
        },
    },
]


class ToolBox:
    """Binds the tool schemas to one user's store, config and config file."""

    def __init__(self, store: Store, config: UserConfig, config_path: Path | None = None) -> None:
        self.store = store
        self.config = config
        self.config_path = Path(config_path) if config_path else None
        self.pending: dict[str, Proposal] = {}

    # -- dispatch ---------------------------------------------------------

    def run(self, name: str, arguments: dict[str, Any]) -> Any:
        handler = {
            "get_status": self._get_status,
            "list_shifts": self._list_shifts,
            "explain_shift": self._explain_shift,
            "get_config": self._get_config,
            "propose_config_change": self._propose,
        }.get(name)
        if handler is None:
            raise ToolError(f"unknown tool {name!r}")
        return handler(**arguments)

    # -- reads ------------------------------------------------------------

    def _get_status(self) -> dict[str, Any]:
        return build_status(self.store, self.config)

    def _list_shifts(
        self, verdict: str | None = None, claimed_only: bool = False, limit: int = 20
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit or 20), self.config.llm.max_shifts_in_context))
        shifts = build_shifts(self.store, self.config, limit=200)
        if verdict:
            wanted = verdict.strip().lower()
            shifts = [s for s in shifts if s["verdict"] == wanted]
        if claimed_only:
            shifts = [s for s in shifts if s["claimed"]]
        return [_trim(s) for s in shifts[:limit]]

    def _explain_shift(self, shift_id: str) -> dict[str, Any]:
        wanted = (shift_id or "").strip().lower()
        for shift in build_shifts(self.store, self.config, limit=200):
            if str(shift["id"]).lower() == wanted:
                out = _trim(shift)
                out["failed_attempts"] = self.store.failed_attempts(self.config.name, shift["id"])
                out["max_claim_attempts"] = self.config.rules.max_claim_attempts
                return out
        raise ToolError(
            f"No shift with id {shift_id!r} has been seen. Use list_shifts to see the ids."
        )

    def _get_config(self) -> dict[str, Any]:
        return build_settings(self.store, self.config)

    # -- the one write, in two halves -------------------------------------

    def _propose(self, patch: dict[str, Any], summary: str) -> dict[str, Any]:
        if self.config_path is None:
            raise ToolError(
                "This dashboard was not started with a config file, so settings cannot be "
                "changed from here. Edit the YAML directly."
            )
        if not isinstance(patch, dict) or not patch:
            raise ToolError("patch must be a non-empty object mirroring the config file")

        current_text = self.config_path.read_text(encoding="utf-8")
        current = yaml.safe_load(current_text) or {}

        # Round-trip through ruamel rather than safe_dump, which would rewrite the
        # file from the parsed data and throw away every comment in it. Those
        # comments are the only documentation of what the settings mean; losing
        # them to a one-line rest change would be a bad trade.
        rt = _round_tripper()

        # Before touching anything, check we can reproduce the file byte for byte.
        # If we cannot, editing it would silently reformat regions the user never
        # asked about, and the diff they are meant to approve would be mostly
        # noise. Refusing is better than quietly rewriting someone's config.
        if _reserialise(rt, current_text) != current_text:
            raise ToolError(
                "this config file uses formatting I cannot reproduce exactly, so editing "
                "it here would reformat parts you did not ask to change. Tell the user to "
                "edit the file by hand for this one."
            )

        document = rt.load(current_text) or {}
        _deep_merge(document, _quote_strings(patch))

        buffer = io.StringIO()
        rt.dump(document, buffer)
        new_text = buffer.getvalue()

        # Validate by re-reading the text we are actually going to write, not a
        # parallel merge of the same patch into a dict. Those two can disagree —
        # a quoting difference alone is enough — and when they do, the check
        # passes on one value while the file receives another.
        try:
            proposed = yaml.safe_load(new_text) or {}
            UserConfig.model_validate(proposed)
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(f"that change would make the config invalid: {exc}") from exc

        # Compare parsed structures, not text: a formatting-only difference is
        # not a change worth asking someone to approve.
        if proposed == current:
            return {"change_id": None, "diff": "", "note": "that is already the current setting"}

        diff = "".join(
            difflib.unified_diff(
                current_text.splitlines(keepends=True),
                new_text.splitlines(keepends=True),
                fromfile="config (current)",
                tofile="config (proposed)",
                n=2,
            )
        )
        if not diff.strip():
            return {"change_id": None, "diff": "", "note": "that is already the current setting"}

        change_id = _stdlib_secrets.token_urlsafe(9)
        if len(self.pending) >= _MAX_PENDING:
            self.pending.pop(next(iter(self.pending)))
        self.pending[change_id] = Proposal(
            change_id=change_id,
            diff=diff,
            summary=summary,
            config_path=self.config_path,
            new_yaml=new_text,
        )
        return {
            "change_id": change_id,
            "summary": summary,
            "diff": diff,
            "note": "Not applied. The user must press Apply on the diff shown in the panel.",
        }

    def apply(self, change_id: str) -> Proposal:
        """Write an approved proposal.

        Called by the server when the user presses Apply — never by the model.
        That split is the whole safety story for this tool: text arriving from
        the portal can reach `_propose`, but only a click can reach here.
        """
        proposal = self.pending.pop(change_id, None)
        if proposal is None:
            raise ToolError("that change has expired; ask again and re-approve the new diff")

        backup = proposal.config_path.with_name(proposal.config_path.name + ".bak")
        try:
            backup.write_text(
                proposal.config_path.read_text(encoding="utf-8"), encoding="utf-8"
            )
            proposal.config_path.write_text(proposal.new_yaml, encoding="utf-8")
        except OSError as exc:
            raise ToolError(f"could not write the config file: {exc}") from exc

        log.info("config changed via chat: %s", proposal.summary)
        return proposal


def _quote_strings(value: Any) -> Any:
    """Force every string in a patch to be written quoted.

    YAML 1.1 reads an unquoted `20:00` as base-60 — 1200 — which pydantic then
    coerces to 00:20. Asking to move a Friday window to 8pm would silently set
    it to twenty past midnight, and the validation step would not catch it
    because it sees the Python string while the file gets the integer. The same
    trap trips `no` and `off`, which become False.
    """
    if isinstance(value, str):
        return DoubleQuotedScalarString(value)
    if isinstance(value, dict):
        return {k: _quote_strings(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_quote_strings(v) for v in value]
    return value


def _reserialise(rt: YAML, text: str) -> str:
    """Load and dump unchanged — the fidelity probe for the guard above."""
    buffer = io.StringIO()
    rt.dump(rt.load(text) or {}, buffer)
    return buffer.getvalue()


def _round_tripper() -> YAML:
    """A ruamel loader tuned to leave this project's config file alone.

    Defaults would reindent every list and rewrite `null` as an empty value,
    turning a one-line rest change into six unrelated hunks. Nobody reads a diff
    like that carefully, and a diff nobody reads carefully is not a safety
    control — so the dumper is matched to the file's existing style instead.
    """
    rt = YAML()
    rt.preserve_quotes = True
    rt.width = 4096  # never re-wrap a long comment line
    # offset must stay below sequence, or ruamel emits list items it cannot
    # itself re-read.
    rt.indent(mapping=2, sequence=4, offset=2)
    rt.representer.add_representer(
        type(None), lambda dumper, _: dumper.represent_scalar("tag:yaml.org,2002:null", "null")
    )
    return rt


def _trim(shift: dict[str, Any]) -> dict[str, Any]:
    """Only the fields worth spending context on."""
    return {
        key: shift.get(key)
        for key in (
            "id",
            "title",
            "start",
            "end",
            "verdict",
            "verdict_label",
            "detail",
            "grade",
            "claimed",
            "claim_outcome",
            "dry_run",
        )
        if shift.get(key) not in (None, "")
    }


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Merge nested dicts; lists and scalars are replaced wholesale.

    Replacing rather than extending lists matters for availability slots — a
    user asking to "change Friday" means the new list, not the old one plus a
    second Friday.
    """
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = _deep_merge(base[key], value)
        else:
            base[key] = value
    return base
