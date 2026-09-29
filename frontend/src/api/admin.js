/**
 * 管理员面板 API 模块
 */
import { del, get, put } from './index.js'

/** 获取全局仪表盘数据 */
export function getDashboard() {
  return get('/admin/dashboard')
}

/** 获取所有用户列表 */
export function getUsers() {
  return get('/admin/users')
}

/**
 * 修改用户角色
 *
 * 原先是手写 `fetch`，漏了 401 处理 —— 登录态过期时会把错误响应体当成
 * 正常结果 `r.json()` 解析，用户看到的是"修改失败"而不是"请重新登录"。
 * 现在统一走 put()。
 */
export function updateUserRole(userId, role) {
  return put(`/admin/users/${userId}/role`, { role })
}

/** 删除用户（同上，原手写 fetch 漏 401） */
export function deleteUser(userId) {
  return del(`/admin/users/${userId}`)
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
