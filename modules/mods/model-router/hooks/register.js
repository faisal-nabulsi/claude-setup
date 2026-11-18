// model-router: picks a model and effort for each turn from what the prompt asks.
// Rules live in classify.js. Overrides: start a prompt with !opus, !sonnet,
// !haiku, !fable (switch model for that turn), !deep / !quick (effort only),
// or !auto (no routing). /route shows status; /route off|on toggles it.
// Helper agents get a model by task (see pickAgentModel) unless the call names one.

import { classify, atLeast, label, pickAgentModel } from './classify.js'

let enabled = true
let haikuOk = false
let pending = null // route chosen at prompt.submit, waiting for its turn
let current = null // { turnId, route } for the turn in progress
let last = null // previous turn's route, for follow-ups like "yes do it"
let counts = {}
const NAMES = { opus: 'Opus 5.5', sonnet: 'Sonnet 5.5', haiku: 'Haiku 5.5', fable: 'Fable 5.1' }
const BIG_CONTEXT = 150000

export function register(on) {
  on('session.start', async ($, e, next) => {
    const saved = await $.store.get('enabled')
    if (typeof saved === 'boolean') enabled = saved
    counts = (await $.store.get('counts')) || {}
    const v = await $.session.version()
    haikuOk = atLeast(v.base || v.version, '2.1.293')
    try {
      await $.command.register({ name: 'route', description: 'Model router: status, or /route on|off', argumentHint: '[on|off]', immediate: true })
    } catch {
      // name taken; the router still works without its command
    }
    return next(e)
  })

  on('command.run', { command: 'route' }, async ($, e) => {
    const arg = (e.args || '').trim().toLowerCase()
    if (arg === 'on' || arg === 'off') {
      enabled = arg === 'on'
      await $.store.set('enabled', enabled)
    }
    const tally = Object.entries(counts).map(([k, v]) => k + ' ' + v).join(', ') || 'none yet'
    return {
      text: 'Model router ' + (enabled ? 'ON' : 'OFF') + '. Haiku 5.5 routing: ' + (haikuOk ? 'on' : 'off (update the app to 2.1.293+)') +
        '. Routed turns so far: ' + tally + '. Overrides: !opus !sonnet !haiku !fable !deep !quick !auto',
    }
  })

  on('prompt.submit', async ($, e, next) => {
    // A prompt delivered into a running turn doesn't start a new one: leave it
    if (e.turnId) return next(e)
    const { text, route } = classify(e.text, last, haikuOk)
    let r = enabled || text !== e.text ? route : null
    // In a long chat a model switch re-reads everything uncached, so past
    // BIG_CONTEXT tokens only effort changes (an explicit !model still switches)
    if (r && r.model && r.kind !== 'pinned') {
      const u = await $.session.usage()
      // A coding turn would only have dropped Opus to Sonnet's effort, so keep the session's own setting;
      // quick turns still go to low effort, which keeps the cache
      if (u && u.context && u.context.tokens > BIG_CONTEXT) r = r.kind === 'quick' && r.effort ? { kind: r.kind, effort: r.effort } : null
    }
    pending = r
    return text === e.text ? next(e) : next({ ...e, text })
  }).catch(async ($, e, next) => {
    // If routing fails, send the prompt exactly as typed
    pending = null
    return next(e)
  })

  on('turn.start', async ($, e, next) => {
    current = pending ? { turnId: e.turnId, route: pending } : null
    last = pending
    pending = null
    return next(e)
  })

  on('turn.step', async function* ($, e, next) {
    if (e.agentId || !current || current.turnId !== e.turnId) return yield* next(e)
    const r = current.route
    const patch = {}
    if (r.model && !e.model.startsWith(r.model)) patch.model = r.model
    if (r.effort && r.effort !== e.effort) patch.effort = r.effort
    if (patch.model || patch.effort) {
      if (e.index === 0) {
        counts[r.kind] = (counts[r.kind] || 0) + 1
        await $.store.set('counts', counts)
        const who = patch.model ? NAMES[label(patch.model)] || label(patch.model) : 'Opus, same model'
        const why = { code: 'coding', quick: 'quick question', deep: 'needs deeper thinking', pinned: 'you asked' }[r.kind] || r.kind
        $.ui.status('This reply: ' + who + (patch.effort ? ', ' + patch.effort + ' effort' : '') + ' (' + why + ')')
      }
      return yield* next({ ...e, ...patch })
    }
    return yield* next(e)
  })

  // Helper agents: choose a model only when the Agent call named none
  on('agent.spawn', async ($, e, next) => {
    if (!enabled || e.model) return next(e)
    const pick = pickAgentModel(e.subagentType, e.description, haikuOk)
    if (!pick) return next(e)
    counts[pick.kind] = (counts[pick.kind] || 0) + 1
    await $.store.set('counts', counts)
    return next({ ...e, model: pick.model })
  }).catch(async ($, e, next) => next(e))

  on('turn.complete', async ($, e, next) => {
    if (!e.agentId && current && current.turnId === e.turnId) {
      current = null
      $.ui.status(undefined) // the note is only for the reply it describes
    }
    return next(e)
  })
}
