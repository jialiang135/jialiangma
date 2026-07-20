import { get, del, uploadFiles } from './index.js'

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

export function rebuildKb() {
  const token = localStorage.getItem('token')
  return fetch('/api/kb/rebuild', {
    method: 'POST',
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  }).then(r => {
    if (r.status === 401) {
      localStorage.removeItem('token')
      localStorage.removeItem('user')
      window.dispatchEvent(new CustomEvent('auth-expired'))
      throw new Error('登录已过期，请重新登录')
    }
    return r.json()
  })
}
