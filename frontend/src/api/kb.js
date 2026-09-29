import { del, get, post, uploadFiles } from './index.js'

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
