import { expect, test } from 'claude-code/testing'
import { viewRows } from '../hooks/view.js'

const sample = {
  now: 'Fri Oct 9 · 1:40pm',
  missed: [{ time: '1pm', title: 'History class', tag: 'HIST', soft: false, urgent: false }],
  days: [{ label: 'Today · Oct 9', items: [{ time: '6pm', title: 'Lab write-up', tag: 'BIO', soft: false, urgent: false }, { time: '7pm', title: 'Journal 6/14', tag: 'HIST', soft: true, urgent: false }] }],
  later: 4,
}

test('panel rows: header, missed, day sections, footer', async () => {
  const rows = viewRows(sample)
  expect(rows[0].kind).toBe('header')
  expect(rows[1].text).toBe('Missed? Not marked done')
  expect(rows[2].missed).toBe(true)
  expect(rows[3].text).toBe('TODAY · OCT 9')
  expect(rows.filter((r) => r.kind === 'item').length).toBe(3)
  expect(rows[rows.length - 1].text).toContain('+4 more')
})

test('empty week says so', async () => {
  const rows = viewRows({ now: 'x', missed: [], days: [], later: 0 })
  expect(rows.some((r) => r.text === 'Nothing in the next 7 days')).toBe(true)
})

test('/todo text prints the script output', async ($, on) => {
  on('process.run', () => ({ value: { exitCode: 0, stdout: 'TODAY: test list\n', stderr: '' } }))
  const r = await $.command.run({ command: 'todo', args: 'text' })
  expect(r.text).toBe('TODAY: test list')
})

const SAMPLE_JSON = JSON.stringify(sample)
for (const surface of ['terminal', 'desktop'] as const) {
  test('panel draws on ' + surface, async ($, on) => {
    on('process.run', () => ({ value: { exitCode: 0, stdout: SAMPLE_JSON, stderr: '' } }))
    on('ui.open', () => ({ value: { isPlaced: true } }))
    on('ui.invalidate', () => ({ value: undefined }))
    on('ui.render', async ($, e) => { const { Text } = $.ui.resolve(e); return Text({ children: 'engine' }) })
    await $.command.run({ command: 'todo', args: '' })
    const ui = await $.ui.mount({ plugin: 'todo-panel', surface, component: 'Pane', requestId: 'todo-panel', props: { title: 'To-do', isFocused: true, bodyColumns: 90, placement: 'dock' } } as any)
    expect(await ui.find({ type: 'Text', text: /Lab write-up/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /TODAY · OCT 9/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /engine/ })).toBeUndefined()
    await ui.unmount()
  })
}
