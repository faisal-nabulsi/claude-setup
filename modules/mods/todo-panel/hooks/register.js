// todo-panel: /todo opens a clean to-do panel (no model call): missed items,
// then each of the next 7 days with bold times, a colored class tag and short
// titles; soft (self-imposed) items are dimmed. Esc closes it.
// `/todo text` prints the plain list into the transcript instead.
// Data comes from ~/.claude/hooks/today.py (--json for the panel, --pretty for text).

import { viewRows } from './view.js'

const TODAY = '{{HOME}}/.claude/hooks/today.py'  // install.sh writes your home folder here
const PANE = 'todo-panel'
// Props without undefined values: a drawing with an unknown/undefined prop can be refused
const P = (o) => Object.fromEntries(Object.entries(o).filter(([, v]) => v !== undefined && v !== false))
// Each tag gets a stable color picked from its name, so new courses need no edit here.
const PALETTE = ['magenta', 'blue', 'yellow', 'cyan', 'green', 'red']
const colorOf = (tag) => tag === 'Other' ? 'gray' : PALETTE[[...String(tag)].reduce((h, c) => (h * 31 + c.charCodeAt(0)) >>> 0, 7) % PALETTE.length]

let data = null
let error = ''

async function load($) {
  try {
    const r = await $.process.run(['python3', TODAY, '--json'], { timeoutMs: 10000 })
    if (r.exitCode !== 0) throw new Error(r.stderr.trim().slice(0, 200))
    data = JSON.parse(r.stdout)
    error = ''
  } catch (err) {
    error = 'Could not read the tracker: ' + String(err).slice(0, 200)
  }
}

export function register(on) {
  on('session.start', async ($, e, next) => {
    try {
      await $.command.register({ name: 'todo', description: 'Open your to-do list (no model call). /todo text prints it as plain text', argumentHint: '[text]', immediate: true })
    } catch {
      await $.command.register({ name: 'todo-list', description: 'Open your to-do list (no model call)', argumentHint: '[text]', immediate: true })
    }
    return next(e)
  })

  on('command.run', { command: ['todo', 'todo-list'] }, async ($, e) => {
    if ((e.args || '').trim() === 'text') {
      try {
        const r = await $.process.run(['python3', TODAY, '--pretty'], { timeoutMs: 10000 })
        return { text: r.exitCode === 0 ? r.stdout.trim() : 'Could not read the tracker: ' + r.stderr.trim().slice(0, 300) }
      } catch (err) {
        return { text: 'Could not read the tracker: ' + String(err).slice(0, 300) }
      }
    }
    await load($)
    await $.ui.open({ id: PANE, title: 'To-do', focus: true, closeOnEscape: true })
    $.ui.invalidate('ui.render')
    return {}
  })

  on('ui.render', { component: 'Pane' }, async ($, e, next) => {
    if (e.requestId !== PANE) return next(e)
    const { Box, Text } = $.ui.resolve(e)
    if (error || !data) return Text({ color: 'red', children: error || 'Loading…' })
    const kids = []
    for (const row of viewRows(data)) {
      if (row.kind === 'header') {
        kids.push(Box({ flexDirection: 'row', columnGap: 1, children: [
          Text({ bold: true, children: 'To-do' }),
          Text({ dimColor: true, children: '· ' + row.text }),
        ] }))
      } else if (row.kind === 'section') {
        kids.push(Text({ children: ' ' }))
        kids.push(Text(P({ bold: true, color: row.missed ? 'red' : undefined, children: row.text })))
      } else if (row.kind === 'item') {
        kids.push(Box({ flexDirection: 'row', columnGap: 2, children: [
          Box({ width: 8, children: [Text(P({ bold: !row.soft, dimColor: row.soft, color: row.urgent || row.missed ? 'red' : undefined, children: row.time }))] }),
          Box({ width: 9, children: [Text(P({ color: colorOf(row.tag), dimColor: row.soft, children: row.tag }))] }),
          Text(P({ dimColor: row.soft, children: row.title })),
        ] }))
      } else if (row.kind === 'footer') {
        kids.push(Text({ children: ' ' }))
        kids.push(Text({ dimColor: true, children: row.text }))
      }
    }
    return Box({ flexDirection: 'column', paddingX: 1, children: kids })
  })
}
