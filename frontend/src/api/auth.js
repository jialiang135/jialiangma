import { post } from './index.js'

export function login(username, password) {
  return post('/auth/login', { username, password })
}
