"""Claude Code coding agent interface — headless `claude -p` on your subscription.

Runs `claude -p --output-format stream-json --verbose` and tails its JSONL
stdout line by line, forwarding each event to a callback WHILE the agent works —
the same streaming-by-construction contract agent_pi.py fulfills, behind the
same `run(PiRequest, ...) -> PiResult` signature, so callers need zero changes.

Session semantics differ from pi and are absorbed here. Pi's `--session-id` is
create-or-continue; Claude Code splits them: `--session-id <uuid>` CREATES
(and errors "already in use" on a second call), `--resume <uuid>` CONTINUES the
same id. SSSF's session ids (`sssf-<adw_id>-<agent>-<hex>`) are not UUIDs, so
each one is mapped to a deterministic UUIDv5 and a marker file in
`request.session_dir` records that the Claude session exists — first call
creates, every later call resumes, and the marker is what tells them apart.

Model naming convention: sssf configs write `provider/model-id` and validate()
enforces that shape. For this backend the provider is `anthropic` and the id is
whatever `claude --model` accepts — an alias (`sonnet`, `opus`, `haiku`) or a
full model name (`claude-sonnet-4-5`). `anthropic/sonnet` -> `--model sonnet`.
A bare pattern with no slash is treated as anthropic. Any other provider is a
config error: this backend runs on the Claude subscription, nothing else.

Deltas from pi, on purpose:
  * `thinking:` is accepted but not forwarded — Claude Code manages its own
    extended thinking; there is no CLI knob equivalent to pi's `--thinking`.
  * `harness_engineering:` (pi `-e` extensions) is ignored — Claude Code loads
    its own skills/plugins from the user's installation.
  * `tools:` allowlists are translated (read->Read, bash->Bash, find->Glob, …)
    and passed as `--allowedTools`, which auto-approves them; unmapped names
    (pi extension tools) are dropped. Runs use `--permission-mode acceptEdits`;
    SSSF's own permissions.py (diff + rollback) stays the enforcement boundary.
  * Cost breakdown: Claude reports one `total_cost_usd`, not per-component
    dollars, so only `total_cost` is populated in the UsageBreakdown.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

from .data_types import PiRequest, PiResult
from .utils import now_iso, operator_env

CLAUDE_PATH = os.environ.get("CLAUDE_PATH", "")

RESULT_SNIPPET_CHARS = 20_000   # tool output rides along whole; clip only guards pathological cases
ARG_VALUE_CHARS = 20_000        # args too — the UI scrolls, it must not be handed cut-off data
LABEL_CHARS = 80                # "bash: <command>" shown as the event name

# The arg that identifies a call at a glance, in the order Claude's tools use.
PRIMARY_ARGS = ("command", "file_path", "path", "pattern", "query", "url", "prompt")

# pi tool name -> Claude Code tool name. `ls` has no Claude equivalent; Bash
# covers it. Unmapped names (pi extension tools like subagent_create) are
# dropped rather than passed through — an unknown --allowedTools entry would
# approve nothing and only clutter the invocation.
TOOL_MAP = {
    "read": "Read", "bash": "Bash", "edit": "Edit", "write": "Write",
    "grep": "Grep", "find": "Glob", "ls": "Bash", "glob": "Glob",
    "webfetch": "WebFetch", "websearch": "WebSearch", "task": "Task",
}
# tools: None in the config means "all tools usable" — for Claude that still
# needs an explicit auto-approve list or every call dies waiting for a prompt
# that headless mode cannot show.
DEFAULT_ALLOWED = ["Bash", "Read", "Edit", "Write", "Grep", "Glob", "WebFetch"]


def _claude_path() -> str:
    """Resolve the claude executable — on Windows it is claude.cmd/.exe, which
    a bare "claude" argv[0] does not always find without the shell."""
    if CLAUDE_PATH:
        return CLAUDE_PATH
    return shutil.which("claude") or "claude"


def resolve_model(pattern: str) -> tuple[str, str]:
    """Resolve `anthropic/<model>` to an explicit ``(provider, model_id)`` pair.

    Mirrors agent_pi.resolve_model's contract so validate() can dispatch to
    either backend. The id is handed to `claude --model` verbatim.
    """
    if "/" in pattern:
        provider, model_id = pattern.split("/", 1)
    else:
        provider, model_id = "anthropic", pattern
    if provider != "anthropic":
        raise ValueError(
            f"model pattern {pattern!r}: coding_agent 'claude_code' runs Anthropic "
            f"models only — write it as anthropic/<alias-or-model-id>, "
            f"e.g. anthropic/sonnet")
    if not model_id.strip():
        raise ValueError(f"model pattern {pattern!r} has no model id after the provider")
    return provider, model_id


def _allowed_tools(tools: Optional[list[str]]) -> list[str]:
    if tools is None:
        return list(DEFAULT_ALLOWED)
    mapped = [TOOL_MAP[t] for t in tools if t in TOOL_MAP]
    seen: list[str] = []
    for tool in mapped:
        if tool not in seen:
            seen.append(tool)
    return seen or list(DEFAULT_ALLOWED)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _label(tool: str, args: dict) -> str:
    """One-line human name for a tool call: `bash: ls -la src`."""
    value = next((args[key] for key in PRIMARY_ARGS
                  if isinstance(args.get(key), str) and args[key].strip()), "")
    if not value:
        value = next((v for v in args.values() if isinstance(v, str) and v.strip()), "")
    value = " ".join(str(value).split())
    name = tool.lower()
    return f"{name}: {_clip(value, LABEL_CHARS)}" if value else name


def _result_text(content) -> str:
    """A tool_result's content is a plain string or a list of typed blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content
                       if isinstance(part, dict) and part.get("type") == "text")
    return ""


class ToolCallTracker:
    """Folds Claude's stream into ONE normalized record per completed call.

    Claude announces a call as a `tool_use` block on an assistant event and
    delivers its outcome as a `tool_result` block on a later user event. Only
    the result carries the verdict, so that is where a record is emitted — the
    same fold agent_pi.ToolCallTracker performs on pi's three-event shape.

    `observe(event)` returns a LIST of finished-call records (a user message
    can settle several parallel tool calls at once); agents._event_forwarder
    normalizes list-or-dict-or-None across both backends.
    """

    def __init__(self) -> None:
        self._open: dict[str, dict] = {}

    def observe(self, event: dict) -> list[dict]:
        etype = event.get("type", "")
        message = event.get("message") or {}
        content = message.get("content")
        if not isinstance(content, list):
            return []
        if etype == "assistant":
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    self._announce(block.get("id"), block.get("name"),
                                   block.get("input"))
            return []
        if etype != "user":
            return []
        records = []
        for block in content:
            if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                continue
            call_id = str(block.get("tool_use_id") or "")
            opened = self._open.pop(call_id, {})
            tool = str(opened.get("tool") or "tool")
            args = opened.get("args") or {}
            record = {
                "tool": tool,
                "tool_call_id": call_id,
                "args": {key: _clip(value, ARG_VALUE_CHARS) if isinstance(value, str) else value
                         for key, value in args.items()},
                "ok": not block.get("is_error", False),
                "label": _label(tool, args),
            }
            result_text = _result_text(block.get("content"))
            if result_text:
                record["result_snippet"] = _clip(result_text, RESULT_SNIPPET_CHARS)
            record["ended_at"] = now_iso()
            if opened.get("clock"):
                record["duration_ms"] = int((time.monotonic() - opened["clock"]) * 1000)
            if opened.get("started_at"):
                record["started_at"] = opened["started_at"]
            records.append(record)
        return records

    def _announce(self, call_id, tool, args) -> None:
        if not call_id:
            return
        known = self._open.get(str(call_id), {})
        self._open[str(call_id)] = {
            "tool": tool or known.get("tool", ""),
            "args": args or known.get("args", {}),
            "started_at": known.get("started_at") or now_iso(),   # wall clock, for the row
            "clock": known.get("clock") or time.monotonic(),      # monotonic, for duration
        }


# ── session mapping (sssf session id -> Claude session UUID) ─────────────────

def _claude_uuid(session_id: str) -> str:
    """Deterministic UUID for an sssf session id — stable across processes, so
    a crashed run resumed by adw_id maps back to the same Claude session."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"sssf-claude-code:{session_id}"))


def _marker_path(request: PiRequest) -> Path:
    return Path(request.session_dir) / f"{request.session_id}.json"


def _session_args(request: PiRequest) -> tuple[list[str], str, bool]:
    """(cli args, claude session uuid, created) for this call."""
    marker = _marker_path(request)
    if marker.exists():
        claude_sid = json.loads(marker.read_text()).get("claude_session_id") \
            or _claude_uuid(request.session_id)
        return ["--resume", claude_sid], claude_sid, False
    claude_sid = _claude_uuid(request.session_id)
    return ["--session-id", claude_sid], claude_sid, True


def _record_session(request: PiRequest, claude_sid: str) -> None:
    marker = _marker_path(request)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"claude_session_id": claude_sid,
                                  "sssf_session_id": request.session_id,
                                  "created_at": now_iso()}, indent=2))


# ── run ──────────────────────────────────────────────────────────────────────

def _usage_as_pi(usage: dict, cost: float) -> tuple[dict, int]:
    """Reshape Claude's usage object into the pi shape UsageBreakdown folds.

    Claude reports one lump `total_cost_usd`, so only cost.total is populated —
    the per-component dollar columns stay zero rather than being guessed.
    """
    parts = {
        "input": usage.get("input_tokens") or 0,
        "output": usage.get("output_tokens") or 0,
        "cacheRead": usage.get("cache_read_input_tokens") or 0,
        "cacheWrite": usage.get("cache_creation_input_tokens") or 0,
    }
    total = sum(parts.values())
    return {**parts, "cost": {"total": cost}}, total


def _context_tokens(usage: dict) -> int:
    """Window occupancy after a turn: everything that was in the prompt plus
    what came out. Cache reads count — cached prompt is still prompt."""
    return int(sum(usage.get(part) or 0
                   for part in ("input_tokens", "cache_read_input_tokens",
                                "cache_creation_input_tokens", "output_tokens")))


def run(request: PiRequest, on_event: Optional[Callable[[dict], None]] = None,
        on_spawn: Optional[Callable[[int], None]] = None,
        on_exit: Optional[Callable[[int], None]] = None) -> PiResult:
    """Run one non-interactive Claude Code turn.

    `on_spawn(pid)` and `on_exit(pid)` bracket the child process so the caller
    can record it as killable, exactly as agent_pi.run does.
    """
    _, model_id = resolve_model(request.model)
    session_args, claude_sid, creating = _session_args(request)
    attempt_args = [session_args]
    if creating:
        # A marker lost to a crash mid-write would make us re-create a session
        # that exists; "already in use" then falls through to a resume.
        attempt_args.append(["--resume", claude_sid])

    raw_path = Path(request.raw_output_path)
    raw_path.parent.mkdir(parents=True, exist_ok=True)

    for sess in attempt_args:
        result, stderr = _spawn(request, model_id, sess, raw_path,
                                on_event, on_spawn, on_exit)
        if result.returncode == 0 or "already in use" not in stderr:
            break

    if result.returncode == 0:
        _record_session(request, claude_sid)
    if result.returncode != 0 and not result.text:
        raise RuntimeError(f"claude exited {result.returncode}: {stderr.strip()[-800:]}")
    return result


def _spawn(request: PiRequest, model_id: str, session_args: list[str],
           raw_path: Path, on_event, on_spawn, on_exit) -> tuple[PiResult, str]:
    # The prompt rides right after -p, BEFORE any flags: --allowedTools is a
    # variadic option and would swallow a trailing positional as another tool
    # ("Input must be provided..." was how that failure presented).
    cmd = [
        _claude_path(), "-p", request.prompt,
        "--output-format", "stream-json", "--verbose",
        "--model", model_id,
        "--permission-mode", "acceptEdits",
        "--append-system-prompt", request.system_prompt,
        *session_args,
    ]
    allowed = _allowed_tools(request.tools)
    if allowed:
        cmd += ["--allowedTools", ",".join(allowed)]

    result = PiResult(session_id=request.session_id)
    # stdin is DEVNULL for the same reason as agent_pi: the prompt travels in
    # argv, and an inherited non-TTY stdin can make the child wait on piped
    # input that never comes. stderr is drained on a thread so a chatty child
    # cannot deadlock against a full pipe while we read stdout.
    process = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8", errors="replace",
                               bufsize=1, cwd=request.cwd, env=operator_env())
    if on_spawn:
        on_spawn(process.pid)

    stderr_chunks: list[str] = []
    drain = threading.Thread(target=lambda: stderr_chunks.append(process.stderr.read()),
                             daemon=True)
    drain.start()

    last_text = ""
    with raw_path.open("a", encoding="utf-8") as raw:
        assert process.stdout is not None
        for line in process.stdout:
            raw.write(line)
            raw.flush()                      # events land on disk as they happen
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            if etype == "assistant":
                message = event.get("message") or {}
                for block in message.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "text" \
                            and block.get("text"):
                        last_text = block["text"]   # last assistant text wins
                usage = message.get("usage") or {}
                # Occupancy is read off the latest assistant turn; the same
                # usage object repeats on every block-event of one message, so
                # overwriting (never summing) is what keeps it honest.
                turn = _context_tokens(usage)
                if turn:
                    result.context_tokens = turn
            elif etype == "result":
                # The run's totals live here, once, already summed over every
                # turn — which is why spend is NOT accumulated per assistant
                # event (those repeat one message's usage per content block).
                result.cost = event.get("total_cost_usd") or 0.0
                pi_usage, total = _usage_as_pi(event.get("usage") or {}, result.cost)
                result.tokens = total
                result.usage.add_turn(pi_usage, total)
                if event.get("result"):
                    result.text = event["result"]
                for model_usage in (event.get("modelUsage") or {}).values():
                    window = int(model_usage.get("contextWindow") or 0)
                    if window:
                        result.context_window = window
                        break
            if on_event:
                on_event(event)

    result.returncode = process.wait()
    drain.join(timeout=10)
    if on_exit:
        on_exit(process.pid)
    if not result.text:
        result.text = last_text
    return result, "".join(stderr_chunks)
