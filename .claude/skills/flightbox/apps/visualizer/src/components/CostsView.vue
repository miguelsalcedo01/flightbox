<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, shallowRef } from 'vue'
import { CalendarDays, CircleDollarSign, Workflow } from 'lucide-vue-next'
import type { CostsResponse } from '../lib/types'
import { fetchCosts } from '../lib/api'
import { fmtCost, fmtDate, fmtTokens } from '../lib/format'
import { AGENT_FALLBACK_COLORS } from '../lib/events'

const costs = shallowRef<CostsResponse>({ by_adw: [], by_agent: [], by_day: [] })
const apiError = ref<string | null>(null)
const loaded = ref(false)

let timer: ReturnType<typeof setInterval> | undefined
let inflight = false

async function tick() {
  if (inflight) return
  inflight = true
  try {
    costs.value = await fetchCosts()
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

// ── Headline numbers ─────────────────────────────────────────────────────────

const totalSpend = computed(() => costs.value.by_adw.reduce((sum, r) => sum + r.total_cost, 0))
const totalRuns = computed(() => costs.value.by_adw.reduce((sum, r) => sum + r.runs, 0))

/** ISO date (UTC) n days ago — matches SQLite's date(started_at) grouping. */
function isoDaysAgo(n: number): string {
  const d = new Date(Date.now() - n * 86_400_000)
  return d.toISOString().slice(0, 10)
}

const last7Spend = computed(() => {
  const floor = isoDaysAgo(6)
  return costs.value.by_day.filter((d) => d.day >= floor).reduce((sum, d) => sum + d.cost, 0)
})

// ── Daily bar chart — last 30 days, gaps filled so quiet days still show ─────

interface DayBar {
  day: string
  /** "8/4" style tick label. */
  label: string
  runs: number
  cost: number
  tokens: number
  /** 0–100 against the busiest day. */
  pct: number
}

const dayBars = computed<DayBar[]>(() => {
  const byDay = new Map(costs.value.by_day.map((d) => [d.day, d]))
  const out: DayBar[] = []
  let max = 0
  for (let i = 29; i >= 0; i--) {
    const day = isoDaysAgo(i)
    const row = byDay.get(day)
    const cost = row?.cost ?? 0
    max = Math.max(max, cost)
    out.push({
      day,
      label: `${Number(day.slice(5, 7))}/${Number(day.slice(8, 10))}`,
      runs: row?.runs ?? 0,
      cost,
      tokens: row?.tokens ?? 0,
      pct: 0,
    })
  }
  for (const bar of out) bar.pct = max > 0 ? (bar.cost / max) * 100 : 0
  return out
})

function barTitle(bar: DayBar): string {
  return `${bar.day} — ${fmtCost(bar.cost)} across ${bar.runs} run${bar.runs === 1 ? '' : 's'} (${fmtTokens(bar.tokens)} tokens)`
}

function agentDot(color: string | null, index: number): string {
  return color ?? AGENT_FALLBACK_COLORS[index % AGENT_FALLBACK_COLORS.length] ?? '#c89bff'
}
</script>

<template>
  <div class="costs">
    <div v-if="apiError" class="error-bar">api unreachable — retrying {{ apiError }}</div>

    <div class="headline">
      <div class="stat-tile">
        <span class="tile-label"><CircleDollarSign :size="18" :stroke-width="2" /> total spend</span>
        <span class="tile-value">{{ fmtCost(totalSpend) }}</span>
      </div>
      <div class="stat-tile">
        <span class="tile-label"><Workflow :size="18" :stroke-width="2" /> total runs</span>
        <span class="tile-value">{{ totalRuns }}</span>
      </div>
      <div class="stat-tile">
        <span class="tile-label"><CalendarDays :size="18" :stroke-width="2" /> last 7 days</span>
        <span class="tile-value">{{ fmtCost(last7Spend) }}</span>
      </div>
    </div>

    <div class="panels">
      <section class="panel">
        <h2>by workflow</h2>
        <table v-if="costs.by_adw.length">
          <thead>
            <tr>
              <th>workflow</th>
              <th class="num">runs</th>
              <th class="num">spend</th>
              <th class="num">avg / run</th>
              <th class="num">tokens</th>
              <th class="num">last run</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in costs.by_adw" :key="row.adw_name">
              <td class="name" :title="row.adw_name">{{ row.adw_name }}</td>
              <td class="num">{{ row.runs }}</td>
              <td class="num strong">{{ fmtCost(row.total_cost) }}</td>
              <td class="num">{{ fmtCost(row.avg_cost) }}</td>
              <td class="num">{{ fmtTokens(row.total_tokens) }}</td>
              <td class="num dim">{{ fmtDate(row.last_run_at) }}</td>
            </tr>
          </tbody>
        </table>
        <div v-else-if="loaded" class="panel-empty">no runs yet</div>
      </section>

      <section class="panel">
        <h2>by agent</h2>
        <table v-if="costs.by_agent.length">
          <thead>
            <tr>
              <th>agent</th>
              <th class="num">runs</th>
              <th class="num">spend</th>
              <th class="num">tokens</th>
              <th class="num">last active</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(row, i) in costs.by_agent" :key="row.agent">
              <td class="name">
                <span class="agent-dot" :style="{ background: agentDot(row.color, i) }" />
                {{ row.agent }}
              </td>
              <td class="num">{{ row.runs || '—' }}</td>
              <td class="num strong">{{ fmtCost(row.total_cost) }}</td>
              <td class="num">{{ fmtTokens(row.total_tokens) }}</td>
              <td class="num dim">{{ fmtDate(row.last_used_at) }}</td>
            </tr>
          </tbody>
        </table>
        <div v-else-if="loaded" class="panel-empty">no agent activity yet</div>
      </section>

      <section class="panel panel-wide">
        <h2>by day — last 30 days</h2>
        <div class="chart" role="img" aria-label="Daily spend, last 30 days">
          <div v-for="bar in dayBars" :key="bar.day" class="bar-col" :title="barTitle(bar)">
            <span class="bar-amount" :class="{ show: bar.cost > 0 }">{{
              bar.cost > 0 ? fmtCost(bar.cost) : ''
            }}</span>
            <div class="bar-track">
              <div
                class="bar-fill"
                :style="{ height: bar.cost > 0 ? `${Math.max(bar.pct, 3)}%` : '0%' }"
              />
            </div>
            <span class="bar-label">{{ bar.label }}</span>
          </div>
        </div>
      </section>
    </div>

    <div v-if="!loaded && !apiError" class="empty-state">loading spend…</div>
  </div>
</template>

<style scoped>
.costs {
  padding: 20px 24px 40px;
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.headline {
  display: flex;
  gap: 18px;
  flex-wrap: wrap;
}

.stat-tile {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 16px 22px;
  min-width: 200px;
  border: 1px solid var(--border-soft);
  border-radius: 14px;
  background: var(--surface);
}

.tile-label {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 16px;
  color: var(--dim);
  letter-spacing: 0.04em;
}

.tile-value {
  font-family: var(--mono);
  font-variant-numeric: tabular-nums;
  font-size: 28px;
  font-weight: 700;
  color: var(--text);
}

.panels {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(440px, 1fr));
  gap: 18px;
}

.panel {
  border: 1px solid var(--border-soft);
  border-radius: 14px;
  background: var(--surface);
  padding: 16px 20px 20px;
  overflow-x: auto;
}

.panel-wide {
  grid-column: 1 / -1;
}

.panel h2 {
  margin: 0 0 12px;
  font-size: 17px;
  font-weight: 700;
  color: var(--dim);
  letter-spacing: 0.05em;
  text-transform: uppercase;
}

table {
  width: 100%;
  border-collapse: collapse;
  font-size: 16px;
}

th {
  text-align: left;
  font-weight: 400;
  color: var(--faint);
  font-size: 16px;
  padding: 6px 10px;
  border-bottom: 1px solid var(--border-soft);
  white-space: nowrap;
}

td {
  padding: 8px 10px;
  border-bottom: 1px solid rgba(34, 43, 61, 0.45);
  white-space: nowrap;
}

tr:last-child td {
  border-bottom: none;
}

th.num,
td.num {
  text-align: right;
  font-family: var(--mono);
  font-variant-numeric: tabular-nums;
}

td.name {
  color: var(--text);
  max-width: 26ch;
  overflow: hidden;
  text-overflow: ellipsis;
}

td.strong {
  color: var(--text);
  font-weight: 700;
}

.agent-dot {
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  margin-right: 8px;
  vertical-align: baseline;
}

.panel-empty {
  padding: 24px 0;
  color: var(--dim);
  font-size: 16px;
}

/* ── Daily bar chart — pure CSS, tooltips native, amounts on hover ─────────── */

.chart {
  display: flex;
  align-items: flex-end;
  gap: 5px;
  height: 220px;
  padding-top: 26px;
}

.bar-col {
  flex: 1 1 0;
  min-width: 0;
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  position: relative;
}

.bar-amount {
  position: absolute;
  top: -22px;
  font-family: var(--mono);
  font-size: 16px;
  color: var(--text);
  white-space: nowrap;
  opacity: 0;
  transition: opacity 0.12s ease;
  pointer-events: none;
}

.bar-col:hover .bar-amount.show {
  opacity: 1;
}

.bar-track {
  flex: 1;
  width: 100%;
  display: flex;
  align-items: flex-end;
  justify-content: center;
}

.bar-fill {
  width: 70%;
  max-width: 26px;
  border-radius: 4px 4px 0 0;
  background: linear-gradient(180deg, var(--purple), rgba(200, 155, 255, 0.25));
  transition: height 0.25s ease;
}

.bar-col:hover .bar-fill {
  background: linear-gradient(180deg, var(--cyan), rgba(90, 210, 221, 0.3));
  box-shadow: 0 0 12px rgba(90, 210, 221, 0.35);
}

.bar-label {
  font-family: var(--mono);
  font-size: 16px;
  color: var(--faint);
  white-space: nowrap;
  overflow: hidden;
  max-width: 100%;
  text-overflow: clip;
}

/* Thin out the tick labels so 30 of them never collide. */
.bar-col:nth-child(odd) .bar-label {
  visibility: hidden;
}
</style>
