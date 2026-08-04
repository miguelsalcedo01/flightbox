# Flightbox — Gap Analysis & Clone/Rebrand Plan

*Source: `super-simple-software-factory` (branch `claude-code-backend`), evaluated 2026-08-04 against the Ideabrowser "Flightbox" idea (AI agent flight recorder + spend governance).*

## Verdict

**Genuine fit — roughly 55–65% of Flightbox's core infrastructure already exists**, but with one big strategic caveat (see "The honest gap" below).

## Capability map

### 1. Session replay — ✅ ~80% built
- Every event streams live into SQLite (`adws/adw_data/sssf.db`, WAL mode): 10 event types across 7 tables (`sessions`, `phases`, `events`, `envelopes`, `gate_results`, `agent_sessions`, `processes`).
- Tool calls are folded into one readable row per real call (`bash: ls -la src`) with `{tool, args, result_snippet, ok, duration_ms, agent}`; `parent_id` nests spans so a phase expands into its tool calls.
- A read-only Vue/Vite replay UI already exists (`.claude/skills/sssf/apps/visualizer/`): session list, trace waterfall, per-phase tool-call detail. Live view and history are the same cursor query.
- **Gap:** UI is engineer-grade. Flightbox's buyer includes security/finance — needs a plain-language timeline, diff views, and "what did the agent read/write/spend" summaries a non-engineer can navigate.

### 2. Cost attribution — ✅ ~70% built
- Per-call `UsageBreakdown`: input/output/cache-read/cache-write/reasoning tokens, per-component costs, `total_cost` (`adw_modules/data_types.py`).
- Both backends report spend: pi (per-provider costs) and Claude Code (`total_cost_usd`) — `agent_pi.py`, `agent_cc.py`.
- Accumulated per session in the trace DB (`tracer.session_add_usage`), attributed per agent and per phase, printed per run.
- **Gap:** no roll-ups by team/project/time period, no dashboards, no cost-per-provider pricing tables of its own (it trusts what the harness reports — fine for pi/Claude Code, the hard part for arbitrary providers).

### 3. Policy enforcement — ⚠️ ~40% built
- Already enforced in code: per-agent `writes` boundaries with post-call diff + automatic rollback, `protected_files`, gates as the definition of done, bounded retry loops, `processes` table so a stuck run can be found and killed.
- **Missing entirely: spend governance.** No task/session budget caps, no "stop the agent at $X", no approval workflow for risky actions, no pre-run cost estimates or alerts. The word "approval" in the codebase is only the reviewer accept/revise loop.
- This is also the *cheapest big win*: the harness already owns the loop and accumulates cost per call, so a budget gate ("if `run.total_cost > cap`: halt phase, require human approval") is a small, natural extension of the existing gate/phase machinery.

## The honest gap (the feasibility 6/10, showing up on schedule)

SSSF logs agents **it launches** (pi and Claude Code subprocesses inside ADW chains). Flightbox promises to record **any production agent** across providers. SSSF is an orchestrator that traces its own runs — it is not a drop-in logging SDK/proxy for someone else's agent stack. Two positioning options:

- **Option A (fastest to revenue):** sell the *managed runway*, not the recorder. "Run your agents through Flightbox and you get flight-recording, replay, and spend governance for free." The factory is the product; observability is the moat. Weaker TAM, much stronger differentiation and near-zero new infra.
- **Option B (the full Ideabrowser thesis):** extract the trace schema + tracer + visualizer into a standalone ingest layer (OTel-compatible spans, provider SDK wrappers/proxy). Bigger market, but this is the genuinely hard cross-provider maintenance work — 6/10 feasibility applies here, not to what's already built.

Recommended: ship A first (it's ~4–6 weeks of product work), design the schema extraction so B stays open.

**DECISION (2026-08-04): Option A — managed runway. Approved by Miguel.**

## Option A revenue model

**Positioning:** "Run your agent workflows through Flightbox. Every run is flight-recorded, cost-attributed, and spend-governed — because we own the loop, we can *stop* an agent at its budget, not just alert after the invoice."

**Who the first 10 customers are:** teams *about to* operationalize agents, not teams with an existing stack to instrument:
1. Agencies/consultancies shipping AI automation to clients who demand an audit trail ("what did the agent do to my repo, and what did it cost").
2. Small eng teams (5–30 devs) adopting agentic coding workflows who need per-run cost visibility before finance signs off.
3. Solo operators running high-volume repeated workflows (content pipelines, migrations, triage bots) where run-1000 consistency and cost caps matter.
Channel: the SSSF/agentic-engineering audience already exists (IndyDevDan's video traffic proves demand for this exact shape); Flightbox is the productized, governed version.

**Tier boundary when you own the runway:**
| Tier | Price | What gates it |
|---|---|---|
| Free / OSS core | $0 | Stamp the factory, local trace DB, engineer-grade visualizer. This is the funnel — same play as the SSSF repo itself. |
| Recorder | $99/mo | Hosted/aggregated replay UI across repos & teammates, plain-language session timelines, spend roll-ups by agent/workflow/day, retention. |
| Governance | $499/mo | Budget gates that halt runs, approval phases for risky actions, pre-run cost estimates, threshold alerts (webhook/Slack/email), policy file per repo, org-wide policy view. |
| Enterprise | Custom | SSO, SOC 2 posture, finance export (cost-center mapping), multi-org policy, private hosting. |

Rule of thumb for the boundary: **seeing** what agents did is $99; **controlling** what agents may do is $499. The free tier is deliberately generous because the OSS core is already public upstream — value concentrates in aggregation, governance, and hosting.

**Path to $1M ARR (sanity check):** ~170 customers at $499 or ~840 at $99-equivalent mix. At agency/team pricing with a working governance story, 100–200 paying teams is a realistic 18–24 month target for a solo/duo operation with an existing audience channel.

## Clone & rebrand plan

Licensing: SSSF is MIT (IndyDevDan). Forking, rebranding, and commercializing are permitted — **keep the LICENSE file / copyright notice** and don't imply his endorsement.

### Step 1 — Clone into a fresh repo
```bash
cd "C:\Users\migue\OneDrive\Documents\GitHub"
git clone SoftwareFactory/super-simple-software-factory flightbox
cd flightbox
git checkout claude-code-backend
git remote remove origin        # detach from upstream before creating your own repo
```
Then create a private GitHub repo `flightbox` and push. (Memory note: the SSSF port already flagged "needs fork before push" — same rule applies.)

### Step 2 — Rebrand (mechanical, ~1 day)
Name decision: **Flightbox** is the working name (matches the idea; "flight recorder" framing sells to non-technical buyers). Swapping to Blackbox later is a find-replace on the same list.
- `sssf` → `fbx` everywhere it's an identifier: skill dir `.claude/skills/sssf` → `flightbox`, `sssf.config.yaml` → `flightbox.config.yaml`, `sssf.db` → `flightbox.db`, env prefix `SSSF_` → `FBX_`, justfile recipes, `SKILL.md` name.
- Rewrite README around the flight-recorder story; keep the architecture docs.
- Visualizer: retitle, new palette/logo, port stays 4600.

### Step 3 — Product work to reach the $99 tier (replay + observability)
1. Non-engineer replay UI: plain-language event timeline, per-session spend summary, file-change diff view, search/filter.
2. Session roll-ups: spend by agent / workflow / day; a simple dashboard page over the existing `sessions` table.
3. Multi-repo aggregation: one visualizer over N stamped repos' DBs (the `--db` flag already points anywhere; add a registry).

### Step 4 — Product work to reach the $499 tier (governance)
1. **Budget gates:** `max_cost` per agent and per run in config; harness halts the phase when exceeded (extend `agents.py` `add_usage` path + a new gate).
2. **Approval phase kind:** `kind="engineer"` already exists conceptually — implement a blocking human-approval step for gated actions (deploys, spends over threshold).
3. **Pre-run estimates & alerts:** rough cost projection from the chain's history in `sessions`; webhook/email alert on threshold crossings.
4. Policy file: per-repo `flightbox.policy.yaml` (spend caps, protected paths, approval-required actions) — the `writes`/`protected_files` machinery generalized.

### Step 5 — Later (only if pursuing Option B)
- Extract tracer + schema into `flightbox-ingest` (OTel span mapping, provider wrappers for OpenAI/Anthropic/Gemini SDKs).
- SOC 2 posture, hosted DB (SQLite → Turso/Postgres), auth + orgs for the Enterprise tier.

## Effort estimate
| Milestone | New work |
|---|---|
| Rebranded working clone | ~1 day |
| $99-tier feature-complete (replay + spend visibility) | ~2–3 weeks |
| $499-tier governance (budget gates, approvals, alerts) | ~2–3 weeks more |
| Cross-provider standalone recorder (Option B) | months, ongoing maintenance |
