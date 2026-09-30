import { del, get, getBlob, post, put, uploadFiles } from './index.js'

export function getKbFiles() {
  return get('/kb/files')
}

export function uploadKbFiles(files) {
  return uploadFiles('/kb/upload', files)
}

export function deleteKbFile(fileId) {
  return del(`/kb/files/${fileId}`)
}

export function clearKb() {
  return del('/kb/clear')
}

export function getUploadStatus(taskId) {
  return get(`/kb/upload-status/${taskId}`)
}

// ── 切块设置（知识库级） ──
//
// 读：登录即可（与 /kb/files 同口径）；写：仅管理员（后端 require_admin）。

/** 读取当前知识库的切块配置（含默认值、合法区间与"何时生效"说明） */
export function getKbChunking() {
  return get('/kb/chunking')
}

/** 保存切块配置（仅管理员）。越界值后端返回 400，错误信息可直接展示 */
export function updateKbChunking(payload) {
  return put('/kb/chunking', payload)
}

/**
 * 重建向量索引
 *
 * 原先这里把 401 处理**复制粘贴**了一遍（和 api/index.js 里的逐字相同）。
 * 重复的代价是两份会漂移：以后改了一处忘另一处，行为就不一致了。
 * 现在走统一的 post()。
 */
export function rebuildKb() {
  return post('/kb/rebuild')
}

// ── 预览：入库切片 / 原文件内容 ──

/** 某个文件的入库切片（按 chunk_idx 升序） */
export function getKbFileChunks(fileId, { limit = 200, offset = 0 } = {}) {
  return get(`/kb/files/${fileId}/chunks`, { limit, offset })
}

/** 解析后的纯文本（含 PDF 的逐页文本） */
export function getKbFileText(fileId) {
  return get(`/kb/files/${fileId}/content`, { mode: 'text' })
}

/**
 * 原文件（二进制）。
 *
 * 走 blob 而不是直接给 `<iframe src>` 指 URL —— 那个请求带不上 Authorization
 * 头。调用方负责 `URL.createObjectURL` 与 `revokeObjectURL`。
 */
export function getKbFileBlob(fileId) {
  return getBlob(`/kb/files/${fileId}/content`, { mode: 'raw' })
}
