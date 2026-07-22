/**
 * 管理员面板 API 模块
 */
import { get, post, del } from './index.js'

const BASE = '/api/admin'

/** 获取全局仪表盘数据 */
export function getDashboard() {
  return get('/admin/dashboard')
}

/** 获取所有用户列表 */
export function getUsers() {
  return get('/admin/users')
}

/** 修改用户角色 */
export function updateUserRole(userId, role) {
  const token = localStorage.getItem('token')
  return fetch(`${BASE}/users/${userId}/role`, {
    method: 'PUT',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify({ role }),
  }).then(r => r.json())
}

/** 删除用户 */
export function deleteUser(userId) {
  const token = localStorage.getItem('token')
  return fetch(`${BASE}/users/${userId}`, {
    method: 'DELETE',
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  }).then(r => r.json())
}

/** 查询所有对话日志 */
export function getChatLogs(limit = 50, offset = 0, username = '') {
  const params = new URLSearchParams({ limit, offset })
  if (username) params.set('username', username)
  return get(`/admin/chat-logs?${params}`)
}

/** 查询所有文件 */
export function getFiles(limit = 50, offset = 0, username = '') {
  const params = new URLSearchParams({ limit, offset })
  if (username) params.set('username', username)
  return get(`/admin/files?${params}`)
}

/** 查询审计日志 */
export function getAuditLogs(limit = 50, action = '', username = '') {
  const params = new URLSearchParams({ limit })
  if (action) params.set('action', action)
  if (username) params.set('username', username)
  return get(`/admin/audit-logs?${params}`)
}

/** 熔断器状态 */
export function getCircuitStatus() {
  return get('/admin/circuit-status')
}

/** 异步队列状态 */
export function getQueueStatus() {
  return get('/admin/queue-status')
}
