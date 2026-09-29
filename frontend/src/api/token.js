import { get } from './index.js'

/**
 * 获取 Token 使用统计
 *
 * @param {number} days 统计天数
 * @param {'me'|'all'} scope `all` = 全部用户（仅管理员，普通用户会 403）
 */
export function getTokenStats(days = 7, scope = 'me') {
  return get('/token/stats', { days, scope })
}
