import assert from 'node:assert/strict'
import { afterEach, test } from 'node:test'
import { createPinia, setActivePinia } from 'pinia'

import { useAuthStore } from '../src/stores/auth.js'

const originalStorage = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')

function installStorage(initial) {
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

afterEach(() => {
  if (originalStorage) Object.defineProperty(globalThis, 'localStorage', originalStorage)
  else delete globalThis.localStorage
})

test('corrupt persisted user data clears the stale token instead of crashing startup', () => {
  const storage = installStorage({ token: 'stale-token', user: '{not-json' })
  setActivePinia(createPinia())

  const auth = useAuthStore()

  assert.equal(auth.isLoggedIn, false)
  assert.equal(auth.user, null)
  assert.equal(storage.getItem('token'), null)
  assert.equal(storage.getItem('user'), null)
})

test('a valid persisted session restores role and username', () => {
  installStorage({
    token: 'signed-token',
    user: JSON.stringify({ username: 'admin', owner_id: 1, role: 'admin' }),
  })
  setActivePinia(createPinia())

  const auth = useAuthStore()

  assert.equal(auth.isLoggedIn, true)
  assert.equal(auth.username, 'admin')
  assert.equal(auth.isAdmin, true)
})
