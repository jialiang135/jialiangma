<template>
  <div class="eval-view">
    <!-- 发起评测 -->
    <div v-if="auth.isAdmin" class="eval-panel">
      <h3>📊 发起评测</h3>
      <p class="eval-hint">
        评测会对评测集中的每条问题跑一次完整问答，再用 RAGAS 逐条判定质量。
        <strong>很慢</strong>（每条约 10~30 秒），因此是后台任务，可随时离开本页。
      </p>

      <div class="eval-form">
        <label class="eval-field">
          <span>评测集</span>
          <select v-model="form.testset" :disabled="running">
            <option v-for="t in testsets" :key="t.name" :value="t.name">
              {{ t.title }}（{{ t.count }} 题）
            </option>
          </select>
        </label>

        <label class="eval-field">
          <span>样本数</span>
          <input type="number" v-model.number="form.sampleLimit" min="1" max="100" :disabled="running" />
        </label>

        <div class="eval-field eval-metrics">
          <span>指标</span>
          <div class="metric-checkboxes">
            <label v-for="m in metrics" :key="m" class="metric-chip">
              <input type="checkbox" :value="m" v-model="form.metrics" :disabled="running" />
              {{ metricLabel(m) }}
            </label>
          </div>
        </div>

        <button class="btn btn-primary" :disabled="running || !form.metrics.length" @click="doRun">
          {{ running ? '评测中…' : '开始评测' }}
        </button>
      </div>

      <p class="eval-note">
        样本按分类轮询抽取，因此小样本也能覆盖到「幻觉检测」类问题 ——
        那一类用来检验<strong>诚实度</strong>：知识库里本就没有答案时，
        系统有没有如实说"没有相关信息"。
      </p>
    </div>

    <!-- 进行中的任务 -->
    <div v-if="activeReport" class="eval-panel">
      <h3>⏳ 进行中：报告 #{{ activeReport.id }}</h3>
      <div class="progress-bar-track">
        <div :class="['progress-bar-fill', activeReport.status]"
             :style="{ width: activeReport.progress + '%' }"></div>
      </div>
      <div class="eval-progress-text">
        {{ activeReport.progress }}% · 评测集 {{ activeReport.testset_name }}
        · 已完成 {{ activeReport.completed }}/{{ activeReport.total_questions || '?' }}
      </div>
    </div>

    <!-- 报告列表 -->
    <div class="eval-panel">
      <div class="eval-panel-header">
        <h3>📋 历史报告</h3>
        <button class="btn btn-sm btn-outline" :disabled="loading" @click="loadReports">刷新</button>
      </div>

      <div v-if="!reports.length" class="empty-state">还没有评测记录</div>

      <div v-else class="table-wrapper">
        <table class="eval-table">
          <thead>
            <tr>
              <th>#</th>
              <th>评测集</th>
              <th>状态</th>
              <th>题数</th>
              <th>忠实度</th>
              <th>诚实度</th>
              <th>耗时</th>
              <th>时间</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="r in reports" :key="r.id" :class="{ 'row-active': detail && detail.id === r.id }">
              <td>{{ r.id }}</td>
              <td>{{ r.testset_name }}</td>
              <td><span :class="['eval-badge', r.status]">{{ statusLabel(r.status) }}</span></td>
              <td>{{ r.completed }}/{{ r.total_questions }}</td>
              <td>{{ fmt(metricOf(r, 'faithfulness')) }}</td>
              <td>{{ r.honesty_rate == null ? '—' : Math.round(r.honesty_rate * 100) + '%' }}</td>
              <td>{{ r.duration_seconds ? r.duration_seconds + 's' : '—' }}</td>
              <td>{{ (r.created_at || '').slice(5, 16) }}</td>
              <td class="eval-actions">
                <button class="btn btn-xs" @click="showDetail(r.id)">详情</button>
                <button v-if="auth.isAdmin" class="btn btn-xs" @click="doDelete(r.id)">删除</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- 报告详情 -->
    <div v-if="detail" class="eval-panel eval-detail-anchor">
      <div class="eval-panel-header">
        <h3>🔍 报告 #{{ detail.id }} · {{ detail.testset_name }}</h3>
        <button class="btn btn-sm btn-outline" @click="detail = null">收起</button>
      </div>

      <div v-if="detail.error" class="status-msg error">{{ detail.error }}</div>

      <div class="metric-grid">
        <div v-for="(v, k) in detailMetrics" :key="k" class="metric-card">
          <div class="metric-value">{{ v }}</div>
          <div class="metric-name">{{ metricLabel(k) }}</div>
        </div>
        <div v-if="detail.honesty_rate != null" class="metric-card">
          <div class="metric-value">{{ Math.round(detail.honesty_rate * 100) }}%</div>
          <div class="metric-name">诚实度</div>
        </div>
      </div>

      <div v-if="detailRecommendations.length" class="eval-recs">
        <h4>改进建议</h4>
        <ol>
          <li v-for="(r, i) in detailRecommendations" :key="i">{{ r }}</li>
        </ol>
      </div>

      <h4 class="eval-sub">逐题明细</h4>
      <div class="table-wrapper">
        <table class="eval-table">
          <thead>
            <tr>
              <th>分类</th>
              <th>问题</th>
              <th>回答</th>
              <th>检索到</th>
              <th>如实拒绝</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="q in detailResults" :key="q.id">
              <td>{{ q.category }}</td>
              <td class="cell-q">{{ q.question }}</td>
              <td class="cell-a">{{ q.answer }}</td>
              <td>{{ q.contexts_count }}</td>
              <td>
                <span :class="['eval-badge', q.refused ? 'done' : 'pending']">
                  {{ q.refused ? '是' : '否' }}
                </span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <div v-if="statusMsg" :class="['status-msg', statusType]">{{ statusMsg }}</div>
  </div>
</template>

<script setup>
import { ref, computed, nextTick, onMounted, onUnmounted } from 'vue'
import {
  getEvalTestsets, runEval, getEvalReports, getEvalReport, deleteEvalReport,
} from '../api/eval.js'
import { useAuthStore } from '../stores/auth.js'

const auth = useAuthStore()

const testsets = ref([])
const metrics = ref([])
const reports = ref([])
const detail = ref(null)
const loading = ref(false)
const running = ref(false)
const activeReport = ref(null)
const statusMsg = ref('')
const statusType = ref('info')
let timer = null

const form = ref({
  testset: '',
  sampleLimit: 5,
  metrics: [],
})

const METRIC_LABELS = {
  faithfulness: '忠实度（是否忠于检索内容）',
  answer_relevancy: '相关度（是否答到点）',
  context_precision: '上下文精确率',
  context_recall: '上下文召回率',
}

function metricLabel(key) {
  if (key === 'honesty_rate') return '诚实度'
  return METRIC_LABELS[key] || key
}

function showMsg(msg, type = 'info') {
  statusMsg.value = msg
  statusType.value = type
  setTimeout(() => { statusMsg.value = '' }, 5000)
}

function statusLabel(s) {
  return {
    pending: '排队中',
    running: '评测中',
    done: '完成',
    failed: '失败',
  }[s] || s
}

function fmt(v) {
  if (v == null) return '—'
  return typeof v === 'number' ? v.toFixed(3) : v
}

function metricOf(report, key) {
  if (!report.metrics_json) return null
  try {
    return JSON.parse(report.metrics_json)[key]
  } catch {
    return null
  }
}

const detailMetrics = computed(() => {
  if (!detail.value?.metrics_json) return {}
  try {
    const m = JSON.parse(detail.value.metrics_json)
    // 下划线开头的是内部错误信息，不作为指标展示
    return Object.fromEntries(Object.entries(m).filter(([k]) => !k.startsWith('_')))
  } catch {
    return {}
  }
})

const detailRecommendations = computed(() => {
  if (!detail.value?.recommendations_json) return []
  try {
    return JSON.parse(detail.value.recommendations_json)
  } catch {
    return []
  }
})

const detailResults = computed(() => {
  if (!detail.value?.results_json) return []
  try {
    return JSON.parse(detail.value.results_json)
  } catch {
    return []
  }
})

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
    showMsg(`评测集加载失败: ${e.message}`, 'error')
  }
}

async function loadReports() {
  if (!auth.isLoggedIn) return
  loading.value = true
  try {
    const res = await getEvalReports(30)
    reports.value = res.reports || []
  } catch (e) {
    showMsg(`报告加载失败: ${e.message}`, 'error')
  } finally {
    loading.value = false
  }
}

async function doRun() {
  if (!auth.isLoggedIn) { showMsg('请先登录', 'error'); return }
  running.value = true
  try {
    const res = await runEval({
      testset: form.value.testset,
      metrics: form.value.metrics,
      sample_limit: form.value.sampleLimit,
    })
    const reportId = res?.data?.report_id
    if (!res.success || !reportId) {
      showMsg(`❌ ${res.error || res.detail || '提交失败'}`, 'error')
      return
    }
    showMsg(`✅ 评测任务已提交（报告 #${reportId}）`, 'success')
    await loadReports()
    await poll(reportId)
  } catch (e) {
    showMsg(`❌ ${e.message}`, 'error')
  } finally {
    running.value = false
  }
}

/** 轮询评测进度，直到完成或失败 */
async function poll(reportId) {
  activeReport.value = { id: reportId, progress: 0, status: 'pending', completed: 0 }
  for (let i = 0; i < 1600; i++) {   // 最多轮询 40 分钟（1.5s 一次）
    await new Promise(r => setTimeout(r, 1500))
    let rep
    try {
      const res = await getEvalReport(reportId)
      rep = res.report
    } catch {
      continue
    }
    if (!rep) continue
    activeReport.value = rep
    if (rep.status === 'done' || rep.status === 'failed') {
      activeReport.value = null
      await loadReports()
      await showDetail(reportId)
      showMsg(rep.status === 'done' ? '✅ 评测完成' : `❌ 评测失败: ${rep.error}`,
              rep.status === 'done' ? 'success' : 'error')
      return
    }
  }
  activeReport.value = null
  showMsg('⚠️ 评测仍在进行，可稍后刷新查看', 'info')
}

async function showDetail(id) {
  try {
    const res = await getEvalReport(id)
    detail.value = res.report || null
    // 详情面板渲染在页面下方，报告多时点了「详情」在视口里看不到任何变化。
    // 等 DOM 更新后滚过去，让点击一定产生可见反馈。
    await nextTick()
    document.querySelector('.eval-detail-anchor')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  } catch (e) {
    showMsg(`详情加载失败: ${e.message}`, 'error')
  }
}

async function doDelete(id) {
  if (!confirm(`确定删除评测报告 #${id}？`)) return
  try {
    await deleteEvalReport(id)
    if (detail.value?.id === id) detail.value = null
    await loadReports()
    showMsg(`已删除报告 #${id}`, 'success')
  } catch (e) {
    showMsg(`删除失败: ${e.message}`, 'error')
  }
}

onMounted(async () => {
  await loadTestsets()
  await loadReports()
})

onUnmounted(() => {
  if (timer) clearInterval(timer)
})
</script>
