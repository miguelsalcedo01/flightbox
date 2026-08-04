<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, shallowRef } from 'vue'
import { CircleDollarSign } from 'lucide-vue-next'
import type { SessionSummary } from '../lib/types'
import { fetchSessions } from '../lib/api'
import { ts } from '../lib/format'
import { costsHref } from '../lib/router'
import SessionCard from './SessionCard.vue'

const sessions = shallowRef<SessionSummary[]>([])
const apiError = ref<string | null>(null)
const loaded = ref(false)
const nowMs = ref(Date.now())

let timer: ReturnType<typeof setInterval> | undefined
let inflight = false

async function tick() {
  if (inflight) return
  inflight = true
  try {
    sessions.value = await fetchSessions()
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

/** Optimistic removal; an empty id means the write failed, so re-sync instead. */
function onArchived(adwId: string) {
  if (!adwId) {
    void tick()
    return
  }
  sessions.value = sessions.value.filter((s) => s.adw_id !== adwId)
}

const ordered = computed(() =>
  sessions.value.toSorted((a, b) => (ts(b.started_at) || 0) - (ts(a.started_at) || 0)),
)
</script>

<template>
  <div class="sessions">
    <div v-if="apiError" class="error-bar">api unreachable — retrying {{ apiError }}</div>

    <div class="list-head">
      <span v-if="ordered.length" class="dim">{{ ordered.length }} runs</span>
      <span v-else />
      <a class="spend-link" :href="costsHref()">
        <CircleDollarSign :size="17" :stroke-width="2" />
        spend
      </a>
    </div>

    <div v-if="ordered.length" class="cards">
      <SessionCard
        v-for="s in ordered"
        :key="s.adw_id"
        :session="s"
        :now-ms="nowMs"
        @archived="onArchived"
      />
    </div>
    <div v-else-if="loaded" class="empty-state">no sessions yet — run an ADW to see it here</div>
    <div v-else-if="!apiError" class="empty-state">loading sessions…</div>
  </div>
</template>

<style scoped>
.sessions {
  display: flex;
  flex-direction: column;
}

.list-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 16px 24px 0;
  font-size: 16px;
}

.spend-link {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 16px;
  color: var(--dim);
  border: 1px solid var(--border-soft);
  border-radius: 999px;
  padding: 4px 13px;
  background: rgba(19, 26, 38, 0.6);
}

.spend-link:hover {
  color: var(--text);
  border-color: var(--border);
}

.cards {
  /* Uniform grid: every card the same width and (fixed in SessionCard) height,
     independent of content. */
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(460px, 1fr));
  gap: 18px;
  padding: 16px 24px 28px;
}





</style>
