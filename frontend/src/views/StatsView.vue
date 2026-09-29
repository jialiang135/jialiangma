<script setup>
/**
 * 用量统计页
 * ==========
 *
 * 原先是挤在对话页底部的一条折叠横条 —— 那个位置的问题是：
 * 1. 它常年占着一行高度，而这是个**偶尔看一眼**的信息
 * 2. 折叠态只能显示三个数字，展开后又被消息区挤得很难看
 * 3. 它是"统计"，和"对话"本来就是两件事，塞在一起不伦不类
 *
 * 现在独立成页，放进导航菜单，展开就是完整的分析视图。
 *
 * 数据来自 `GET /api/token/stats`，注意那个接口是**带信封**的
 * （`{success, message, data: {...}}`），要取 `data.data`。
 */
import { computed, onMounted, ref, watch } from 'vue'

import UiAlert from '../components/ui/UiAlert.vue'
import UiCard from '../components/ui/UiCard.vue'
import UiEmpty from '../components/ui/UiEmpty.vue'
import UiIcon from '../components/ui/UiIcon.vue'
import UiSpinner from '../components/ui/UiSpinner.vue'
import UiTabs from '../components/ui/UiTabs.vue'
import { getTokenStats } from '../api/token.js'
import { useAsyncData } from '../composables/useAsyncData.js'
import { useTokenUsageSignal } from '../composables/useTokenUsage.js'
import { useAuthStore } from '../stores/auth.js'
import { formatCost, formatTokens } from '../utils/format.js'

const auth = useAuthStore()

const PERIODS = [
  { key: '7', label: '近 7 天' },
  { key: '30', label: '近 30 天' },
]
const period = ref('7')

/**
 * 视角：只看自己 / 全部用户。
 * 「全部用户」只有管理员能用（后端会校验角色，前端这里只是不给他看见入口）。
 */
const SCOPES = [
  { key: 'me', label: '我的' },
  { key: 'all', label: '全部用户' },
]
const scope = ref('me')
const scopeTabs = computed(() =>
  auth.isAdmin ? SCOPES : SCOPES.filter((s) => s.key === 'me'),
)

const { data, loading, error, run } = useAsyncData(() =>
  getTokenStats(Number(period.value), scope.value),
)

// 信封：真正的数据在 data.data 里
const stats = computed(() => data.value?.data || null)
const totals = computed(() => stats.value?.totals || null)
const models = computed(() => stats.value?.model_breakdown || [])
const daily = computed(() => stats.value?.daily || [])
/** 未匹配费率的模型 —— 必须提示，否则那个 ¥0 会被误读成"没花钱" */
const unmapped = computed(() => stats.value?.unmapped_models || [])
/** 按用户分解（仅「全部用户」视角返回） */
const byUser = computed(() => stats.value?.by_user || [])
/** 全部用户视角下，各用户占总量比例（用于占比条） */
const userMax = computed(() =>
  Math.max(1, ...byUser.value.map((u) => Number(u.total_tokens) || 0)),
)

/** 柱图高度按当周期最大值归一化；全 0 时给 1 避免除零 */
const dailyMax = computed(() =>
  Math.max(1, ...daily.value.map((d) => Number(d.total_tokens) || 0)),
)

function barHeight(row) {
  const ratio = (Number(row.total_tokens) || 0) / dailyMax.value
  // 有数据但极小时也留 4% 高度，否则看起来像没数据
  return `${Math.max(4, Math.round(ratio * 100))}%`
}

const dayLabel = (day) => String(day || '').slice(5) // "2026-09-29" → "09-29"

/** 每个模型占总量的比例，用来画那条占比条 */
function modelShare(row) {
  const total = Number(totals.value?.total_tokens) || 0
  if (!total) return 0
  return Math.round(((Number(row.total_tokens) || 0) / total) * 100)
}

watch([period, scope], () => run())
// 对话完成后会 bump 这个信号，跟着刷新
watch(useTokenUsageSignal(), () => run())
onMounted(run)
</script>

<template>
  <div class="page">
    <header class="page-head">
      <div class="head-text">
        <h1 class="page-title">用量统计</h1>
        <p class="page-desc">Token 消耗与费用估算。对话完成会自动刷新。</p>
      </div>
      <div v-if="totals" class="head-meta">
        <span class="meta-item">
          <UiIcon name="activity" :size="15" />
          {{ formatTokens(totals.total_tokens) }} tokens
        </span>
        <span class="meta-item">
          <UiIcon name="chart" :size="15" />
          {{ totals.total_calls }} 次调用
        </span>
      </div>
    </header>

    <div class="toolbar">
      <UiTabs v-model="period" :tabs="PERIODS" variant="pill" />
      <!-- 视角切换：只有管理员会看到「全部用户」这一项 -->
      <UiTabs v-if="scopeTabs.length > 1" v-model="scope" :tabs="scopeTabs" variant="pill" />
      <span v-if="loading" class="loading-hint"><UiSpinner :size="13" /> 加载中…</span>
    </div>

    <UiAlert v-if="error" tone="danger">加载失败：{{ error }}</UiAlert>

    <div v-else-if="loading && !totals" class="center"><UiSpinner :size="24" /></div>

    <UiEmpty
      v-else-if="!totals || totals.total_calls === 0"
      icon="activity"
      title="这段时间还没有用量"
      description="去对话页问几个问题，这里就会有数据了。"
    />

    <template v-else>
      <!-- 费用未知：查不到费率的模型，成本按 0 计 -->
      <UiAlert v-if="unmapped.length" tone="warning">
        费用未知：{{ unmapped.join('、') }} 不在费率表里，成本按 0 计（未套用任何默认价）。
        在 <code>config/.env</code> 里用 <code>LLM_COST_RATES</code> 补上真实费率即可。
      </UiAlert>

      <!-- 总量：四个数字卡片 -->
      <div class="totals-grid">
        <UiCard class="stat">
          <span class="stat-label">总 Token</span>
          <span class="stat-value">{{ formatTokens(totals.total_tokens) }}</span>
        </UiCard>
        <UiCard class="stat">
          <span class="stat-label">总费用</span>
          <span class="stat-value">
            {{ formatCost(totals.total_cost) }}
            <span v-if="unmapped.length" class="stat-warn">（不完整）</span>
          </span>
        </UiCard>
        <UiCard class="stat">
          <span class="stat-label">调用次数</span>
          <span class="stat-value">{{ formatTokens(totals.total_calls) }}</span>
        </UiCard>
        <UiCard class="stat">
          <span class="stat-label">缓存命中</span>
          <span class="stat-value">{{ formatTokens(totals.cached_tokens) }}</span>
        </UiCard>
      </div>

      <!-- 每日用量 -->
      <UiCard v-if="daily.length">
        <template #header>
          <h2 class="section-title">每日用量</h2>
        </template>
        <div class="chart">
          <div
            v-for="row in daily"
            :key="row.day"
            class="bar-col"
            :title="`${row.day} · ${formatTokens(row.total_tokens)} tokens · ${formatCost(row.cost)}`"
          >
            <span class="bar-value">{{ formatTokens(row.total_tokens) }}</span>
            <div class="bar-track">
              <div class="bar-fill" :style="{ height: barHeight(row) }" />
            </div>
            <span class="bar-label">{{ dayLabel(row.day) }}</span>
          </div>
        </div>
      </UiCard>

      <!-- 按模型 -->
      <UiCard v-if="models.length">
        <template #header>
          <h2 class="section-title">按模型</h2>
        </template>
        <ul class="models">
          <li v-for="m in models" :key="m.model" class="model">
            <div class="model-head">
              <span class="model-name" :title="m.model">{{ m.model }}</span>
              <span class="model-calls">{{ formatTokens(m.call_count) }} 次</span>
              <span class="model-tokens">{{ formatTokens(m.total_tokens) }}</span>
              <span class="model-cost">{{ formatCost(m.cost) }}</span>
            </div>
            <!-- 占比条：一眼看出哪个模型吃掉了大部分额度 -->
            <div class="share-track">
              <div class="share-fill" :style="{ width: `${modelShare(m)}%` }" />
            </div>
          </li>
        </ul>
      </UiCard>

      <!-- 按用户（仅「全部用户」视角返回） -->
      <UiCard v-if="scope === 'all' && byUser.length">
        <template #header>
          <h2 class="section-title">按用户</h2>
        </template>
        <ul class="models">
          <li v-for="u in byUser" :key="u.owner_id" class="model">
            <div class="model-head">
              <span class="model-name" :title="`owner_id=${u.owner_id}`">
                {{ u.username || `用户 #${u.owner_id}` }}
              </span>
              <span class="model-calls">{{ formatTokens(u.call_count) }} 次</span>
              <span class="model-tokens">{{ formatTokens(u.total_tokens) }}</span>
              <span class="model-cost">{{ formatCost(u.cost) }}</span>
            </div>
            <!-- 占比条按"用量最多的那个用户"归一化，一眼看出谁在消耗 -->
            <div class="share-track">
              <div
                class="share-fill"
                :style="{ width: `${Math.max(2, Math.round(((Number(u.total_tokens) || 0) / userMax) * 100))}%` }"
              />
            </div>
          </li>
        </ul>
      </UiCard>

      <!-- 明细 -->
      <UiCard>
        <template #header>
          <h2 class="section-title">明细</h2>
        </template>
        <dl class="breakdown">
          <div class="row"><dt>输入</dt><dd>{{ formatTokens(totals.prompt_tokens) }}</dd></div>
          <div class="row"><dt>输出</dt><dd>{{ formatTokens(totals.completion_tokens) }}</dd></div>
          <div class="row"><dt>其中推理</dt><dd>{{ formatTokens(totals.reasoning_tokens) }}</dd></div>
          <div class="row"><dt>缓存命中</dt><dd>{{ formatTokens(totals.cached_tokens) }}</dd></div>
        </dl>
      </UiCard>
    </template>
  </div>
</template>

<style scoped>
.page {
  max-width: var(--content-max);
  margin: 0 auto;
  padding: var(--sp-6) var(--sp-5) var(--sp-10);
}

.page-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--sp-4);
  margin-bottom: var(--sp-5);
}
.page-title {
  font-size: var(--fs-xl);
  font-weight: var(--fw-semibold);
}
.page-desc {
  margin-top: var(--sp-1);
  font-size: var(--fs-sm);
  color: var(--c-text-2);
}
.head-meta {
  display: flex;
  gap: var(--sp-4);
  flex-shrink: 0;
}
.meta-item {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-2);
  font-size: var(--fs-sm);
  font-family: var(--font-mono);
  color: var(--c-text-2);
}

.toolbar {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  margin-bottom: var(--sp-5);
}
.loading-hint {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
  font-size: var(--fs-xs);
  color: var(--c-text-3);
}

.center {
  display: flex;
  justify-content: center;
  padding: var(--sp-12);
}

/* 四个总量卡片：数字是主角，所以用大字号 + 等宽 */
.totals-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: var(--sp-3);
  margin-bottom: var(--sp-4);
}
.stat {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.stat-label {
  font-size: var(--fs-xs);
  color: var(--c-text-3);
}
.stat-value {
  font-size: var(--fs-xl);
  font-weight: var(--fw-semibold);
  font-family: var(--font-mono);
  color: var(--c-text);
}
.stat-warn {
  font-size: var(--fs-xs);
  font-weight: var(--fw-normal);
  color: var(--c-warning-text);
}

.section-title {
  font-size: var(--fs-base);
  font-weight: var(--fw-semibold);
}

/* 每日柱图 */
.chart {
  display: flex;
  align-items: flex-end;
  gap: var(--sp-2);
  height: 180px;
}
.bar-col {
  display: flex;
  flex: 1;
  flex-direction: column;
  align-items: center;
  min-width: 0;
  height: 100%;
}
.bar-value {
  margin-bottom: var(--sp-1);
  font-size: 10px;
  font-family: var(--font-mono);
  color: var(--c-text-3);
  white-space: nowrap;
}
.bar-track {
  display: flex;
  flex: 1;
  align-items: flex-end;
  width: 100%;
}
.bar-fill {
  width: 100%;
  max-width: 40px;
  margin: 0 auto;
  background: var(--c-accent);
  border-radius: var(--r-sm) var(--r-sm) 0 0;
  transition: height var(--dur-base) var(--ease);
}
.bar-label {
  margin-top: var(--sp-2);
  font-size: var(--fs-xs);
  color: var(--c-text-3);
  white-space: nowrap;
}

/* 模型明细 */
.models {
  display: flex;
  flex-direction: column;
  gap: var(--sp-4);
}
.model-head {
  display: flex;
  align-items: baseline;
  gap: var(--sp-3);
  margin-bottom: var(--sp-2);
}
.model-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  font-size: var(--fs-sm);
  color: var(--c-text);
  text-overflow: ellipsis;
  white-space: nowrap;
}
.model-calls,
.model-tokens,
.model-cost {
  font-family: var(--font-mono);
  font-size: var(--fs-sm);
  color: var(--c-text-2);
  white-space: nowrap;
}
.model-cost {
  min-width: 68px;
  text-align: right;
  color: var(--c-text);
}
.share-track {
  height: 4px;
  overflow: hidden;
  background: var(--c-surface-3);
  border-radius: var(--r-full);
}
.share-fill {
  height: 100%;
  background: var(--c-accent);
  border-radius: var(--r-full);
  transition: width var(--dur-base) var(--ease);
}

/* 明细表 */
.breakdown {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.row {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--sp-3);
  padding-bottom: var(--sp-2);
  border-bottom: 1px solid var(--c-border);
}
.row:last-child {
  padding-bottom: 0;
  border-bottom: none;
}
.breakdown dt {
  font-size: var(--fs-sm);
  color: var(--c-text-2);
}
.breakdown dd {
  font-family: var(--font-mono);
  font-size: var(--fs-sm);
  color: var(--c-text);
}

:deep(.ui-card) + :deep(.ui-card) {
  margin-top: var(--sp-4);
}

@media (max-width: 768px) {
  .page {
    padding: var(--sp-4) var(--sp-3) var(--sp-8);
  }
  .page-head {
    flex-direction: column;
  }
  .chart {
    height: 130px;
  }
  .bar-value {
    display: none;
  }
}
</style>
