<script setup lang="ts">
/**
 * The plain-language replay: the same run the waterfall shows, retold as
 * sentences a non-engineer can read top to bottom. No jargon — "workflow",
 * not ADW; "checks", not gates; every dollar figure right-aligned in mono.
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ChartNoAxesGantt } from 'lucide-vue-next'
import type { AgentEndPayload, EventRow, Phase, Session } from '../lib/types'
import { fetchEvents, fetchSession } from '../lib/api'
import { fmtClock, fmtCost, fmtDuration, fmtTokens, ts } from '../lib/format'
import { parsePayload } from '../lib/events'
import { hrefFor } from '../lib/router'

const props = defineProps<{ adwId: string }>()

const session = ref<Session | null>(null)
const phases = ref<Phase[]>([])
const events = ref<EventRow[]>([])
const apiError = ref<string | null>(null)
const loaded = ref(false)
const nowMs = ref(Date.now())

let cursor = 0
let inflight = false
let timer: ReturnType<typeof setInterval> | undefined

async function tick() {
  if (inflight) return
  inflight = true
  try {
    const detail = await fetchSession(props.adwId)
    session.value = detail.session
    phases.value = detail.phases

    const fresh: EventRow[] = []
    let page
    do {
      // Cursor pagination is inherently sequential: each request needs the previous cursor.
      // oxlint-disable-next-line no-await-in-loop
      page = await fetchEvents(props.adwId, cursor, 1000)
      cursor = Math.max(cursor, page.cursor)
      fresh.push(...page.events)
    } while (page.has_more)
    if (fresh.length) events.value = [...events.value, ...fresh]

    nowMs.value = Date.now()
    apiError.value = null
    loaded.value = true
  } catch (err) {
    apiError.value = err instanceof Error ? err.message : String(err)
  } finally {
    inflight = false
  }
}

onMounted(() => {
  void tick()
  timer = setInterval(() => void tick(), 500)
})

onUnmounted(() => clearInterval(timer))

// ── Story assembly ───────────────────────────────────────────────────────────

type Tone = 'normal' | 'phase' | 'green' | 'red' | 'amber'

interface StoryEntry {
  key: string
  timeMs: number
  clock: string
  /** The sentence. */
  text: string
  /** Secondary line under the sentence — a description or a tool summary. */
  sub: string | null
  /** Right column: money spent at this moment, when there was any. */
  cost: number | null
  tokens: number | null
  tone: Tone
}

const phaseById = computed(() => new Map(phases.value.map((p) => [p.phase_id, p])))

/** Owner of the phase an event ran under — "who was doing this". */
function ownerOf(e: EventRow): string | null {
  return (e.phase_id ? phaseById.value.get(e.phase_id)?.owner : null) ?? null
}

/**
 * Tool-call counts per phase, bucketed into human verbs. A finance reviewer
 * needs "ran 14 commands, read 6 files", never the 14 command lines.
 */
interface ToolBuckets {
  bash: number
  read: number
  edit: number
  write: number
  other: number
}

const toolBucketsByPhase = computed<Map<string, ToolBuckets>>(() => {
  const map = new Map<string, ToolBuckets>()
  for (const e of events.value) {
    if (e.type !== 'tool_call' || !e.phase_id) continue
    let buckets = map.get(e.phase_id)
    if (!buckets) {
      buckets = { bash: 0, read: 0, edit: 0, write: 0, other: 0 }
      map.set(e.phase_id, buckets)
    }
    const payload = parsePayload(e.payload_json)
    const tool = (typeof payload?.tool === 'string' ? payload.tool : (e.name ?? ''))
      .split(':')[0]
      ?.trim()
      .toLowerCase()
    if (tool === 'bash' || tool === 'shell') buckets.bash++
    else if (tool === 'read') buckets.read++
    else if (tool === 'edit') buckets.edit++
    else if (tool === 'write') buckets.write++
    else buckets.other++
  }
  return map
})

function plural(n: number, noun: string): string {
  return `${n} ${noun}${n === 1 ? '' : 's'}`
}

function toolSummary(phaseId: string | null): string | null {
  if (!phaseId) return null
  const b = toolBucketsByPhase.value.get(phaseId)
  if (!b) return null
  const parts: string[] = []
  if (b.bash) parts.push(`ran ${plural(b.bash, 'command')}`)
  if (b.read) parts.push(`read ${plural(b.read, 'file')}`)
  if (b.edit) parts.push(`edited ${plural(b.edit, 'file')}`)
  if (b.write) parts.push(`wrote ${plural(b.write, 'file')}`)
  if (b.other) parts.push(`${plural(b.other, 'other action')}`)
  return parts.length ? parts.join(', ') : null
}

function asString(value: unknown): string | null {
  if (typeof value === 'string' && value.trim() !== '') return value
  return null
}

/** Violations / breaches arrive as JSON arrays of strings; flatten kindly. */
function listOf(value: unknown): string | null {
  if (Array.isArray(value)) {
    const items = value.filter((v): v is string => typeof v === 'string')
    if (items.length) return items.join('; ')
  }
  return asString(value)
}

const entries = computed<StoryEntry[]>(() => {
  const out: StoryEntry[] = []
  const s = session.value
  if (!s) return out

  const startMs = ts(s.started_at)
  const who = s.engineer ?? 'Someone'
  out.push({
    key: 'run-start',
    timeMs: Number.isFinite(startMs) ? startMs : 0,
    clock: fmtClock(s.started_at),
    text: `${who} asked: ${s.request ?? '(no request recorded)'}`,
    sub: s.adw_name ? `workflow: ${s.adw_name.replaceAll('adw_', '').replaceAll('_', ' ')}` : null,
    cost: null,
    tokens: null,
    tone: 'normal',
  })

  const sorted = events.value.toSorted((a, b) => a.rowid - b.rowid)
  for (const e of sorted) {
    const timeMs = ts(e.started_at)
    const clock = fmtClock(e.started_at)
    const base = { timeMs: Number.isFinite(timeMs) ? timeMs : 0, clock }
    const payload = parsePayload(e.payload_json)

    switch (e.type) {
      case 'phase_start': {
        const phase = e.phase_id ? phaseById.value.get(e.phase_id) : undefined
        // The engineer's request phase is already the opening line.
        if (phase?.kind === 'engineer') break
        const owner = phase?.owner ?? 'the workflow'
        const name = phase?.name ?? e.name ?? 'a step'
        out.push({
          ...base,
          key: e.event_id,
          text: `${owner} started ${name}`,
          sub:
            [phase?.description ?? null, phase?.kind === 'agent' ? toolSummary(e.phase_id) : null]
              .filter(Boolean)
              .join(' — ') || null,
          cost: null,
          tokens: null,
          tone: 'phase',
        })
        break
      }
      case 'agent_end': {
        const p = (payload ?? {}) as AgentEndPayload
        const agent = ownerOf(e) ?? e.name ?? 'agent'
        const cost = typeof p.cost === 'number' ? p.cost : null
        const tokens = p.usage?.total_tokens ?? e.tokens ?? null
        out.push({
          ...base,
          key: e.event_id,
          text: `${agent} finished — spent ${fmtCost(cost)}${tokens != null ? ` (${fmtTokens(tokens)} tokens)` : ''}`,
          sub: null,
          cost,
          tokens,
          tone: 'normal',
        })
        break
      }
      case 'gate_pass': {
        out.push({
          ...base,
          key: e.event_id,
          text: `checks passed: ${e.name ?? 'all checks'}`,
          sub: null,
          cost: null,
          tokens: null,
          tone: 'green',
        })
        break
      }
      case 'gate_fail': {
        const violations = listOf(payload?.violations) ?? e.name ?? 'see details'
        out.push({
          ...base,
          key: e.event_id,
          text: `checks FAILED: ${violations}`,
          sub: null,
          cost: null,
          tokens: null,
          tone: 'red',
        })
        break
      }
      case 'error': {
        if (e.name === 'budget_exceeded') {
          const breaches =
            listOf(payload?.breaches) ??
            asString(payload?.message) ??
            'spending limit reached'
          out.push({
            ...base,
            key: e.event_id,
            text: `BUDGET HALT: ${breaches}`,
            sub: 'the workflow was stopped before it could spend more',
            cost: null,
            tokens: null,
            tone: 'red',
          })
        }
        break
      }
      case 'approval_requested': {
        const desc = asString(payload?.description) ?? e.name ?? 'a decision'
        out.push({
          ...base,
          key: e.event_id,
          text: `Waiting on human approval: ${desc}`,
          sub: null,
          cost: null,
          tokens: null,
          tone: 'amber',
        })
        break
      }
      case 'approval_granted': {
        const by = asString(payload?.decided_by)
        out.push({
          ...base,
          key: e.event_id,
          text: by ? `Approved by ${by}` : 'Approved',
          sub: asString(payload?.description),
          cost: null,
          tokens: null,
          tone: 'amber',
        })
        break
      }
      case 'approval_denied': {
        const by = asString(payload?.decided_by)
        out.push({
          ...base,
          key: e.event_id,
          text: by ? `Denied by ${by}` : 'Denied',
          sub: asString(payload?.description),
          cost: null,
          tokens: null,
          tone: 'amber',
        })
        break
      }
      case 'handoff': {
        const agent = ownerOf(e) ?? 'agent'
        const summary = asString(payload?.summary) ?? e.name ?? ''
        out.push({
          ...base,
          key: e.event_id,
          text: `${agent} handed off${summary ? `: ${summary}` : ''}`,
          sub: null,
          cost: null,
          tokens: null,
          tone: 'normal',
        })
        break
      }
      default:
        break
    }
  }

  if (s.ended_at) {
    const endMs = ts(s.ended_at)
    const durationMs =
      Number.isFinite(endMs) && Number.isFinite(startMs) ? endMs - startMs : NaN
    const outcome =
      s.status === 'success'
        ? 'The workflow finished successfully'
        : s.status === 'fail'
          ? 'The workflow FAILED'
          : 'The workflow ended'
    out.push({
      key: 'run-end',
      timeMs: Number.isFinite(endMs) ? endMs : 0,
      clock: fmtClock(s.ended_at),
      text: `${outcome} — total spend ${fmtCost(s.total_cost)}, took ${fmtDuration(durationMs)}`,
      sub: s.total_tokens != null ? `${fmtTokens(s.total_tokens)} tokens billed in total` : null,
      cost: s.total_cost,
      tokens: s.total_tokens,
      tone: s.status === 'fail' ? 'red' : 'green',
    })
  }

  return out
})

const running = computed(() => session.value?.status === 'running')
</script>

<template>
  <div class="story">
    <div v-if="apiError" class="error-bar">api unreachable — retrying {{ apiError }}</div>

    <div v-if="session" class="story-head">
      <h1>What happened in this run</h1>
      <a class="tech-link" :href="hrefFor(props.adwId)">
        <ChartNoAxesGantt :size="18" :stroke-width="2" />
        engineer view
      </a>
    </div>

    <div v-if="entries.length" class="timeline">
      <div
        v-for="entry in entries"
        :key="entry.key"
        class="entry"
        :class="`tone-${entry.tone}`"
      >
        <span class="clock">{{ entry.clock }}</span>
        <span class="dot" />
        <span class="body">
          <span class="text">{{ entry.text }}</span>
          <span v-if="entry.sub" class="sub">{{ entry.sub }}</span>
        </span>
        <span class="spend">{{ entry.cost != null ? fmtCost(entry.cost) : '' }}</span>
      </div>
      <div v-if="running" class="entry tone-normal live-entry">
        <span class="clock">now</span>
        <span class="dot live" />
        <span class="body"><span class="text dim">still running…</span></span>
        <span class="spend" />
      </div>
    </div>
    <div v-else-if="loaded" class="empty-state">nothing recorded for this run yet</div>
    <div v-else-if="!apiError" class="empty-state">loading story…</div>
  </div>
</template>

<style scoped>
.story {
  padding: 20px 24px 48px;
  max-width: 960px;
}

.story-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 18px;
  flex-wrap: wrap;
}

.story-head h1 {
  margin: 0;
  font-size: 22px;
  font-weight: 700;
}

.tech-link {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  font-size: 16px;
  color: var(--dim);
  border: 1px solid var(--border-soft);
  border-radius: 999px;
  padding: 5px 14px;
  background: rgba(19, 26, 38, 0.6);
}

.tech-link:hover {
  color: var(--text);
  border-color: var(--border);
}

/* ── Vertical timeline: one line, one dot per moment ───────────────────────── */

.timeline {
  position: relative;
  padding: 8px 0;
}

/* The spine — runs through the dot column. */
.timeline::before {
  content: '';
  position: absolute;
  top: 12px;
  bottom: 12px;
  /* clock column (86px) + gap (14px) + half the dot (6px) − half the line */
  left: 105px;
  width: 2px;
  background: linear-gradient(
    180deg,
    rgba(200, 155, 255, 0.4),
    rgba(90, 210, 221, 0.28) 60%,
    rgba(90, 210, 221, 0.08)
  );
}

.entry {
  position: relative;
  display: grid;
  grid-template-columns: 86px 12px 1fr auto;
  gap: 0 14px;
  align-items: baseline;
  padding: 9px 0;
}

.clock {
  font-family: var(--mono);
  font-size: 16px;
  color: var(--faint);
  text-align: right;
  white-space: nowrap;
}

.dot {
  width: 12px;
  height: 12px;
  border-radius: 50%;
  background: var(--panel-2);
  border: 2px solid var(--faint);
  align-self: center;
  z-index: 1;
}

.body {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}

.text {
  font-size: 17px;
  color: var(--text);
  overflow-wrap: anywhere;
}

.sub {
  font-size: 16px;
  color: var(--dim);
  overflow-wrap: anywhere;
}

.spend {
  font-family: var(--mono);
  font-variant-numeric: tabular-nums;
  font-size: 16px;
  color: var(--dim);
  text-align: right;
  white-space: nowrap;
  min-width: 76px;
}

/* Phase entries group the moments under them: bolder line, colored dot. */
.tone-phase .text {
  font-weight: 700;
}

.tone-phase .dot {
  border-color: var(--purple);
  box-shadow: 0 0 8px rgba(200, 155, 255, 0.4);
}

.tone-green .dot {
  border-color: var(--green);
  box-shadow: 0 0 8px rgba(74, 222, 128, 0.4);
}

.tone-green .text {
  color: var(--green);
}

.tone-red {
  background: rgba(255, 111, 103, 0.07);
  border-radius: 10px;
}

.tone-red .dot {
  border-color: var(--red);
  background: rgba(255, 111, 103, 0.4);
  box-shadow: 0 0 10px rgba(255, 111, 103, 0.55);
}

.tone-red .text {
  color: var(--red);
  font-weight: 700;
}

.tone-amber {
  background: rgba(232, 182, 74, 0.07);
  border-radius: 10px;
}

.tone-amber .dot {
  border-color: var(--amber);
  background: rgba(232, 182, 74, 0.35);
  box-shadow: 0 0 10px rgba(232, 182, 74, 0.5);
}

.tone-amber .text {
  color: var(--amber);
  font-weight: 700;
}

.dot.live {
  border-color: var(--blue);
  animation: pulse 1.4s ease-in-out infinite;
}
</style>
