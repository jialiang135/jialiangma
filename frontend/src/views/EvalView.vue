<template>
  <div class="page">
    <!-- ══ 发起评测（仅管理员） ══ -->
    <UiCard v-if="auth.isAdmin">
      <template #header>
        <div class="panel-title">
          <UiIcon name="activity" :size="18" />
          <h2>发起评测</h2>
        </div>
      </template>

      <UiAlert tone="info" class="run-hint">
        评测会对评测集中的每条问题跑一次完整问答，再用 RAGAS 逐条判定质量。
        <strong>很慢</strong>（每条约 10~30 秒），因此是后台任务，可随时离开本页。
      </UiAlert>

      <div class="run-form">
        <div class="field">
          <span class="field-label">评测集</span>
          <UiSelect v-model="form.testset" :options="testsetOptions" :disabled="running" />
        </div>

        <div class="field field-sm">
          <span class="field-label">样本数</span>
          <UiInput v-model="sampleLimit" type="number" size="md" :disabled="running" />
        </div>

        <div class="field field-wide">
          <span class="field-label">指标</span>
          <div class="pick-row">
            <button
              v-for="m in metrics"
              :key="m"
              type="button"
              class="pick-chip"
              :class="{ 'is-on': form.metrics.includes(m) }"
              :aria-pressed="form.metrics.includes(m)"
              :disabled="running"
              @click="toggleMetric(m)"
            >
              <UiIcon name="check" :size="14" class="pick-check" />
              <span>{{ metricLabel(m) }}</span>
            </button>
          </div>
        </div>
      </div>

      <template #footer>
        <div class="run-actions">
          <UiButton
            variant="primary"
            :loading="running"
            :disabled="running || !form.metrics.length"
            @click="doRun"
          >
            <template #icon><UiIcon name="zap" :size="16" /></template>
            {{ running ? '评测中' : '开始评测' }}
          </UiButton>
          <span v-if="!form.metrics.length" class="run-note">至少选择一个指标</span>
        </div>
      </template>
    </UiCard>

    <!-- ══ 进行中的评测 ══ -->
    <UiCard v-if="activeReport" class="live-card">
      <div class="live-head">
        <UiSpinner v-if="isLive" :size="16" />
        <UiIcon v-else name="clock" :size="16" />
        <h3 class="live-title">评测进行中</h3>
        <span class="live-id">报告 #{{ activeReport.id }}</span>
        <UiBadge :tone="statusTone(activeReport.status)" :dot="isLive">
          {{ statusLabel(activeReport.status) }}
        </UiBadge>
      </div>

      <UiProgress
        :value="pct"
        :tone="progressTone(activeReport.status)"
        :active="isLive"
        label="评测进度"
      />

      <div class="live-meta">
        <span class="live-percent">{{ pct }}%</span>
        <span>评测集 {{ activeReport.testset_name || '—' }}</span>
        <span>
          已完成 {{ activeReport.completed ?? 0 }}/{{ activeReport.total_questions || '?' }}
        </span>
      </div>
    </UiCard>

    <!-- ══ 历史报告 ══ -->
    <UiCard :padded="false">
      <template #header>
        <div class="panel-title">
          <UiIcon name="list" :size="18" />
          <h2>历史报告</h2>
        </div>
        <UiButton size="sm" variant="secondary" :loading="loading" @click="loadReports">
          <template #icon><UiIcon name="refresh" :size="15" /></template>
          刷新
        </UiButton>
      </template>

      <UiTable
        :columns="reportColumns"
        :rows="reports"
        row-key="id"
        :loading="loading"
        empty-title="还没有评测记录"
        empty-description="发起一次评测后，报告会出现在这里。"
        empty-icon="chart"
      >
        <template #cell-id="{ row }">
          <span class="mono">#{{ row.id }}</span>
          <UiIcon
            v-if="detail && detail.id === row.id"
            name="chevron-right"
            :size="14"
            class="open-mark"
            label="当前展开"
          />
        </template>

        <template #cell-status="{ row }">
          <UiBadge :tone="statusTone(row.status)" :dot="row.status === 'running'">
            {{ statusLabel(row.status) }}
          </UiBadge>
        </template>

        <template #cell-progress="{ row }">
          <span class="mono">{{ row.completed ?? 0 }}/{{ row.total_questions ?? '?' }}</span>
        </template>

        <template #cell-faithfulness="{ row }">
          <span class="mono score-inline">{{ scoreOf(row, 'faithfulness').text }}</span>
        </template>

        <template #cell-honesty="{ row }">
          <span class="mono score-inline">{{ scoreOf(row, 'honesty_rate').text }}</span>
        </template>

        <template #cell-duration="{ row }">
          {{ fmtDuration(row.duration_seconds) }}
        </template>

        <template #cell-created_at="{ row }">
          <span class="muted">{{ formatDateTime(row.created_at) || '—' }}</span>
        </template>

        <template #cell-actions="{ row }">
          <div class="row-actions">
            <UiButton size="sm" variant="secondary" @click="showDetail(row.id)">详情</UiButton>
            <UiButton
              v-if="auth.isAdmin"
              size="sm"
              variant="danger"
              @click="askDelete(row.id)"
            >
              删除
            </UiButton>
          </div>
        </template>
      </UiTable>
    </UiCard>

    <!-- ══ 报告详情 ══ -->
    <section v-if="detail" ref="detailEl" class="detail">
      <UiCard>
        <template #header>
          <div class="panel-title">
            <UiIcon name="search" :size="18" />
            <h2>报告 #{{ detail.id }}</h2>
            <span class="panel-sub">{{ detail.testset_name }}</span>
          </div>
          <UiButton size="sm" variant="ghost" @click="detail = null">
            <template #icon><UiIcon name="close" :size="15" /></template>
            收起
          </UiButton>
        </template>

        <UiAlert v-if="detail.error" tone="danger" class="detail-error">
          {{ detail.error }}
        </UiAlert>

        <!-- 指标：本页主角 -->
        <div v-if="detailScores.length" class="score-grid">
          <div
            v-for="s in detailScores"
            :key="s.key"
            class="score-card"
            :class="s.tone ? `tone-${s.tone}` : ''"
          >
            <div class="score-value">{{ s.text }}</div>
            <div class="score-label">{{ s.label }}</div>
            <div class="score-bar">
              <span :style="{ width: s.percent + '%' }" />
            </div>
          </div>
        </div>
        <p v-else class="muted detail-none">本次评测没有可展示的指标。</p>

        <!-- 改进建议 -->
        <div v-if="detailRecommendations.length" class="block">
          <h3 class="block-title">
            <UiIcon name="zap" :size="16" />
            改进建议
          </h3>
          <ol class="rec-list">
            <li v-for="(r, i) in detailRecommendations" :key="i">{{ r }}</li>
          </ol>
        </div>

        <!-- 逐题明细 -->
        <div class="block">
          <h3 class="block-title">
            <UiIcon name="message" :size="16" />
            逐题明细
            <span class="block-count">{{ detailResults.length }} 题</span>
          </h3>
          <UiTable
            :columns="resultColumns"
            :rows="detailResults"
            row-key="__rowKey"
            empty-title="没有逐题明细"
            empty-icon="file"
          >
            <template #cell-category="{ row }">
              <UiBadge tone="neutral">{{ row.category || '—' }}</UiBadge>
            </template>

            <template #cell-question="{ row }">
              <p class="long-text">{{ row.question || '—' }}</p>
            </template>

            <template #cell-answer="{ row }">
              <p class="long-text">{{ row.answer || '—' }}</p>
            </template>

            <template #cell-refused="{ row }">
              <UiBadge :tone="row.refused ? 'success' : 'neutral'">
                {{ row.refused ? '是' : '否' }}
              </UiBadge>
            </template>
          </UiTable>
        </div>
      </UiCard>
    </section>

    <!-- ══ 删除确认 ══ -->
    <UiModal
      :open="deleteTarget != null"
      size="sm"
      title="删除评测报告"
      :close-on-overlay="false"
      @close="deleteTarget = null"
    >
      <p class="confirm-text">
        确定删除评测报告 #{{ deleteTarget }}？报告及其逐题明细都会一并移除，此操作不可撤销。
      </p>
      <template #footer>
        <UiButton variant="ghost" :disabled="deleting" @click="deleteTarget = null">取消</UiButton>
        <UiButton variant="danger" :loading="deleting" @click="confirmDelete">删除</UiButton>
      </template>
    </UiModal>
  </div>
</template>

<script setup>
import { ref, computed, nextTick, onMounted } from 'vue'
import {
  getEvalTestsets, runEval, getEvalReports, getEvalReport, deleteEvalReport,
} from '../api/eval.js'
import { useAuthStore } from '../stores/auth.js'
import { useToast } from '../composables/useToast.js'
import { usePolling } from '../composables/usePolling.js'
import { useAsyncAction } from '../composables/useAsyncData.js'
import { formatDateTime } from '../utils/format.js'
import UiCard from '../components/ui/UiCard.vue'
import UiButton from '../components/ui/UiButton.vue'
import UiBadge from '../components/ui/UiBadge.vue'
import UiAlert from '../components/ui/UiAlert.vue'
import UiTable from '../components/ui/UiTable.vue'
import UiModal from '../components/ui/UiModal.vue'
import UiProgress from '../components/ui/UiProgress.vue'
import UiInput from '../components/ui/UiInput.vue'
import UiSelect from '../components/ui/UiSelect.vue'
import UiIcon from '../components/ui/UiIcon.vue'
import UiSpinner from '../components/ui/UiSpinner.vue'

const auth = useAuthStore()

/** 评测状态 → 进度条色调（与知识库页同一套映射，走同一个组件） */
function progressTone(status) {
  if (status === 'done') return 'success'
  if (status === 'failed') return 'danger'
  if (status === 'skipped') return 'neutral'
  return 'accent'
}
const toast = useToast()
const polling = usePolling()

/* ── 状态 ── */
const testsets = ref([])
const metrics = ref([])
const reports = ref([])
const detail = ref(null)
const detailEl = ref(null)
const loading = ref(false)
const running = ref(false)
const activeReport = ref(null)
const deleteTarget = ref(null)

const form = ref({ testset: '', sampleLimit: 5, metrics: [] })

/** 样本数：UiInput 只回传字符串，这里统一收敛成数字 */
const sampleLimit = computed({
  get: () => form.value.sampleLimit,
  set: (v) => {
    const n = Number(v)
    form.value.sampleLimit = Number.isFinite(n) ? n : v
  },
})

/* ── 展示用的纯函数 ── */

const METRIC_LABELS = {
  faithfulness: '忠实度',
  answer_relevancy: '答案相关度',
  context_precision: '上下文精确率',
  context_recall: '上下文召回率',
}

function metricLabel(key) {
  if (key === 'honesty_rate') return '诚实度'
  return METRIC_LABELS[key] || key
}

// partial = 跑完了但结果有已知降级（某指标没算出来，或有题目因检索为空被排除）。
// ⚠️ 必须认识它 —— 否则 usePolling 等不到终态，会一直轮询到 40 分钟上限。
const STATUS_LABELS = {
  pending: '排队中',
  running: '评测中',
  done: '完成',
  partial: '部分完成',
  failed: '失败',
}
const STATUS_TONES = {
  pending: 'warning',
  running: 'info',
  done: 'success',
  partial: 'warning',
  failed: 'danger',
}

const statusLabel = (s) => STATUS_LABELS[s] || s || '—'
const statusTone = (s) => STATUS_TONES[s] || 'neutral'

/**
 * 落库的 JSON 字段都是字符串，四处解析逻辑一致，收成一个函数。
 * 已经是对象（后端某天直接返回结构体）时也照样能用。
 */
function parseJson(raw, fallback) {
  if (raw == null || raw === '') return fallback
  if (typeof raw === 'object') return raw
  try {
    const parsed = JSON.parse(raw)
    return parsed ?? fallback
  } catch {
    return fallback
  }
}

function toneFor(n) {
  if (n >= 0.8) return 'success'
  if (n >= 0.6) return 'warning'
  return 'danger'
}

/**
 * 评测指标的展示形态。RAGAS 指标是 0~1 的小数，
 * 直接 toFixed(3) 会糊出一堆没意义的位数，这里换算成百分比。
 */
function scoreInfo(v) {
  if (v == null || v === '') return { text: '—', percent: 0, tone: '' }
  const n = Number(v)
  if (!Number.isFinite(n)) return { text: String(v), percent: 0, tone: '' }
  if (n >= 0 && n <= 1) {
    const pct = Math.round(n * 1000) / 10
    return { text: `${pct}%`, percent: pct, tone: toneFor(n) }
  }
  return { text: String(n), percent: Math.max(0, Math.min(100, n)), tone: '' }
}

/** 从报告的 metrics_json 里取某个指标（单项） */
function metricOf(report, key) {
  const m = parseJson(report?.metrics_json, {})
  return m && typeof m === 'object' ? m[key] : null
}

/** 列表单元格用：报告自带字段优先，否则回退到 metrics_json */
function scoreOf(report, key) {
  const raw = key === 'honesty_rate' ? report?.honesty_rate : metricOf(report, key)
  return scoreInfo(raw)
}

function fmtDuration(sec) {
  const n = Number(sec)
  if (!Number.isFinite(n) || n <= 0) return '—'
  if (n < 60) return `${n}s`
  const m = Math.floor(n / 60)
  const s = Math.round(n % 60)
  return `${m}m ${s}s`
}

/* ── 计算属性 ── */

const testsetOptions = computed(() =>
  testsets.value.map((t) => ({ value: t.name, label: `${t.title}（${t.count} 题）` })),
)

const pct = computed(() => {
  const p = Number(activeReport.value?.progress)
  if (!Number.isFinite(p)) return 0
  return Math.max(0, Math.min(100, Math.round(p)))
})

const isLive = computed(() =>
  ['pending', 'running'].includes(activeReport.value?.status),
)

const detailScores = computed(() => {
  const m = parseJson(detail.value?.metrics_json, {})
  const list = m && typeof m === 'object'
    ? Object.entries(m)
        // 下划线开头的是内部错误信息，不作为指标展示
        .filter(([k]) => !k.startsWith('_'))
        .map(([k, v]) => ({ key: k, label: metricLabel(k), ...scoreInfo(v) }))
    : []
  const honesty = detail.value?.honesty_rate
  if (honesty != null) {
    list.push({ key: 'honesty_rate', label: '诚实度', ...scoreInfo(honesty) })
  }
  return list
})

const detailRecommendations = computed(() => {
  const arr = parseJson(detail.value?.recommendations_json, [])
  return Array.isArray(arr) ? arr : []
})

const detailResults = computed(() => {
  const arr = parseJson(detail.value?.results_json, [])
  if (!Array.isArray(arr)) return []
  // 兜底行键：后端若没给 id，用下标保证 Vue 列表键唯一
  return arr.map((r, i) => ({ ...r, __rowKey: r?.id ?? i }))
})

const reportColumns = [
  { key: 'id', label: '#', width: '72px', mono: true },
  { key: 'testset_name', label: '评测集' },
  { key: 'status', label: '状态', width: '96px' },
  { key: 'progress', label: '题数', width: '80px', align: 'right' },
  { key: 'faithfulness', label: '忠实度', width: '84px', align: 'right', mono: true },
  { key: 'honesty', label: '诚实度', width: '84px', align: 'right', mono: true },
  { key: 'duration', label: '耗时', width: '88px', align: 'right' },
  { key: 'created_at', label: '时间', width: '120px' },
  { key: 'actions', label: '操作', width: '140px' },
]

const resultColumns = [
  { key: 'category', label: '分类', width: '110px' },
  { key: 'question', label: '问题', width: '26%' },
  { key: 'answer', label: '回答' },
  { key: 'contexts_count', label: '检索到', width: '84px', align: 'right', mono: true },
  { key: 'refused', label: '如实拒绝', width: '92px' },
]

/* ── 表单交互 ── */

function toggleMetric(key) {
  const list = form.value.metrics
  const idx = list.indexOf(key)
  if (idx === -1) list.push(key)
  else list.splice(idx, 1)
}

/* ── 数据加载 ── */

async function loadTestsets() {
  try {
    const res = await getEvalTestsets()
    testsets.value = res.testsets || []
    metrics.value = res.metrics || []
    if (!form.value.testset && testsets.value.length) {
      // 优先选覆盖最全的那个评测集
      const best = testsets.value.reduce((a, b) => (b.count > a.count ? b : a))
      form.value.testset = best.name
    }
    if (!form.value.metrics.length) form.value.metrics = [...metrics.value]
  } catch (e) {
    toast.error(`评测集加载失败: ${e.message}`)
  }
}

async function loadReports() {
  if (!auth.isLoggedIn) return
  loading.value = true
  try {
    const res = await getEvalReports(30)
    reports.value = res.reports || []
  } catch (e) {
    toast.error(`报告加载失败: ${e.message}`)
  } finally {
    loading.value = false
  }
}

/* ── 发起评测 + 轮询 ── */

async function doRun() {
  if (!auth.isLoggedIn) {
    toast.error('请先登录')
    return
  }
  running.value = true
  try {
    const res = await runEval({
      testset: form.value.testset,
      metrics: form.value.metrics,
      sample_limit: form.value.sampleLimit,
    })
    const reportId = res?.data?.report_id
    if (!res.success || !reportId) {
      toast.error(res.error || res.detail || '提交失败')
      return
    }
    toast.success(`评测任务已提交（报告 #${reportId}）`)
    await loadReports()
    await poll(reportId)
  } catch (e) {
    toast.error(e.message)
  } finally {
    running.value = false
  }
}

/** 轮询评测进度直到终态（usePolling 会在组件卸载时自动停止） */
async function poll(reportId) {
  activeReport.value = { id: reportId, progress: 0, status: 'pending', completed: 0 }
  const final = await polling.start(
    async () => {
      // 单次请求失败视为瞬时抖动，返回 null 让循环继续，而不是中止整轮轮询
      try {
        const res = await getEvalReport(reportId)
        return res?.report || null
      } catch {
        return null
      }
    },
    {
      interval: 1500,
      maxAttempts: 1600, // 约 40 分钟
      isDone: (rep) =>
        !!rep && ['done', 'failed', 'partial'].includes(rep.status),
      onTick: (rep) => {
        if (rep) activeReport.value = rep
      },
    },
  )

  activeReport.value = null

  if (!final) {
    // 超时或被卸载取消
    toast.info('评测仍在进行，可稍后刷新查看')
    return
  }

  await loadReports()
  await showDetail(reportId)
  if (final.status === 'done') {
    toast.success('评测完成')
  } else if (final.status === 'partial') {
    // 结果可用但有降级（例如有题目因检索为空被排除），把原因如实说出来，
    // 不要让用户拿一个"看着完整"的数字去做结论
    toast.warning(`评测部分完成：${final.error || '部分指标未算出'}`, 8000)
  } else {
    toast.error(`评测失败: ${final.error || '未知错误'}`)
  }
}

/* ── 详情 ── */

async function showDetail(id) {
  try {
    const res = await getEvalReport(id)
    detail.value = res.report || null
    // 详情面板在页面下方，报告多时点「详情」在视口里看不到变化；
    // 等 DOM 更新后滚过去，让点击一定产生可见反馈。
    await nextTick()
    detailEl.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  } catch (e) {
    toast.error(`详情加载失败: ${e.message}`)
  }
}

/* ── 删除（二次确认） ── */

const { pending: deleting, error: deleteError, run: runDelete } = useAsyncAction(
  (id) => deleteEvalReport(id),
)

function askDelete(id) {
  deleteTarget.value = id
}

async function confirmDelete() {
  const id = deleteTarget.value
  await runDelete(id)
  if (deleteError.value) {
    toast.error(`删除失败: ${deleteError.value}`)
    return
  }
  if (detail.value?.id === id) detail.value = null
  deleteTarget.value = null
  await loadReports()
  toast.success(`已删除报告 #${id}`)
}

onMounted(async () => {
  await loadTestsets()
  await loadReports()
})
</script>

<style scoped>
/* ══ 页面骨架 ══ */
.page {
  display: flex;
  flex-direction: column;
  gap: var(--sp-5);
  width: 100%;
  padding: var(--sp-5) var(--sp-4) var(--sp-8);
}

/* ══ 卡片标题 / 区块标题 ══ */
.panel-title {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  min-width: 0;
  color: var(--c-text);
}
.panel-title h2 {
  margin: 0;
  font-size: var(--fs-md);
  font-weight: var(--fw-semibold);
}
.panel-title .ui-icon {
  color: var(--c-text-2);
}
.panel-sub {
  font-size: var(--fs-sm);
  font-weight: var(--fw-normal);
  color: var(--c-text-3);
}

.block {
  margin-top: var(--sp-6);
}
.block-title {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  margin: 0 0 var(--sp-3);
  font-size: var(--fs-base);
  font-weight: var(--fw-semibold);
  color: var(--c-text);
}
.block-title .ui-icon {
  color: var(--c-text-2);
}
.block-count {
  font-size: var(--fs-xs);
  font-weight: var(--fw-normal);
  color: var(--c-text-3);
}

/* ══ 发起评测表单 ══ */
.run-hint {
  margin-bottom: var(--sp-5);
}

.run-form {
  display: grid;
  grid-template-columns: minmax(220px, 1fr) 140px;
  gap: var(--sp-4) var(--sp-5);
}
.field {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  min-width: 0;
}
.field-wide {
  grid-column: 1 / -1;
}
.field-label {
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--c-text-2);
}

.pick-row {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
}
.pick-chip {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-2);
  min-height: 32px;
  padding: 0 var(--sp-3);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--c-text-2);
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--r-full);
  cursor: pointer;
  transition: color var(--dur-fast) var(--ease), background var(--dur-fast) var(--ease),
    border-color var(--dur-fast) var(--ease);
}
.pick-chip:hover:not(:disabled) {
  color: var(--c-text);
  border-color: var(--c-border-strong);
}
.pick-chip .pick-check {
  color: var(--c-text-3);
  transition: color var(--dur-fast) var(--ease);
}
.pick-chip.is-on {
  color: var(--c-accent-text);
  background: var(--c-accent-soft);
  border-color: var(--c-accent-border);
}
.pick-chip.is-on .pick-check {
  color: var(--c-accent);
}
.pick-chip:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.run-actions {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
}
.run-note {
  font-size: var(--fs-xs);
  color: var(--c-text-3);
}

/* ══ 进行中的评测 ══ */
.live-card {
  border-color: var(--c-accent-border);
  box-shadow: var(--sh-2);
}
.live-head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  color: var(--c-accent);
  margin-bottom: var(--sp-4);
}
.live-title {
  margin: 0;
  font-size: var(--fs-md);
  font-weight: var(--fw-semibold);
  color: var(--c-text);
}
.live-id {
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  color: var(--c-text-3);
}
.live-head :deep(.ui-badge) {
  margin-left: auto;
}

/* 进度条已抽成 UiProgress 组件。
   原先本页手写的那套类名（.progress-track/.progress-fill）是坏的：
   它们只定义在 KnowledgeView 的 <style scoped> 里，
   scoped 选择器带 [data-v-*]，命中不了别的组件，所以那一版根本没有样式。 */

.live-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--sp-4);
  margin-top: var(--sp-3);
  font-size: var(--fs-sm);
  color: var(--c-text-2);
}
.live-percent {
  font-family: var(--font-mono);
  font-size: var(--fs-xl);
  font-weight: var(--fw-semibold);
  line-height: var(--lh-tight);
  color: var(--c-accent);
}

/* ══ 表格内的通用件 ══ */
.mono {
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
}
.muted {
  color: var(--c-text-3);
}
.score-inline {
  font-weight: var(--fw-medium);
}
.open-mark {
  margin-left: var(--sp-1);
  color: var(--c-accent);
  vertical-align: middle;
}
.row-actions {
  display: flex;
  gap: var(--sp-2);
}
.long-text {
  margin: 0;
  max-width: 46ch;
  font-size: var(--fs-sm);
  line-height: var(--lh-base);
  color: var(--c-text);
  word-break: break-word;
}

/* ══ 详情：指标 hero ══ */
.detail {
  scroll-margin-top: var(--sp-5);
}
.detail-error {
  margin-bottom: var(--sp-5);
}
.detail-none {
  font-size: var(--fs-sm);
}

.score-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  gap: var(--sp-3);
}
.score-card {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  padding: var(--sp-4);
  background: var(--c-surface-2);
  border: 1px solid var(--c-border);
  border-radius: var(--r-md);
}
.score-card.tone-success {
  --score-color: var(--c-success);
}
.score-card.tone-warning {
  --score-color: var(--c-warning);
}
.score-card.tone-danger {
  --score-color: var(--c-danger);
}
.score-value {
  font-family: var(--font-mono);
  font-size: var(--fs-2xl);
  font-weight: var(--fw-semibold);
  line-height: var(--lh-tight);
  color: var(--score-color, var(--c-text));
}
.score-label {
  font-size: var(--fs-xs);
  color: var(--c-text-2);
  line-height: var(--lh-tight);
}
.score-bar {
  height: 4px;
  overflow: hidden;
  background: var(--c-surface-3);
  border-radius: var(--r-full);
}
.score-bar span {
  display: block;
  height: 100%;
  background: var(--score-color, var(--c-accent));
  border-radius: var(--r-full);
  transition: width var(--dur-base) var(--ease);
}

/* ══ 建议列表 ══ */
.rec-list {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  margin: 0;
  padding-left: var(--sp-5);
}
.rec-list li {
  font-size: var(--fs-sm);
  line-height: var(--lh-base);
  color: var(--c-text);
}

/* ══ 删除确认 ══ */
.confirm-text {
  margin: 0;
  font-size: var(--fs-sm);
  line-height: var(--lh-base);
  color: var(--c-text-2);
}

/* ══ 窄屏 ══ */
@media (max-width: 640px) {
  .page {
    padding: var(--sp-4) var(--sp-3) var(--sp-6);
  }
  .run-form {
    grid-template-columns: 1fr;
  }
  .score-grid {
    grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
  }
  .long-text {
    max-width: none;
  }
}
</style>
