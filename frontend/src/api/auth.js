import { post } from './index.js'

export function login(username, password) {
  return post('/auth/login', { username, password })
}

export function register(username, password) {
  return post('/auth/register', { username, password })
}
