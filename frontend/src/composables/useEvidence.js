/**
 * 证据（检索来源）
 * ================
 *
 * 证据轨的数据源。后端通过 SSE 的 `evidence` 事件下发本轮回答所依据的
 * 知识库片段，结构为：
 *
 *   [{ source, score, content, chunk_idx }]
 *
 * 这里做三件事：**规范化**（后端字段缺失/类型不对时不炸）、
 * **排序**（按相关度降序，最相关的在前）、**限流**（超过上限只留前 N 条，
 * 面板不该变成一堵墙）。
 *
 * 相关度分档说明：分数来自 rerank，0~1 之间。用三档而不是连续渐变色 ——
 * 0.42 和 0.47 用颜色区分，用户根本看不出来，只有分档才读得出"这条明显更相关"。
 */
import { computed, ref, shallowRef } from 'vue'

/** 超过这个分数算"强相关" */
const HIGH = 0.5
/** 低于这个分数算"弱相关"，只是沾边 */
const LOW = 0.25

export function relevanceTone(score) {
  const n = Number(score)
  if (!Number.isFinite(n)) return 'neutral'
  if (n >= HIGH) return 'success'
  if (n < LOW) return 'neutral'
  return 'warning'
}

export function relevanceLabel(score) {
  const tone = relevanceTone(score)
  return { success: '强相关', warning: '相关', neutral: '弱相关' }[tone]
}

function normalizeOne(raw, index) {
  if (!raw || typeof raw !== 'object') return null
  const score = Number(raw.score)
  return {
    key: `${raw.source ?? 'unknown'}#${raw.chunk_idx ?? index}`,
    source: String(raw.source ?? '未知来源'),
    score: Number.isFinite(score) ? score : null,
    content: String(raw.content ?? ''),
    chunkIdx: raw.chunk_idx ?? null,
  }
}

/**
 * 把后端下发的原始证据数组规范化。
 *
 * **导出它是为了让"流式中的临时消息"和"最终落地的消息"共用同一套结构** ——
 * 否则流式期间渲染一套（原始 field 名 `chunk_idx`），落地后渲染另一套
 * （`chunkIdx`），会出现"字打完的瞬间证据样式跳一下"。
 *
 * @param {Array} list 原始数组 [{source, score, content, chunk_idx}]
 * @param {number} maxItems 上限
 */
export function normalizeEvidenceList(list, maxItems = 6) {
  return (Array.isArray(list) ? list : [])
    .map(normalizeOne)
    .filter(Boolean)
    // 相关度降序；分数缺失的排最后（而不是当作 0，那样会被误认为极不相关）
    .sort((a, b) => {
      if (a.score == null && b.score == null) return 0
      if (a.score == null) return 1
      if (b.score == null) return -1
      return b.score - a.score
    })
    .slice(0, maxItems)
}

/**
 * @param {object} opts
 * @param {number} opts.maxItems 最多保留几条证据，默认 6
 */
export function useEvidence({ maxItems = 6 } = {}) {
  const items = shallowRef([])
  const expanded = ref(new Set())

  function set(list) {
    items.value = normalizeEvidenceList(list, maxItems)
    expanded.value = new Set()
  }

  function clear() {
    items.value = []
    expanded.value = new Set()
  }

  function toggle(key) {
    const next = new Set(expanded.value)
    if (next.has(key)) next.delete(key)
    else next.add(key)
    expanded.value = next
  }

  const isExpanded = (key) => expanded.value.has(key)
  const hasEvidence = computed(() => items.value.length > 0)

  return { items, set, clear, toggle, isExpanded, hasEvidence }
}
