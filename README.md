# Flightbox

> **The flight recorder for AI agent workflows — with a spend governor in the loop.**
> Run your agents through Flightbox and every run is recorded, cost-attributed, and budget-governed. Because Flightbox owns the loop, it can *stop* an agent at its spending cap — not just alert you after the invoice.

Teams running AI agents are flying blind: no audit trail of what an agent actually did, and the invoice is the first sign something went wrong. Flightbox fixes both, by construction rather than by instrumentation.

## How it works

Flightbox is a managed runway for agent workflows (ADWs — AI Developer Workflows). Deterministic Python owns the graph; coding agents are bounded nodes inside it. **Agent proposes, code disposes.** That architecture is what makes the recorder trustworthy and the governor enforceable:

- **Flight recorder.** Every event streams into a SQLite trace DB while the run is live: sessions, phases, tool calls (one readable row per call, with args, result, duration, and outcome), envelopes, gate results, and process IDs. Files stay the raw record; the DB is the queryable mirror. A read-only replay UI (`.claude/skills/flightbox/apps/visualizer/`) shows sessions, a trace waterfall, and per-phase tool-call detail.
- **Cost attribution.** Per-call token and dollar breakdowns (input / output / cache / reasoning), accumulated per agent, per phase, per session. You can answer "what did this run cost, and which agent spent it" with one query.
- **Governance.** Spend caps that *stop the run*: `max_run_cost` on the roster and `max_cost` per agent, enforced at call boundaries — the crossing send is billed and traced, then no further send starts — with **approach warnings** at 75% and 90% of every cap first, because a send can't be stopped mid-flight: the governor's second job is showing you the line while a call boundary is still ahead of it. An advisory **`month_budget`** adds the long horizon: every launch reports month-to-date spend, the last-7-days daily pace, and the projected month-end (`just month` on demand), flagging when today's pace lands the month over. **Approval phases** that block the run on a human: `ph.approval(...)` parks the chain on a pending row until someone grants it — interactively at the terminal, or from anywhere with `just approve <adw_id> <name>` (`just pending` lists what's waiting; silence past the timeout is a denial, because a gate that defaults open is not a gate). **Pre-run cost estimates** at every launch: the trace's own history read back as median / p90 / worst for the ADW you're about to run — no pricing tables, your actual bills are the dataset — with a warning when the p90 already clears your `max_run_cost`, so "this will probably halt on its budget" is said before the first token is bought. Plus per-agent write boundaries enforced in code with automatic rollback, protected paths no agent may touch, gates as the definition of done, and bounded retry loops. Spend roll-ups live in the visualizer's `#/costs` dashboard, and the plain-language replay in each session's story view.

## Install

One line, from the root of the repo you want governed:

```bash
curl -fsSL https://raw.githubusercontent.com/miguelsalcedo01/flightbox/main/get-flightbox.sh | sh
```

Windows (PowerShell):

```powershell
iwr -useb https://raw.githubusercontent.com/miguelsalcedo01/flightbox/main/get-flightbox.ps1 | iex
```

That fetches the skill, stamps the factory (idempotent — existing files are never overwritten), and preps `.env`. Then:

```bash
just demo                                            # smoke test: two cheap read-only runs
just obs                                             # the replay UI (needs bun)
```

Already have a checkout, or working offline? Point the bootstrapper at it: `FLIGHTBOX_REPO=/path/to/flightbox sh get-flightbox.sh`. Manual install is still just two steps: copy `.claude/skills/flightbox` into your repo and run `uv run .claude/skills/flightbox/scripts/install.py`. Or agentically: copy the skill in and type `/flightbox install` inside Claude Code.

**Prereqs:** [`uv`](https://docs.astral.sh/uv/), `sqlite3`, and either [`pi`](https://github.com/mariozechner/pi-coding-agent) or Claude Code as the coding-agent backend. [`bun`](https://bun.sh) for the visualizer.

## Watch a run

```bash
just sessions          # recent runs: status, request, tokens, cost
just phases <adw_id>   # the phase spine of one run
just tail <adw_id>     # live event stream
just procs <adw_id>    # what's still alive
```

Or query the trace directly — reads never block a running workflow (WAL):

```bash
sqlite3 adws/adw_data/flightbox.db "select adw_id, status, substr(request,1,60), total_tokens, total_cost from sessions order by started_at desc limit 10;"
```

## Status

Early but real: orchestration, live trace, session replay (engineer waterfall + plain-language story view), cost attribution and roll-ups, budget gates with approach warnings, approval phases, pre-run estimates, and monthly budget projection all work today. The hosted multi-repo dashboard, alert delivery, and team accounts are in progress.

## Credits & license

MIT — see [`LICENSE`](LICENSE).

Flightbox builds on MIT-licensed open-source software, substantially extended for spend governance and audit — see [`CREDITS.md`](CREDITS.md); the original copyright notice is preserved in [`LICENSE`](LICENSE).
