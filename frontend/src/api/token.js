import { get } from './index.js'

/**
 * 获取 Token 使用统计
 */
export function getTokenStats(days = 7) {
  return get('/token/stats', { days })
}
