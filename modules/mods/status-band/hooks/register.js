// status-band: one tidy band above the prompt, drawn the way token-weather is
// (short styled pieces in a row, no long gray strings):
//
//   ▰▰▰▰▰▰▱▱▱▱  Chat 57% full   571k of 1M   +12k last reply
//   ◷ Today   6pm Lab report draft  ·  11:59pm Club slides     +8 this week
//   ⏭ Queue 2   next: run the tests and fix…                              /q to manage
//   ⚠ 5-hour limit 84%   resets 3:40pm
//
// Rows 2 and 3 appear only when there is something to say. /q queues prompts that
// send one after another as each turn finishes; /band off hides the band.

const TODAY = '{{HOME}}/.claude/hooks/today.py'  // install.sh writes your home folder here
const SEP = '  ·  '

let deadlines = { items: [], week: 0 }
let limits = [] // [{ kind, percentUsed, resetsAt }]
let ctx = null // { tokens, window, percent, delta }
let nowMs = 0
let queue = [] // prompts waiting, oldest first
let paused = false
let bandOn = true
let storeKey = 'queue'

async function refreshDeadlines($) {
  try {
    const r = await $.process.run(['python3', TODAY, '--band'], { timeoutMs: 10000 })
    if (r.exitCode === 0) deadlines = JSON.parse(r.stdout)
  } catch {
    // keep the last good reading
  }
  $.ui.invalidate('ui.render')
}

async function refreshLimits($) {
  try {
    const u = await $.session.usage()
    limits = (u && u.rateLimits) || []
    nowMs = await $.clock.now()
    const c = u && u.context
    if (c && c.window && c.tokens > 0) {
      const percent = Math.round(c.percent != null ? c.percent : (c.tokens / c.window) * 100)
      ctx = { tokens: c.tokens, window: c.window, percent, delta: ctx ? c.tokens - ctx.tokens : 0 }
    }
  } catch {
    limits = []
  }
  $.ui.invalidate('ui.render')
}

async function saveQueue($) {
  await $.store.set(storeKey, { queue, paused })
}

const DAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

// "5am today", "5am tomorrow (Sat Oct 10)", "5am Tuesday, Oct 13"
function clock(iso, nowMs) {
  const d = new Date(iso)
  if (isNaN(d.getTime())) return ''
  let h = d.getHours()
  const m = d.getMinutes()
  const ap = h >= 12 ? 'pm' : 'am'
  h = h % 12 || 12
  const time = h + (m ? ':' + String(m).padStart(2, '0') : '') + ap
  const now = new Date(nowMs)
  const dayDiff = Math.round((new Date(d.getFullYear(), d.getMonth(), d.getDate()) - new Date(now.getFullYear(), now.getMonth(), now.getDate())) / 86400000)
  const date = MONTHS[d.getMonth()] + ' ' + d.getDate()
  if (dayDiff === 0) return time + ' today'
  if (dayDiff === 1) return time + ' tomorrow (' + DAYS[d.getDay()].slice(0, 3) + ' ' + date + ')'
  return time + ' ' + DAYS[d.getDay()] + ', ' + date
}

function label(kind) {
  if (kind === 'five_hour') return '5-hour limit'
  if (kind === 'seven_day') return 'Weekly limit'
  return String(kind).replace(/_/g, ' ') + ' limit'
}

function shorten(s, n) {
  const t = String(s).replace(/\s+/g, ' ').trim()
  return t.length > n ? t.slice(0, n - 1).trimEnd() + '…' : t
}

function short(n) {
  if (n >= 1e6) return (n / 1e6).toFixed(n % 1e6 ? 1 : 0) + 'M'
  if (n >= 1e3) return Math.round(n / 1e3) + 'k'
  return String(n)
}

// Row 0: how full this chat's memory (context window) is, in plain words
function contextRow(Box, Text) {
  if (!ctx) return null
  const filled = Math.max(0, Math.min(10, Math.round(ctx.percent / 10)))
  const color = ctx.percent >= 85 ? 'red' : ctx.percent >= 60 ? 'yellow' : 'green'
  const parts = [
    Text({ color, children: '▰'.repeat(filled) }),
    Text({ dimColor: true, children: '▱'.repeat(10 - filled) + '  ' }),
    Text({ bold: true, children: 'Chat ' + ctx.percent + '% full' }),
    Text({ dimColor: true, children: '   ' + short(ctx.tokens) + ' of ' + short(ctx.window) }),
  ]
  if (ctx.delta > 0) parts.push(Text({ dimColor: true, children: '   +' + short(ctx.delta) + ' last reply' }))
  if (ctx.percent >= 85) parts.push(Text({ color: 'red', children: '   start a fresh session soon' }))
  return Box({ flexDirection: 'row', children: parts })
}

// Row 1: next deadlines, as many as fit the width
function deadlineRow(Box, Text, columns) {
  const items = deadlines.items || []
  if (items.length === 0) return null
  const urgent = items.some((i) => i.urgent)
  const today = items.some((i) => i.today)
  const color = urgent ? 'red' : today ? 'yellow' : 'green'
  const word = urgent ? 'Due soon' : today ? 'Today' : 'Next'
  const parts = [Text({ color, bold: true, children: '◷  ' + word + '   ' })]
  let used = word.length + 6
  const budget = Math.max(30, columns - 18)
  let shown = 0
  for (const it of items) {
    const piece = it.time.length + 1 + it.title.length + (shown ? SEP.length : 0)
    if (shown > 0 && used + piece > budget) break
    if (shown > 0) parts.push(Text({ dimColor: true, children: SEP }))
    parts.push(Text({ bold: true, children: it.time + ' ' }))
    parts.push(Text({ children: it.title }))
    used += piece
    shown += 1
  }
  const more = (deadlines.week || 0) - shown
  if (more > 0) parts.push(Text({ dimColor: true, children: '     +' + more + ' this week · /todo' }))
  return Box({ flexDirection: 'row', children: parts })
}

// Row 2: the prompt queue, only when it has something
function queueRow(Box, Text, columns) {
  if (queue.length === 0) return null
  const head = paused ? '⏸  Queue paused ' : '⏭  Queue '
  return Box({
    flexDirection: 'row',
    children: [
      Text({ color: paused ? 'yellow' : 'cyan', bold: true, children: head + queue.length }),
      Text({ dimColor: true, children: '   next: ' }),
      Text({ children: shorten(queue[0], Math.max(20, columns - 45)) }),
      Text({ dimColor: true, children: paused ? '     /q resume' : '     /q to manage' }),
    ],
  })
}

// Row 3: a usage limit past 75%
function limitRow(Box, Text) {
  const hot = limits.filter((l) => l.percentUsed >= 75).sort((a, b) => b.percentUsed - a.percentUsed)[0]
  if (!hot) return null
  const parts = [Text({ color: hot.percentUsed >= 90 ? 'red' : 'yellow', bold: true, children: '⚠  ' + label(hot.kind) + ' ' + Math.round(hot.percentUsed) + '%' })]
  if (hot.resetsAt) parts.push(Text({ dimColor: true, children: '   resets ' + clock(hot.resetsAt, nowMs || hot.nowMs) }))
  return Box({ flexDirection: 'row', children: parts })
}

export function register(on) {
  on('session.start', async ($, e, next) => {
    const saved = await $.store.get('bandOn')
    if (typeof saved === 'boolean') bandOn = saved
    storeKey = 'queue:' + (await $.session.id())
    const q = await $.store.get(storeKey)
    if (q && Array.isArray(q.queue)) {
      queue = q.queue
      paused = !!q.paused
    }
    await refreshDeadlines($)
    await refreshLimits($)
    $.clock.every(10 * 60 * 1000, () => refreshDeadlines($))
    try {
      await $.command.register({ name: 'q', description: 'Queue a prompt to send after the current turn. /q alone lists; /q clear | pause | resume | drop N', argumentHint: '<prompt> | clear | pause | resume | drop N', immediate: true })
      await $.command.register({ name: 'band', description: 'Show or hide the status band above the prompt', argumentHint: 'on | off', immediate: true })
    } catch {
      // a taken name only loses that command
    }
    return next(e)
  })

  on('command.run', { command: 'band' }, async ($, e) => {
    const arg = (e.args || '').trim().toLowerCase()
    bandOn = arg === 'on' ? true : arg === 'off' ? false : !bandOn
    await $.store.set('bandOn', bandOn)
    $.ui.invalidate('ui.render')
    return { text: 'Status band ' + (bandOn ? 'on' : 'off') }
  })

  on('command.run', { command: 'q' }, async ($, e) => {
    const arg = (e.args || '').trim()
    const word = arg.toLowerCase()
    if (arg === '') {
      if (queue.length === 0) return { text: 'Queue is empty. Add one with /q <prompt>; it sends when the current turn finishes.' }
      return { text: (paused ? 'Queue (paused):\n' : 'Queue:\n') + queue.map((p, i) => '  ' + (i + 1) + '. ' + shorten(p, 100)).join('\n') }
    }
    if (word === 'clear') {
      queue = []
      paused = false
    } else if (word === 'pause') {
      paused = true
    } else if (word === 'resume') {
      paused = false
    } else if (/^drop \d+$/.test(word)) {
      queue.splice(Number(word.split(' ')[1]) - 1, 1)
    } else {
      queue.push(arg)
    }
    await saveQueue($)
    $.ui.invalidate('ui.render')
    return { text: word === 'clear' ? 'Queue cleared' : paused ? 'Queue paused (' + queue.length + ')' : 'Queued (' + queue.length + '). It sends when the current turn finishes.' }
  })

  on('turn.complete', async ($, e, next) => {
    const result = await next(e)
    if (e.agentId) return result
    await refreshLimits($)
    if (queue.length === 0 || paused) return result
    // Stop the chain when a turn didn't finish cleanly, or ended on a question for the user
    if (e.isAborted || e.reason !== 'answer' || /\?\s*$/.test(e.answer || '')) {
      paused = true
      await saveQueue($)
      $.ui.toast('Queue paused: the last turn ' + (e.isAborted ? 'was interrupted' : e.reason !== 'answer' ? 'ended on an error' : 'asked you a question') + '. /q resume to continue.')
      $.ui.invalidate('ui.render')
      return result
    }
    const text = queue.shift()
    await saveQueue($)
    $.ui.invalidate('ui.render')
    $.ui.toast('Queue: sending the next prompt' + (queue.length ? ' (' + queue.length + ' left)' : ''))
    // Not awaited: it resolves only when the new turn starts
    $.prompt.submit({ text, asUser: true })
    return result
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (!bandOn || e.props.hasSurvey) return next(e)
    const { Box, Text } = $.ui.resolve(e)
    const columns = e.props.bodyColumns || 100
    const rows = [contextRow(Box, Text), deadlineRow(Box, Text, columns), queueRow(Box, Text, columns), limitRow(Box, Text)].filter(Boolean)
    const theirs = await next(e)
    if (rows.length === 0) return theirs
    return Box({ flexDirection: 'column', paddingX: 1, children: theirs ? [...rows, theirs] : rows })
  })
}
