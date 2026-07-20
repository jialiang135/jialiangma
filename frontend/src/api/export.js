/**
 * 导出功能 API 模块
 * 提供对话记录 & 评测报告的导出下载
 */

const BASE = '/api'

/**
 * 通用文件下载工具
 * 通过 fetch 获取 blob，利用 Content-Disposition 文件名触发浏览器下载
 * @param {string} url 完整的 API URL（含查询参数）
 * @returns {Promise<void>}
 */
async function downloadAsFile(url) {
  const token = localStorage.getItem('token')

  const res = await fetch(url, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  })

  if (res.status === 401) {
    localStorage.removeItem('token')
    localStorage.removeItem('user')
    window.dispatchEvent(new CustomEvent('auth-expired'))
    throw new Error('登录已过期，请重新登录')
  }

  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`下载失败 (${res.status}): ${text.slice(0, 200)}`)
  }

  // 从 Content-Disposition 头中提取文件名
  const disposition = res.headers.get('Content-Disposition') || ''
  const match = disposition.match(/filename[^;=\n]*=["']?([^"';\n]*)["']?/)
  const filename = match ? match[1] : `export_${Date.now()}`

  const blob = await res.blob()
  const blobUrl = URL.createObjectURL(blob)

  const anchor = document.createElement('a')
  anchor.href = blobUrl
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)

  // 延迟释放 blob URL 以确保下载开始
  setTimeout(() => URL.revokeObjectURL(blobUrl), 10000)
}

/**
 * 导出对话记录为 Markdown 文件
 * @param {string} [conversationId='all'] 对话ID 或 'all'
 * @param {number} [limit=50] 最大记录数
 */
export function exportChatMarkdown(conversationId = 'all', limit = 50) {
  const params = new URLSearchParams({ conversation_id: conversationId, limit })
  return downloadAsFile(`${BASE}/export/chat/markdown?${params}`)
}

/**
 * 导出对话记录为 JSON 文件
 * @param {number} [limit=50] 最大记录数
 */
export function exportChatJson(limit = 50) {
  const params = new URLSearchParams({ limit })
  return downloadAsFile(`${BASE}/export/chat/json?${params}`)
}

/**
 * 导出评测报告为 Markdown 文件
 * @param {number} reportId 报告 ID
 */
export function exportEvalReportMarkdown(reportId) {
  return downloadAsFile(`${BASE}/export/eval/report/${reportId}/markdown`)
}
