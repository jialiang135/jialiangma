import { get, post, del } from './index.js'

/** 列出可用评测集与指标 */
export function getEvalTestsets() {
  return get('/eval/testsets')
}

/**
 * 提交一次评测任务（异步）。
 * @param {{testset: string, metrics?: string[], sample_limit?: number}} payload
 */
export function runEval(payload) {
  return post('/eval/run', payload)
}

/** 列出评测报告（不含逐题明细） */
export function getEvalReports(limit = 20) {
  return get('/eval/reports', { limit })
}

/** 单次评测详情（含逐题明细、指标、建议） */
export function getEvalReport(reportId) {
  return get(`/eval/reports/${reportId}`)
}

export function deleteEvalReport(reportId) {
  return del(`/eval/reports/${reportId}`)
}
