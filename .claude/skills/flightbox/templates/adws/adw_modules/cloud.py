"""Push local traces to Flightbox Cloud.

Three properties this module exists to guarantee:

1. **It never blocks a run.** Sync is out-of-band — invoked after a run, or by hand. A
   Cloud outage, an expired token, or no network at all must never delay, fail, or alter
   an agent run. Nothing here is called from the agent loop.

2. **It never sends more than you asked for.** `metadata` (the default) ships row shapes,
   token counts and costs. `payload_json` — which holds your prompts and your source —
   stays on your disk unless you explicitly choose `full`.

3. **It survives schema drift.** Columns are discovered with PRAGMA table_info at
   runtime rather than hardcoded, so a core version that adds or renames a column does
   not break sync; it just sends what is actually there.

Config, in `flightbox.config.yaml`:

    cloud:
      sync: metadata           # off | metadata | full   (default: off)
      url: https://api.flightbox.dev
      workspace: my-workspace  # informational; the token determines the workspace

Token, in order of precedence:
    FLIGHTBOX_CLOUD_TOKEN env var
    ~/.config/flightbox/credentials   (a single line, or KEY=VALUE)
"""

from __future__ import annotations

import json
import os
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

DEFAULT_URL = "https://api.flightbox.dev"
CRED_PATH = Path.home() / ".config" / "flightbox" / "credentials"

# urllib's default User-Agent trips Cloudflare's browser-integrity check, which answers
# 403 "error code: 1010" — indistinguishable from an auth failure if you don't know to
# look for it. Identify the client honestly instead.
USER_AGENT = "flightbox-sync/1.0 (+https://flightbox.dev)"

# Columns Cloud understands, per table. Anything else in the local db is ignored rather
# than rejected, so the two schemas can drift independently.
WANTED: dict[str, set[str]] = {
    "sessions": {"adw_id", "adw_name", "request", "status", "started_at", "ended_at",
                 "total_tokens", "total_cost", "repo", "branch"},
    "phases": {"phase_id", "adw_id", "name", "kind", "status", "seq", "attempt",
               "tokens", "cost", "started_at", "ended_at"},
    "events": {"event_id", "adw_id", "phase_id", "parent_id", "type", "name",
               "payload_json", "tokens", "cost", "agent", "started_at", "ended_at"},
    "gate_results": {"id", "gate_id", "adw_id", "phase_id", "gate", "name", "passed",
                     "created_at"},
    "approvals": {"approval_id", "adw_id", "phase_id", "name", "status", "decided_by",
                  "requested_at", "decided_at"},
    "agent_sessions": {"adw_id", "agent", "coding_agent", "session_id", "tokens", "cost"},
}


class CloudError(RuntimeError):
    """Sync failed. Never raised into an agent run — only to the sync CLI."""


@dataclass
class CloudConfig:
    mode: str = "off"          # off | metadata | full
    url: str = DEFAULT_URL
    workspace: str | None = None

    @property
    def enabled(self) -> bool:
        return self.mode in ("metadata", "full")


SYNC_MODES = ("off", "metadata", "full")


def normalize_sync_mode(value: Any) -> str:
    """The one parser for `cloud.sync`. Imported by data_types so the config a RUN
    sees and the config the SYNC CLI sees can never drift apart — they disagreed
    once already, and the result was a customer who believed they were syncing
    while nothing ever left the machine.

    yaml 1.1 resolves bare `off`/`no` to False and `on`/`yes`/`true` to True, so
    the boolean spellings arrive here having lost which word was written. They are
    deliberately NOT symmetric:

      False -> "off"   `off` asks for the existing default. Reading it as such
                       costs nothing and is what the config comments document.
      True  -> error   `on` asks to start transmitting to a remote server without
                       saying WHAT to transmit. metadata and full differ in whether
                       prompts and source leave the machine; that is not a guess
                       anyone should make on the user's behalf. Fail loudly and
                       make them name the mode.
    """
    if value is False:
        return "off"
    if value is True:
        raise ValueError(
            "cloud.sync: `on`/`yes`/`true` is ambiguous - write `metadata` (costs and "
            "timings only) or `full` (also sends prompts and source) explicitly")
    mode = str(value if value is not None else "off").strip().lower() or "off"
    if mode not in SYNC_MODES:
        raise ValueError(f"cloud.sync must be off|metadata|full, got {mode!r}")
    return mode


def load_config(cfg: Any) -> CloudConfig:
    """Read the `cloud:` block off an already-parsed config object or dict."""
    block: Any = None
    if isinstance(cfg, dict):
        block = cfg.get("cloud")
    else:
        block = getattr(cfg, "cloud", None)
    if not block:
        return CloudConfig()
    get = block.get if isinstance(block, dict) else lambda k, d=None: getattr(block, k, d)
    try:
        mode = normalize_sync_mode(get("sync", "off"))
    except ValueError as err:
        raise CloudError(str(err)) from err
    return CloudConfig(mode=mode, url=str(get("url", DEFAULT_URL) or DEFAULT_URL).rstrip("/"),
                       workspace=get("workspace", None))


def load_token() -> str | None:
    tok = os.environ.get("FLIGHTBOX_CLOUD_TOKEN", "").strip()
    if tok:
        return tok
    try:
        for line in CRED_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, _, v = line.partition("=")
                if k.strip().upper() in ("FLIGHTBOX_CLOUD_TOKEN", "TOKEN"):
                    return v.strip().strip("'\"")
            else:
                return line
    except OSError:
        pass
    return None


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """Actual columns, discovered at runtime — the reason schema drift is survivable."""
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return []
    return [r[1] for r in rows]


def _select(conn: sqlite3.Connection, table: str, adw_id: str) -> list[dict]:
    cols = [c for c in _columns(conn, table) if c in WANTED.get(table, set())]
    if not cols or "adw_id" not in _columns(conn, table):
        return []
    sql = f"SELECT {', '.join(cols)} FROM {table} WHERE adw_id = ?"
    return [dict(zip(cols, row)) for row in conn.execute(sql, (adw_id,)).fetchall()]


# Governance events carry numbers, not content: what the cap was, what was spent,
# which scope crossed it. Under `metadata` those numbers are the whole point of
# syncing — a halt with its figures stripped can report THAT a run stopped but not
# that the limit you set was the thing that stopped it, which is the one claim the
# record exists to support.
#
# So these events keep a payload, rebuilt from an allowlist rather than filtered.
# An allowlist cannot leak a key added upstream later; a blocklist silently would.
GOVERNANCE_EVENTS = {
    "budget_exceeded", "budget_warning", "month_budget", "cost_estimate", "not_accepted",
    "policy_applied",
}
#
# These are the keys the producers ACTUALLY write, checked against
# runner.py (_enforce_budget / _warn_if_approaching), estimates.py
# (CostEstimate, MonthStatus) and Run.finish. Guessing at names here is worse
# than useless: an allowlisted key that is never emitted is dead, and a real key
# left out silently drops a number the dashboard then renders as $0.00.
GOVERNANCE_KEYS = {
    # budget_exceeded
    "breaches", "agent", "run_cost", "agent_cost", "max_run_cost",
    # budget_warning
    "scope", "threshold", "spent", "cap", "fraction",
    # cost_estimate  (CostEstimate + two extras)
    "adw_name", "runs", "median_cost", "p90_cost", "worst_cost", "likely_halt",
    # month_budget  (MonthStatus)
    "month", "budget", "daily_pace", "days_remaining", "projected", "likely_over",
    # not_accepted
    "reason",
    # policy_applied  (adw_modules/policy.py) — the cap that was actually in
    # force and which side it came from. Without these an export can say a run
    # halted but not that the org's own cap is what stopped it.
    "month_budget", "require_approval_phases", "source",
}


def _governance_payload(event: dict) -> str | None:
    """Return a reduced payload for a governance event, or None to strip it.

    `reason` (from not_accepted) is the one free-text field allowed through. It is
    written by the ADW itself, not by a model and not from a file, so it cannot
    carry source; it is truncated anyway, because "cannot" is a claim worth not
    relying on.
    """
    if event.get("name") not in GOVERNANCE_EVENTS:
        return None
    try:
        data = json.loads(event.get("payload_json") or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    kept = {k: v for k, v in data.items() if k in GOVERNANCE_KEYS}
    if isinstance(kept.get("reason"), str):
        kept["reason"] = kept["reason"][:300]
    return json.dumps(kept) if kept else None


def build_payload(db_path: Path, adw_id: str, mode: str) -> dict:
    """Assemble one run's payload straight out of the local trace."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        sess = _select(conn, "sessions", adw_id)
        if not sess:
            raise CloudError(f"run {adw_id!r} not found in {db_path}")
        events = _select(conn, "events", adw_id)
        if mode != "full":
            # The line that keeps prompts and source on this machine.
            for e in events:
                keep = _governance_payload(e)
                if keep is None:
                    e.pop("payload_json", None)
                else:
                    e["payload_json"] = keep
        gates = _select(conn, "gate_results", adw_id)
        for g in gates:                       # local calls it `id`/`gate`; Cloud wants gate_id/name
            if "gate_id" not in g and "id" in g:
                g["gate_id"] = str(g.pop("id"))
            if "name" not in g and "gate" in g:
                g["name"] = g.get("gate")
        agents = _select(conn, "agent_sessions", adw_id)
        for a in agents:
            a.setdefault("backend", a.get("coding_agent"))
        return {
            "mode": mode,
            "session": sess[0],
            "phases": _select(conn, "phases", adw_id),
            "events": events,
            "gate_results": gates,
            "approvals": _select(conn, "approvals", adw_id),
            "agent_sessions": agents,
        }
    finally:
        conn.close()


def list_runs(db_path: Path, limit: int = 50) -> list[tuple[str, str | None]]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT adw_id, started_at FROM sessions ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [(r[0], r[1]) for r in rows]
    finally:
        conn.close()


def push(cfg: CloudConfig, token: str, payload: dict, timeout: int = 30) -> dict:
    req = urllib.request.Request(
        f"{cfg.url}/v1/ingest",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        if e.code == 401:
            raise CloudError("token rejected — check FLIGHTBOX_CLOUD_TOKEN") from None
        if e.code == 402:
            raise CloudError("no active subscription for this workspace. Your local "
                             "budget caps are unaffected and keep running.") from None
        raise CloudError(f"ingest failed ({e.code}): {body}") from None
    except urllib.error.URLError as e:
        raise CloudError(f"cannot reach {cfg.url}: {e.reason}") from None


def sync(db_path: Path, cfg: CloudConfig, token: str, adw_ids: Iterable[str]) -> list[dict]:
    """Push the given runs. Idempotent — re-syncing overwrites rather than duplicating."""
    out = []
    for adw_id in adw_ids:
        payload = build_payload(db_path, adw_id, cfg.mode)
        out.append(push(cfg, token, payload))
    return out
