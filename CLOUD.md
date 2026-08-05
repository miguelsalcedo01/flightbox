# Flightbox Cloud — the Solo slice

The smallest thing that can legitimately charge money: **sync, auth, hosted history, and
the existing UI made multi-tenant.** That is the whole of the Solo tier ($29/mo, ≤$1,500/mo
governed spend) and nothing more.

Everything Team and Agency need — policy distribution, spend attribution, signed audit
export, SSO/RBAC — is deliberately **out of this slice**. Ship Solo, find out whether
anyone actually wants a hosted layer, then build the rest against real users.

---

## What already exists (do not rebuild)

| Asset | Where | State |
|---|---|---|
| Trace schema, 9 tables | `adw_modules/tracer.py` | done — this is the hard part and it's finished |
| JSON read API, 9 endpoints | `apps/visualizer/server/index.ts` | done, single-tenant, reads a local `.db` |
| Web UI — waterfall, costs, story | `apps/visualizer/src/` | done, consumes that API |

**The gap, stated by the server's own header:** *"There is no ingest endpoint and no
websocket. The data path is agents → sqlite → web ui."* Nothing ever crosses a network.
Cloud inverts that, and everything genuinely new in this slice follows from it.

---

## Decisions

### Platform — Cloudflare Workers + D1

Because **D1 is SQLite**. The local store is SQLite, so the schema and the great majority
of existing queries port across with minimal translation. The existing API server is
TypeScript, which is what Workers run. Wrangler is already authenticated on this machine
and already deploys the marketing site.

*Known ceiling:* D1 caps around 10 GB per database and is not built for heavy sustained
write throughput. At Solo volumes this is a non-issue; revisit at Team scale, and design
the ingest layer so the store is swappable (see "keep the seam" below).

*Alternative if that ceiling arrives sooner than expected:* Postgres on Supabase or Neon.
Costs a schema translation pass; buys headroom.

### Auth — Clerk for humans, workspace tokens for machines

Two different problems, and conflating them is a common mistake:

- **Web login** (viewing history): Clerk. Fastest to ship, and it already models
  organisations, which Team will need. GitHub OAuth is the zero-vendor alternative and
  fits a developer audience well — pick it if avoiding a dependency matters more than
  speed.
- **CLI push** (`flightbox sync`): a long-lived **workspace token**, minted in the web UI,
  stored at `~/.config/flightbox/credentials` with `0600`. Never a Clerk session token —
  CI has no browser.

### Sync privacy — metadata by default, full trace opt-in

This is the decision with the longest tail, so make it deliberately.

`envelopes` and prompt records contain **the user's actual source code and prompts**. The
README now promises traces stay local unless the user chooses otherwise. Syncing prompt
bodies by default would break that promise in spirit, and it is the first objection an
agency's client will raise.

| Mode | Ships | Default |
|---|---|---|
| `metadata` | sessions, phases, event rows, tokens, costs, gate results, halt reasons | **yes** |
| `full` | the above plus `payload_json`, prompts, diffs | opt-in, per repo |

Metadata alone fully satisfies the Solo value proposition — cross-machine history and
cost analytics. Configure via `flightbox.config.yaml`:

```yaml
cloud:
  sync: metadata        # off | metadata | full
  workspace: <slug>
```

State the difference plainly in the docs, and log which mode a run synced under.

---

## Schema

Client-generated `TEXT` primary keys (`adw_id`, `event_id`, …) are already globally
unique, so **ingest is idempotent for free** — upsert on the natural key and a re-sync
cannot duplicate.

**New tables:**

```
workspaces        id, slug, name, owner_user_id, created_at
users             id (clerk id), email, created_at
workspace_members workspace_id, user_id, role            -- role unused until Team
workspace_tokens  id, workspace_id, token_hash, name, last_used_at, created_at, revoked_at
subscriptions     workspace_id, stripe_customer_id, stripe_subscription_id,
                  status, tier, band_usd, current_period_end
sync_log          workspace_id, adw_id, mode, rows, synced_at
```

**Existing tables:** add `workspace_id TEXT NOT NULL` to *every* synced table and index
`(workspace_id, adw_id)`. Denormalising it onto children rather than joining through
`sessions` keeps every query scoped by a single predicate — the cheapest possible defence
against a cross-tenant leak.

Store `token_hash`, never the token. Show the plaintext once, at mint time.

---

## Ingest contract

```
POST /v1/ingest
Authorization: Bearer <workspace token>
Content-Type: application/json

{ "mode": "metadata",
  "session": { ...sessions row... },
  "phases": [...], "events": [...], "gate_results": [...], "approvals": [...] }
```

- **Idempotent.** Upsert on primary key; re-sending a run is a no-op.
- **Batched and resumable.** The client tracks a high-water mark per `adw_id` and ships
  deltas; a failed sync retries without duplicating.
- **Bounded.** Cap request size; chunk large runs. Reject unknown columns rather than
  silently dropping them.
- **Never blocks a run.** Sync is out-of-band — after a run, or via `flightbox sync`. A
  Cloud outage must never delay, fail, or alter an agent run.

---

## Endpoints

| Method | Path | Notes |
|---|---|---|
| POST | `/v1/ingest` | above |
| GET | `/v1/sessions` | workspace-scoped port of the existing endpoint |
| GET | `/v1/sessions/:adw_id` | + `/events`, `/envelopes`, `/gates`, `/agents/:agent/prompts` |
| GET | `/v1/costs` | port of `/api/costs`, scoped |
| POST/DELETE | `/v1/tokens` | mint / revoke workspace tokens |
| POST | `/v1/webhooks/stripe` | signature-verified |
| GET | `/v1/health` | |

The read endpoints are the existing nine with a workspace predicate added. **Port them,
don't reinvent them** — and keep the response shapes identical so the existing UI works
against Cloud with only a base-URL change.

---

## Entitlements

Stripe webhook (`checkout.session.completed`, `customer.subscription.updated|deleted`)
writes `subscriptions`. Verify the signature; ignore unsigned posts.

Enforcement rules, which must match what the pricing page already promises:

1. **No active subscription → ingest returns 402.** Reads of already-synced data stay
   available for a grace period, then go read-only. Never delete a customer's data on
   cancellation without explicit notice.
2. **Over the spend band → never block.** Record the overage, surface it in the UI, nudge
   after three consecutive months. The site says overage is billed at $12 per additional
   $1,000 governed, not enforced by cutoff.
3. **Never touch the local governor.** It is free, it is theirs, and it runs with no
   network. Cloud has no mechanism to disable it and must never acquire one. Halting
   someone's budget cap over an invoice would be indefensible, and the site says so.

---

## Milestones

| # | Deliverable | Done when |
|---|---|---|
| 1 | D1 schema + migrations, multi-tenant | tables exist; a seeded workspace returns scoped rows |
| 2 | Workspace tokens (mint, hash, verify, revoke) | a token authenticates a request; a revoked one 401s |
| 3 | `POST /v1/ingest`, idempotent | same run synced twice yields identical row counts |
| 4 | `flightbox sync` client + config, metadata mode | a real local run appears in D1 |
| 5 | Read endpoints ported, workspace-scoped | existing UI runs against Cloud with a base-URL change |
| 6 | Clerk web auth + workspace creation | sign up, land on your own empty workspace |
| 7 | Stripe webhook → `subscriptions` | a live checkout flips a workspace to active |
| 8 | Deploy + custom domain (`app.flightbox.dev`) | end-to-end on a clean machine |

Ship 1–5 behind a flag first: that is the whole product minus billing, and it is the part
worth testing on yourself before charging anyone.

---

## Keep the seam

Put every D1 call behind a small store interface. It costs an afternoon now and makes the
Postgres migration a rewrite of one module rather than the whole Worker. The same applies
to auth: one `identify(request) -> {workspace_id, user_id}` function, so swapping Clerk
for GitHub OAuth later touches one file.

## Cross-tenant leakage is the one unrecoverable bug

Everything else here is fixable in a patch. Leaking one customer's prompts or costs into
another customer's dashboard is not. Two cheap defences, both worth doing before launch:

1. Every query takes `workspace_id` as a bound parameter — no exceptions, no "the join
   already scopes it".
2. A test that seeds two workspaces and asserts every endpoint returns nothing from the
   other. Run it in CI.
