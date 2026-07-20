<template>
  <div class="token-stats" v-if="visible">
    <div class="ts-header" @click="expanded = !expanded">
      <span class="ts-title">📊 Token 统计</span>
      <span class="ts-summary">
        <span class="stat-item">
          今日: <strong>{{ todayTokens }}</strong> tokens
        </span>
        <span class="stat-item">
          费用: <strong>${{ todayCost }}</strong>
        </span>
      </span>
      <span class="ts-toggle">{{ expanded ? '收起 ▲' : '展开 ▼' }}</span>
    </div>

    <div v-if="expanded" class="ts-body">
      <div v-if="loading" class="ts-loading">加载中...</div>
      <div v-else-if="error" class="ts-error">⚠️ {{ error }}</div>
      <div v-else>
        <!-- 汇总卡片 -->
        <div class="ts-totals-row">
          <div class="ts-total-card">
            <span class="total-label">总 Token</span>
            <span class="total-value">{{ totals.total_tokens?.toLocaleString() || 0 }}</span>
          </div>
          <div class="ts-total-card">
            <span class="total-label">Prompt</span>
            <span class="total-value">{{ totals.prompt_tokens?.toLocaleString() || 0 }}</span>
          </div>
          <div class="ts-total-card">
            <span class="total-label">Completion</span>
            <span class="total-value">{{ totals.completion_tokens?.toLocaleString() || 0 }}</span>
          </div>
          <div class="ts-total-card">
            <span class="total-label">总费用</span>
            <span class="total-value cost">${{ totals.total_cost?.toFixed(5) || '0' }}</span>
          </div>
          <div class="ts-total-card">
            <span class="total-label">调用次数</span>
            <span class="total-value">{{ totals.total_calls || 0 }}</span>
          </div>
        </div>

        <!-- 7 天趋势图 -->
        <div v-if="daily.length > 0" class="ts-chart-section">
          <h4>近 {{ daily.length }} 天趋势</h4>
          <div class="ts-chart">
            <div v-for="d in daily" :key="d.day" class="ts-chart-col">
              <div class="ts-chart-bars">
                <div
                  class="ts-chart-bar prompt"
                  :style="{ height: barHeight(d.prompt_tokens) + '%' }"
                  :title="'Prompt: ' + d.prompt_tokens"
                ></div>
                <div
                  class="ts-chart-bar completion"
                  :style="{ height: barHeight(d.completion_tokens) + '%' }"
                  :title="'Completion: ' + d.completion_tokens"
                ></div>
              </div>
              <span class="ts-chart-label">{{ formatDay(d.day) }}</span>
            </div>
          </div>
          <div class="chart-legend">
            <span class="legend-item"><span class="legend-dot prompt"></span> Prompt</span>
            <span class="legend-item"><span class="legend-dot completion"></span> Completion</span>
          </div>
        </div>

        <!-- 模型分解 -->
        <div v-if="modelBreakdown.length > 0" class="ts-model-section">
          <h4>模型使用分解</h4>
          <table class="ts-model-table">
            <thead>
              <tr>
                <th>模型</th>
                <th>调用次数</th>
                <th>总 Token</th>
                <th>费用</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="m in modelBreakdown" :key="m.model">
                <td><strong>{{ m.model }}</strong></td>
                <td>{{ m.call_count }}</td>
                <td>{{ m.total_tokens?.toLocaleString() || 0 }}</td>
                <td>${{ m.cost?.toFixed(5) || '0' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { getTokenStats } from '../api/token.js'
import { useAuthStore } from '../stores/auth.js'

const auth = useAuthStore()
const expanded = ref(false)
const visible = ref(true)
const loading = ref(false)
const error = ref('')

const daily = ref([])
const modelBreakdown = ref([])
const totals = ref({})

const maxToken = ref(1)

const todayTokens = computed(() => {
  if (daily.value.length === 0) return 0
  const today = daily.value[daily.value.length - 1]
  return today.total_tokens || 0
})

const todayCost = computed(() => {
  if (daily.value.length === 0) return '0'
  const today = daily.value[daily.value.length - 1]
  return today.cost?.toFixed(5) || '0'
})

async function fetchStats() {
  if (!auth.isLoggedIn) return
  loading.value = true
  error.value = ''
  try {
    const res = await getTokenStats(7)
    if (res.success !== false && res.data) {
      daily.value = res.data.daily || []
      modelBreakdown.value = res.data.model_breakdown || []
      totals.value = res.data.totals || {}
      maxToken.value = Math.max(
        1,
        ...daily.value.map(d => (d.prompt_tokens || 0) + (d.completion_tokens || 0))
      )
    }
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

function barHeight(tokens) {
  return maxToken.value > 0 ? ((tokens || 0) / maxToken.value) * 100 : 0
}

function formatDay(dayStr) {
  if (!dayStr) return ''
  const parts = dayStr.split('-')
  if (parts.length >= 3) return parts[1] + '/' + parts[2]
  return dayStr.slice(5)
}

onMounted(() => {
  if (auth.isLoggedIn) fetchStats()
})

watch(() => auth.isLoggedIn, (loggedIn) => {
  if (loggedIn) fetchStats()
})
</script>

<style scoped>
.token-stats {
  border: 1px solid #e0e0e0;
  border-radius: 10px;
  overflow: hidden;
  margin-top: 8px;
  background: #fff;
}
.ts-header {
  display: flex;
  align-items: center;
  padding: 10px 14px;
  cursor: pointer;
  user-select: none;
  gap: 12px;
  background: #fafafa;
  transition: background 0.2s;
}
.ts-header:hover {
  background: #f0f4ff;
}
.ts-title {
  font-weight: 600;
  font-size: 14px;
  white-space: nowrap;
}
.ts-summary {
  display: flex;
  gap: 16px;
  flex: 1;
  font-size: 13px;
  color: #555;
}
.stat-item strong {
  color: #333;
}
.ts-toggle {
  font-size: 12px;
  color: #888;
  white-space: nowrap;
}
.ts-body {
  padding: 14px;
  border-top: 1px solid #e0e0e0;
}
.ts-loading, .ts-error {
  text-align: center;
  padding: 16px;
  color: #888;
  font-size: 14px;
}
.ts-error {
  color: #c62828;
}

/* Totals */
.ts-totals-row {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 16px;
}
.ts-total-card {
  flex: 1;
  min-width: 100px;
  background: #f8f9fa;
  border: 1px solid #eee;
  border-radius: 8px;
  padding: 12px;
  text-align: center;
}
.total-label {
  display: block;
  font-size: 12px;
  color: #888;
  margin-bottom: 4px;
}
.total-value {
  display: block;
  font-size: 18px;
  font-weight: 700;
  color: #333;
}
.total-value.cost {
  color: #2e7d32;
  font-family: monospace;
}

/* Chart */
.ts-chart-section h4,
.ts-model-section h4 {
  margin: 0 0 12px;
  font-size: 14px;
  color: #555;
}
.ts-chart {
  display: flex;
  align-items: flex-end;
  gap: 8px;
  height: 120px;
  padding: 8px 0;
}
.ts-chart-col {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  height: 100%;
}
.ts-chart-bars {
  flex: 1;
  display: flex;
  align-items: flex-end;
  gap: 3px;
  width: 100%;
}
.ts-chart-bar {
  flex: 1;
  border-radius: 3px 3px 0 0;
  min-height: 2px;
  transition: height 0.4s ease;
}
.ts-chart-bar.prompt {
  background: linear-gradient(180deg, #42a5f5, #1e88e5);
}
.ts-chart-bar.completion {
  background: linear-gradient(180deg, #66bb6a, #43a047);
}
.ts-chart-label {
  font-size: 11px;
  color: #888;
  margin-top: 4px;
}
.chart-legend {
  display: flex;
  gap: 16px;
  justify-content: center;
  margin-top: 8px;
  font-size: 12px;
  color: #555;
}
.legend-item {
  display: flex;
  align-items: center;
  gap: 4px;
}
.legend-dot {
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 2px;
}
.legend-dot.prompt {
  background: #1e88e5;
}
.legend-dot.completion {
  background: #43a047;
}

/* Model table */
.ts-model-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.ts-model-table th {
  text-align: left;
  padding: 6px 10px;
  border-bottom: 2px solid #e0e0e0;
  color: #555;
  font-weight: 600;
}
.ts-model-table td {
  padding: 8px 10px;
  border-bottom: 1px solid #f0f0f0;
}
.ts-model-table tbody tr:hover {
  background: #f8f9fa;
}
</style>
