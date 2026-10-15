import assert from 'node:assert/strict'
import { afterEach, test } from 'node:test'

import { get } from '../src/api/index.js'
import { streamChat } from '../src/api/chat.js'

const originalFetch = globalThis.fetch
const originalStorage = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')
const originalWindow = Object.getOwnPropertyDescriptor(globalThis, 'window')
const originalCustomEvent = Object.getOwnPropertyDescriptor(globalThis, 'CustomEvent')

function installStorage(initial = {}) {
  const values = new Map(Object.entries(initial))
  const storage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: (key) => values.delete(key),
  }
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: storage,
  })
  return storage
}

function restoreGlobal(name, descriptor) {
  if (descriptor) Object.defineProperty(globalThis, name, descriptor)
  else delete globalThis[name]
}

afterEach(() => {
  globalThis.fetch = originalFetch
  restoreGlobal('localStorage', originalStorage)
  restoreGlobal('window', originalWindow)
  restoreGlobal('CustomEvent', originalCustomEvent)
})

test('streamChat flushes a final SSE event without a trailing newline', async () => {
  const storage = installStorage({ token: 'test-token' })
  const answerChunks = []
  let closeCount = 0
  let request
  const payload = [
    'data: {"type":"answer","content":"partial"}',
    'data: {"type":"done","content":"{\\"conversation_id\\":\\"c-1\\",\\"answer\\":\\"final answer\\"}"}',
  ].join('\n')

  globalThis.fetch = async (url, options) => {
    request = { url, options }
    return new Response(payload, {
      status: 200,
      headers: { 'Content-Type': 'text/event-stream' },
    })
  }

  const result = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('SSE stream timed out')), 1500)
    streamChat(
      'question',
      {
        onAnswer: (chunk) => answerChunks.push(chunk),
        onDone: (conversationId, answer) => {
          clearTimeout(timer)
          resolve({ conversationId, answer })
        },
        onClose: () => {
          closeCount += 1
        },
        onError: reject,
      },
      'existing-conversation',
    )
  })

  await new Promise((resolve) => setImmediate(resolve))
  assert.deepEqual(result, { conversationId: 'c-1', answer: 'final answer' })
  assert.deepEqual(answerChunks, ['partial'])
  assert.equal(closeCount, 0)
  assert.equal(request.url, '/api/chat/stream')
  assert.equal(request.options.headers.Authorization, 'Bearer test-token')
  assert.deepEqual(JSON.parse(request.options.body), {
    message: 'question',
    agent_mode: 'chat',
    conversation_id: 'existing-conversation',
  })
  assert.equal(storage.getItem('token'), 'test-token')
})

test('HTTP errors are rejected with status and backend detail', async () => {
  installStorage()
  globalThis.fetch = async () =>
    new Response(JSON.stringify({ detail: 'service unavailable' }), {
      status: 503,
      headers: { 'Content-Type': 'application/json' },
    })

  await assert.rejects(get('/health'), (error) => {
    assert.equal(error.status, 503)
    assert.equal(error.message, 'service unavailable')
    return true
  })
})

test('401 clears persisted credentials and broadcasts auth-expired', async () => {
  const storage = installStorage({ token: 'expired-token', user: '{"username":"alice"}' })
  const events = []
  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    value: { dispatchEvent: (event) => events.push(event.type) },
  })
  globalThis.fetch = async () =>
    new Response(JSON.stringify({ detail: 'expired' }), {
      status: 401,
      headers: { 'Content-Type': 'application/json' },
    })

  await assert.rejects(get('/private'), (error) => error.status === 401)
  assert.equal(storage.getItem('token'), null)
  assert.equal(storage.getItem('user'), null)
  assert.deepEqual(events, ['auth-expired'])
})
