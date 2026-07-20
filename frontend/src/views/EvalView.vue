<template>
  <div class="eval-view">
    <div class="eval-upload">
      <h3>📊 上传评测集</h3>
      <div class="upload-row">
        <input type="file" ref="testsetInput" accept=".json" />
        <button class="btn btn-primary" :disabled="uploading" @click="doUploadTestset">
          {{ uploading ? '上传中...' : '上传评测集' }}
        </button>
      </div>
    </div>

    <div class="eval-run">
      <h3>🚀 运行评测</h3>
      <div class="eval-run-row">
        <input v-model="evalFilename" class="input" placeholder="输入已上传的测试集文件名" />
        <button class="btn btn-primary" :disabled="running" @click="doRunEval">
          {{ running ? '评测运行中...' : '运行评测' }}
        </button>
      </div>
    </div>

    <div v-if="statusMsg" :class="['status-msg', statusType]">{{ statusMsg }}</div>

    <div v-if="evalResults.length" class="eval-results">
      <h3>📋 评测结果</h3>
      <table>
        <thead>
          <tr>
            <th>题号</th><th>问题</th><th>匹配分</th><th>幻觉</th><th>检索质量</th><th>详情</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in evalResults" :key="r.question_id">
            <td>{{ r.question_id }}</td>
            <td class="col-q">{{ r.question?.slice(0, 80) }}</td>
            <td>{{ formatScore(r.match_score) }}</td>
            <td>{{ r.is_hallucination ? '⚠️ 幻觉' : '✅' }}</td>
            <td>{{ r.retrieval_quality }}</td>
            <td class="col-d">{{ (r.hallucination_detail || '').slice(0, 100) }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="eval-reports">
      <div class="reports-header">
        <h3>📈 评测报告</h3>
        <button class="btn btn-sm" @click="refreshReports">🔄 刷新</button>
      </div>
      <table v-if="reports.length">
        <thead>
          <tr>
            <th>ID</th><th>测试集</th><th>总题数</th><th>准确率</th><th>幻觉率</th><th>创建时间</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in reports" :key="r.id">
            <td>{{ r.id }}</td>
            <td>{{ r.testset_name?.slice(0, 30) }}</td>
            <td>{{ r.total_questions }}</td>
            <td>{{ formatPct(r.accuracy) }}</td>
            <td>{{ formatPct(r.hallucination_rate) }}</td>
            <td>{{ String(r.created_at || '').slice(0, 19) }}</td>
            <td><button class="btn btn-sm" @click="viewDetail(r.id)">详情</button></td>
          </tr>
        </tbody>
      </table>
      <div v-else class="empty-state">暂无评测报告</div>
    </div>

    <div v-if="reportDetail" class="eval-detail">
      <h3>📝 报告详情
        <button class="btn btn-sm" @click="doExportReport" :disabled="!currentReportId">📥 导出报告</button>
        <button class="btn btn-sm" @click="reportDetail = null; currentReportId = null">关闭</button>
      </h3>
      <pre>{{ reportDetail }}</pre>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { uploadTestset, runEval, getEvalReports, getEvalReportDetail } from '../api/eval.js'
import { exportEvalReportMarkdown } from '../api/export.js'
import { useAuthStore } from '../stores/auth.js'

const auth = useAuthStore()
const testsetInput = ref(null)
const evalFilename = ref('')
const uploading = ref(false)
const running = ref(false)
const statusMsg = ref('')
const statusType = ref('')
const evalResults = ref([])
const reports = ref([])
const reportDetail = ref(null)
const currentReportId = ref(null)

function showMsg(msg, type = 'info') {
  statusMsg.value = msg
  statusType.value = type
  setTimeout(() => { statusMsg.value = '' }, 8000)
}

async function doUploadTestset() {
  const input = testsetInput.value
  if (!input?.files?.length) { showMsg('请选择评测集 JSON 文件', 'error'); return }
  if (!auth.isLoggedIn) { showMsg('请先登录', 'error'); return }
  uploading.value = true
  try {
    const res = await uploadTestset(input.files[0])
    if (res.success) {
      showMsg(`✅ ${res.message}`, 'success')
      evalFilename.value = res.data?.filename || input.files[0].name
      input.value = ''
    } else {
      showMsg(`❌ ${res.message || '上传失败'}`, 'error')
    }
  } catch (e) {
    showMsg(`❌ ${e.message}`, 'error')
  } finally {
    uploading.value = false
  }
}

async function doRunEval() {
  if (!evalFilename.value.trim()) { showMsg('请输入测试集文件名', 'error'); return }
  if (!auth.isLoggedIn) { showMsg('请先登录', 'error'); return }
  running.value = true
  evalResults.value = []
  try {
    const res = await runEval(evalFilename.value.trim())
    if (res.success) {
      showMsg(`✅ ${res.message}`, 'success')
      const data = res.data || {}
      evalResults.value = data.results || []
      await refreshReports()
    } else {
      showMsg(`❌ ${res.message || '评测失败'}`, 'error')
    }
  } catch (e) {
    showMsg(`❌ ${e.message}`, 'error')
  } finally {
    running.value = false
  }
}

async function refreshReports() {
  if (!auth.isLoggedIn) return
  try {
    const res = await getEvalReports()
    reports.value = res.data?.reports || []
  } catch { /* ignore */ }
}

async function viewDetail(id) {
  if (!auth.isLoggedIn) return
  currentReportId.value = id
  try {
    const res = await getEvalReportDetail(id)
    if (res.data) {
      const d = res.data
      let text = `测试集: ${d.testset_name}\n总题数: ${d.total_questions}\n完成数: ${d.completed}\n准确率: ${d.accuracy?.toFixed(2)}%\n幻觉率: ${d.hallucination_rate?.toFixed(2)}%\n平均匹配分: ${d.avg_match_score?.toFixed(2)}\n创建时间: ${d.created_at}\n\n--- 逐题详情 ---\n`
      for (const item of d.results || []) {
        text += `\n题号 ${item.question_id}:\n  问题: ${item.question}\n  匹配分: ${item.match_score?.toFixed(2)}\n  幻觉: ${item.is_hallucination ? '是' : '否'}\n`
        if (item.hallucination_detail) text += `  详情: ${item.hallucination_detail}\n`
      }
      reportDetail.value = text
    }
  } catch (e) {
    showMsg(`获取详情失败: ${e.message}`, 'error')
  }
}

function doExportReport() {
  if (!currentReportId.value) return
  exportEvalReportMarkdown(currentReportId.value).catch(err => {
    console.error('导出报告失败:', err)
    showMsg(`导出失败: ${err.message}`, 'error')
  })
}

function formatScore(s) { return typeof s === 'number' ? s.toFixed(0) : '0' }
function formatPct(v) { return typeof v === 'number' ? v.toFixed(0) + '%' : '-' }

// 进入页面时自动加载评测报告列表
onMounted(() => {
  if (auth.isLoggedIn) refreshReports()
})
</script>
