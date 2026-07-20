import { get, uploadFile } from './index.js'

export function uploadTestset(file) {
  return uploadFile('/eval/testset/upload', file, 'file')
}

export function runEval(filename) {
  const token = localStorage.getItem('token')
  return fetch(`/api/eval/run?testset_filename=${encodeURIComponent(filename)}`, {
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

export function getEvalReports() {
  return get('/eval/reports')
}

export function getEvalReportDetail(reportId) {
  return get(`/eval/reports/${reportId}`)
}
