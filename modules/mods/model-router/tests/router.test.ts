import { expect, test } from 'claude-code/testing'
import { classify, atLeast, MODELS, pickAgentModel } from '../hooks/classify.js'

test('coding prompts go to Sonnet 5.5 at medium', async () => {
  const r = classify('fix the failing unit tests in heartbeat.py', null, true).route
  expect(r.model).toBe(MODELS.sonnet)
  expect(r.effort).toBe('medium')
})

test('quick prompts go to Haiku 5.5 at low effort when available', async () => {
  expect(classify('print my to do list', null, true).route.model).toBe(MODELS.haiku)
  const old = classify('print my to do list', null, false).route
  expect(old.model).toBe(undefined)
  expect(old.effort).toBe('low')
})

test('deep prompts keep the model and raise effort', async () => {
  const r = classify('research the tradeoffs and write a recommendation', null, true).route
  expect(r.model).toBe(undefined)
  expect(r.effort).toBe('high')
})

test('plain prompts are left alone', async () => {
  expect(classify('whens my decal field trip', null, true).route).toBe(null)
})

test('follow-ups inherit the last route', async () => {
  const last = { kind: 'code', model: MODELS.sonnet, effort: 'medium' }
  expect(classify('yes do it', last, true).route).toBe(last)
  expect(classify('ok', last, true).route).toBe(last)
})

test('overrides strip the prefix and pin the model', async () => {
  const r = classify('!opus fix this bug', null, true)
  expect(r.text).toBe('fix this bug')
  expect(r.route.model).toBe(MODELS.opus)
  expect(classify('!quick whats up', null, true).route.effort).toBe('low')
  expect(classify('!deep think hard', null, true).route.effort).toBe('xhigh')
  const auto = classify('!auto fix the bug', null, true)
  expect(auto.route).toBe(null)
  expect(auto.text).toBe('fix the bug')
})

test('version gate for Haiku 5.5', async () => {
  expect(atLeast('2.1.288', '2.1.293')).toBe(false)
  expect(atLeast('2.1.293', '2.1.293')).toBe(true)
  expect(atLeast('2.1.295-dev', '2.1.293')).toBe(true)
})

test('helper agents: searches to Haiku, edits to Sonnet, reviews inherit', async () => {
  expect(pickAgentModel('Explore', 'find the config', true).model).toBe(MODELS.haiku)
  expect(pickAgentModel('Explore', 'find the config', false).model).toBe(MODELS.sonnet)
  expect(pickAgentModel('general-purpose', 'Search repo for callers', true).model).toBe(MODELS.haiku)
  expect(pickAgentModel('general-purpose', 'Implement the band mod', true).model).toBe(MODELS.sonnet)
  expect(pickAgentModel('general-purpose', 'Review and verify the fix', true)).toBe(null)
  expect(pickAgentModel('Plan', 'plan the migration', true)).toBe(null)
  expect(pickAgentModel('fork', 'find stuff', true)).toBe(null)
})
