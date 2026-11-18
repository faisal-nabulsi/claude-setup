// Pure routing rules for model-router, kept separate so tests can import them.
//
// Why routes look like this:
// - Changing EFFORT keeps the prompt cache, so it is the cheap lever: quick turns
//   drop to low effort, deep turns rise to high.
// - Changing the MODEL makes the new model re-read the whole conversation once
//   (its own cache starts cold). So the router only switches model when the gain
//   is clear: coding turns go to Sonnet 5.5 (it leads Opus 5.5 on Terminal-Bench
//   4.0 and costs less), and quick turns go to Haiku 5.5 (1M context, has effort).
// - Short follow-ups ("yes", "do it") inherit the previous turn's route, so a
//   coding thread stays on one model instead of bouncing.

export const MODELS = {
  opus: 'claude-opus-5-5',
  sonnet: 'claude-sonnet-5-5',
  haiku: 'claude-haiku-5-5',
  fable: 'claude-fable-5-1',
}

const OVERRIDE = /^\s*!(opus|sonnet|haiku|fable|deep|quick|auto)\b[:\s]*/i
const FOLLOWUP = /^\s*((y|yes|yeah|yep|ya|ok|okay|sure|do it|go|go ahead|continue|keep going|proceed|next|try again|again|sounds good|perfect|bet|run it|ship it|please|pls)\b[\s,.!]*)+$/i
const QUICK = /^\s*((print|show|list|what('?s| is)|whats)\b.{0,30}\b(to ?-?do|todo|due|deadlines?|list|time|date|schedule)\b|(thanks|thank you|ty|cool|nice|lol|got it)\b)/i
const CODE = /\b(debug|bug|stack ?trace|traceback|exception|compile|compiler|refactor|unit tests?|failing tests?|tests? (fail|pass)|typescript|javascript|swift(ui)?|xcode|python script|regex|sql|wrangler|npm|pip|git (rebase|merge|conflict)|implement|function|endpoint|api route|lint|type ?error|segfault|build (fails|failed|error))\b/i
const DEEP = /\b(essay|research|analy[sz]e|analysis|strategy|trade-?offs?|architecture|design doc|prove|proof|recommend|recommendation|plan out|deep dive|thorough|in depth)\b/i

// Returns { text, route } where route is null (leave the request alone) or
// { kind, model?, effort? }. `last` is the previous turn's route, `haikuOk`
// says whether this Claude Code build knows Haiku 5.5 (2.1.293+).
export function classify(text, last, haikuOk) {
  const m = text.match(OVERRIDE)
  if (m) {
    const word = m[1].toLowerCase()
    const rest = text.slice(m[0].length)
    if (word === 'auto') return { text: rest, route: null }
    if (word === 'deep') return { text: rest, route: { kind: 'deep', effort: 'xhigh' } }
    if (word === 'quick') return { text: rest, route: { kind: 'quick', effort: 'low' } }
    if (word === 'haiku' && !haikuOk) return { text: rest, route: { kind: 'quick', effort: 'low' } }
    return { text: rest, route: { kind: 'pinned', model: MODELS[word] } }
  }
  if (FOLLOWUP.test(text) && last) return { text, route: last }
  if (text.length <= 80 && QUICK.test(text)) {
    return { text, route: haikuOk ? { kind: 'quick', model: MODELS.haiku, effort: 'low' } : { kind: 'quick', effort: 'low' } }
  }
  if (CODE.test(text)) return { text, route: { kind: 'code', model: MODELS.sonnet, effort: 'medium' } }
  if (DEEP.test(text)) return { text, route: { kind: 'deep', effort: 'high' } }
  return { text, route: null }
}

// "2.1.293" >= "2.1.293"
export function atLeast(version, min) {
  const a = String(version || '0').split('.').map((x) => parseInt(x, 10) || 0)
  const b = min.split('.').map(Number)
  for (let i = 0; i < 3; i++) {
    if ((a[i] || 0) !== b[i]) return (a[i] || 0) > b[i]
  }
  return true
}

export function label(model) {
  const hit = Object.entries(MODELS).find(([, id]) => model && model.startsWith(id))
  return hit ? hit[0] : model || 'session model'
}

// Helper agents (subagents): pick a model when the caller named none.
// Searches and lookups -> Haiku 5.5 (Sonnet before 2.1.293); edits/builds ->
// Sonnet 5.5; planning, review, research and anything unrecognised -> left to
// inherit (Opus). A subagent starts with a fresh context, so switching its model
// costs no cache re-read of the main chat.
const AGENT_SEARCH = /\b(search|find|locate|look ?up|grep|scan|list|explore|where is|which files?)\b/i
const AGENT_EDIT = /\b(implement|edit|fix|refactor|write|build|add|update|migrate|rename|convert|patch)\b/i
const AGENT_KEEP = /\b(plan|review|verify|audit|research|design|security|analy[sz]e|judge|critic|refute)\b/i

export function pickAgentModel(subagentType, description, haikuOk) {
  const type = String(subagentType || '')
  const text = String(description || '')
  if (type === 'fork') return null
  if (type === 'Explore' || (type === 'general-purpose' && AGENT_SEARCH.test(text) && !AGENT_KEEP.test(text))) {
    return { kind: 'agent-search', model: haikuOk ? MODELS.haiku : MODELS.sonnet }
  }
  if (type === 'general-purpose' && AGENT_EDIT.test(text) && !AGENT_KEEP.test(text)) {
    return { kind: 'agent-edit', model: MODELS.sonnet }
  }
  return null
}
