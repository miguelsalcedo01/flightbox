import { ref } from 'vue'

// Hash routes:
//   #/                      → sessions list
//   #/costs                 → spend dashboard
//   #/<adw_id>              → waterfall (technical trace)
//   #/<adw_id>/story        → plain-language replay
//   #/<adw_id>/<phase_id>   → waterfall with the phase panel open
// "costs" and "story" are reserved segments; adw_ids are generated hex-ish ids,
// so a collision cannot happen in practice.
export type View = 'sessions' | 'costs' | 'trace' | 'story'

export interface Route {
  view: View
  adwId: string | null
  phaseId: string | null
}

function parse(): Route {
  const parts = window.location.hash
    .replace(/^#\/?/, '')
    .split('/')
    .filter(Boolean)
    .map(decodeURIComponent)
  const first = parts[0] ?? null
  if (!first) return { view: 'sessions', adwId: null, phaseId: null }
  if (first === 'costs') return { view: 'costs', adwId: null, phaseId: null }
  if (parts[1] === 'story') return { view: 'story', adwId: first, phaseId: null }
  return { view: 'trace', adwId: first, phaseId: parts[1] ?? null }
}

const route = ref<Route>(parse())

window.addEventListener('hashchange', () => {
  route.value = parse()
})

export function useRoute() {
  return route
}

// Display name for the phase crumb — set by the trace view once phases load,
// since the phase_id in the URL is not the display name.
export const phaseCrumb = ref<string | null>(null)

export function hrefFor(adwId?: string | null, phaseId?: string | null): string {
  let h = '#/'
  if (adwId) h += encodeURIComponent(adwId)
  if (adwId && phaseId) h += `/${encodeURIComponent(phaseId)}`
  return h
}

/** The spend dashboard. */
export function costsHref(): string {
  return '#/costs'
}

/** The plain-language replay of one run. */
export function storyHref(adwId: string): string {
  return `#/${encodeURIComponent(adwId)}/story`
}

export function navigate(adwId?: string | null, phaseId?: string | null): void {
  window.location.hash = hrefFor(adwId, phaseId)
}
