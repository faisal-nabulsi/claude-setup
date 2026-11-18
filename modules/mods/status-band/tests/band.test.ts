import { expect, test } from 'claude-code/testing'

test('/q queues, lists, pauses and clears', async ($, on) => {
  on('store.set', () => ({ value: undefined }))
  let r = await $.command.run({ command: 'q', args: 'run the tests' })
  expect(r.text).toContain('Queued (1)')
  r = await $.command.run({ command: 'q', args: '' })
  expect(r.text).toContain('1. run the tests')
  r = await $.command.run({ command: 'q', args: 'pause' })
  expect(r.text).toContain('paused')
  r = await $.command.run({ command: 'q', args: 'clear' })
  expect(r.text).toBe('Queue cleared')
  r = await $.command.run({ command: 'q', args: '' })
  expect(r.text).toContain('empty')
})

test('/band toggles', async ($, on) => {
  on('store.set', () => ({ value: undefined }))
  const r = await $.command.run({ command: 'band', args: 'off' })
  expect(r.text).toBe('Status band off')
})
