import { get, post } from './index.js'

export function login(username, password) {
  return post('/auth/login', { username, password })
}

export function register(username, password) {
  return post('/auth/register', { username, password })
}

/**
 * 取注册/登录页要展示的校验规则。
 *
 * 这是**公开端点**，登录页在未登录状态下就能拿到 —— 所以注册页的实时校验清单
 * 是照着后端给的规则渲染的，而不是前端自己写一份。后端改规则，前端自动跟。
 */
export function getAuthRequirements() {
  return get('/auth/requirements')
}
