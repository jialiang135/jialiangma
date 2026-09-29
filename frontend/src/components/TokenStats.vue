<script setup>
/**
 * Token 用量统计
 * ==============
 *
 * 收在对话区底部，默认只有一行 —— 这是个**偶尔看一眼**的信息，
 * 不该常年占着一大块版面（改造前是一条常驻横条 + 展开后 366 行的面板）。
 *
 * 展开后给三样东西：
 *   1. 总量（调用次数 / token / 费用 / 缓存命中）
 *   2. 按天的小柱图 —— 一眼看出"哪几天用得多"
 *   3. 按模型的明细表
 *
 * 柱图是纯 CSS 画的，没引图表库：只有一个维度、十几个数据点，
 * 为此装一个依赖不划算，而且 CSS 版本在窄屏下自动适应。
 */
import { computed, onMounted, ref, watch } from 'vue'

import { getTokenStats } from '../api/token.js'
import { useAsyncData } from '../composables/useAsyncData.js'
import { useTokenUsageSignal } from '../composables/useTokenUsage.js'
import { formatCost, formatTokens } from '../utils/format.js'
import UiIcon from './ui/UiIcon.vue'
import UiSpinner from './ui/UiSpinner.vue'

const PERIOD_DAYS = 7

const expanded = ref(false)

const { data, loading, error, run } = useAsyncData(() => getTokenStats(PERIOD_DAYS))

const totals = computed(() => data.value?.totals || null)
const models = computed(() => data.value?.model_breakdown || [])
const daily = computed(() => data.value?.daily || [])

/** 柱图高度按当天最大值归一化；全为 0 时给个最小值避免除零 */
const dailyMax = computed(() =>
  Math.max(1, ...daily.value.map((d) => Number(d.total_tokens) || 0)),
)

function barHeight(row) {
  const ratio = (Number(row.total_tokens) || 0) / dailyMax.value
  // 有数据但极少时也留 4% 高度，否则看着像没数据
  return `${Math.max(4, Math.round(ratio * 100))}%`
}

function dayLabel(day) {
  return String(day || '').slice(5) // "2026-09-29" → "09-29"
}

// 对话完成时会 bumpTokenUsage()，这里跟着刷新
watch(useTokenUsageSignal(), () => {
  if (expanded.value) run()
})

onMounted(() => {
  // 折叠态也要拿到总量数字，所以首次就拉一次
  run()
})
</script>

<template>
  <div class="token-stats">
    <button class="ts-bar" type="button" :aria-expanded="expanded" @click="expanded = !expanded">
      <UiIcon name="activity" :size="14" class="ts-icon" />
      <span class="ts-title">Token 统计</span>

      <UiSpinner v-if="loading && !totals" :size="13" />
      <template v-else-if="totals">
        <span class="ts-metric">
          <span class="ts-num">{{ formatTokens(totals.total_tokens) }}</span>
          <span class="ts-unit">tokens</span>
        </span>
        <span class="ts-sep">·</span>
        <span class="ts-metric">
          <span class="ts-num">${{ formatCost(totals.total_cost) }}</span>
        </span>
        <span class="ts-sep">·</span>
        <span class="ts-metric ts-muted">{{ totals.total_calls }} 次调用</span>
      </template>
      <span v-else-if="error" class="ts-error">{{ error }}</span>

      <span class="ts-period">近 {{ PERIOD_DAYS }} 天</span>
      <UiIcon :name="expanded ? 'chevron-down' : 'chevron-right'" :size="14" class="ts-chevron" />
    </button>

    <Transition name="ts-expand">
      <div v-if="expanded" class="ts-panel">
        <div v-if="!totals" class="ts-panel-empty">暂无数据</div>

        <template v-else>
          <!-- 按天小柱图 -->
          <section v-if="daily.length" class="ts-section">
            <h4 class="ts-label">每日用量</h4>
            <div class="ts-chart">
              <div v-for="row in daily" :key="row.day" class="ts-bar-col" :title="`${row.day} · ${formatTokens(row.total_tokens)} tokens`">
                <div class="ts-bar-fill" :style="{ height: barHeight(row) }" />
                <span class="ts-bar-label">{{ dayLabel(row.day) }}</span>
              </div>
            </div>
          </section>

          <!-- 按模型明细 -->
          <section v-if="models.length" class="ts-section">
            <h4 class="ts-label">按模型</h4>
            <ul class="ts-models">
              <li v-for="m in models" :key="m.model" class="ts-model">
                <span class="ts-model-name" :title="m.model">{{ m.model }}</span>
                <span class="ts-model-calls">{{ m.call_count }} 次</span>
                <span class="ts-model-tokens">{{ formatTokens(m.total_tokens) }}</span>
                <span class="ts-model-cost">${{ formatCost(m.cost) }}</span>
              </li>
            </ul>
          </section>

          <!-- 总量明细 -->
          <section class="ts-section ts-totals">
            <div class="ts-total">
              <span class="ts-total-label">输入</span>
              <span class="ts-total-value">{{ formatTokens(totals.prompt_tokens) }}</span>
            </div>
            <div class="ts-total">
              <span class="ts-total-label">输出</span>
              <span class="ts-total-value">{{ formatTokens(totals.completion_tokens) }}</span>
            </div>
            <div class="ts-total">
              <span class="ts-total-label">推理</span>
              <span class="ts-total-value">{{ formatTokens(totals.reasoning_tokens) }}</span>
            </div>
            <div class="ts-total">
              <span class="ts-total-label">缓存命中</span>
              <span class="ts-total-value">{{ formatTokens(totals.cached_tokens) }}</span>
            </div>
          </section>
        </template>
      </div>
    </Transition>
  </div>
</template>

<style scoped>
.token-stats {
  font-size: var(--fs-xs);
}

.ts-bar {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  width: 100%;
  padding: var(--sp-1) var(--sp-2);
  color: var(--c-text-3);
  border-radius: var(--r-sm);
  transition: background var(--dur-fast) var(--ease);
}
.ts-bar:hover {
  background: var(--c-surface-2);
}

.ts-icon {
  flex-shrink: 0;
}
.ts-title {
  font-weight: var(--fw-medium);
  color: var(--c-text-2);
}

.ts-metric {
  display: inline-flex;
  align-items: baseline;
  gap: 3px;
}
.ts-num {
  font-family: var(--font-mono);
  font-weight: var(--fw-medium);
  color: var(--c-text);
}
.ts-unit {
  color: var(--c-text-3);
}
.ts-muted {
  color: var(--c-text-3);
}
.ts-sep {
  color: var(--c-border-strong);
}
.ts-error {
  color: var(--c-danger-text);
}

.ts-period {
  margin-left: auto;
  color: var(--c-text-3);
}
.ts-chevron {
  flex-shrink: 0;
}

/* —— 展开面板 —— */
.ts-panel {
  padding: var(--sp-3) var(--sp-2) var(--sp-2);
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--r-md);
}
.ts-panel-empty {
  padding: var(--sp-2);
  color: var(--c-text-3);
}

.ts-section + .ts-section {
  margin-top: var(--sp-4);
}
.ts-label {
  margin-bottom: var(--sp-2);
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  color: var(--c-text-2);
}

/* 每日柱图 */
.ts-chart {
  display: flex;
  align-items: flex-end;
  gap: var(--sp-1);
  height: 72px;
}
.ts-bar-col {
  display: flex;
  flex: 1;
  flex-direction: column;
  align-items: center;
  justify-content: flex-end;
  height: 100%;
  min-width: 0;
}
.ts-bar-fill {
  width: 100%;
  max-width: 28px;
  background: var(--c-accent);
  border-radius: var(--r-sm) var(--r-sm) 0 0;
  transition: height var(--dur-base) var(--ease);
}
.ts-bar-label {
  margin-top: var(--sp-1);
  font-size: 10px;
  color: var(--c-text-3);
  white-space: nowrap;
}

/* 模型明细 */
.ts-models {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.ts-model {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  padding: var(--sp-1) 0;
  border-bottom: 1px solid var(--c-border);
}
.ts-model:last-child {
  border-bottom: none;
}
.ts-model-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  color: var(--c-text);
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ts-model-calls,
.ts-model-tokens,
.ts-model-cost {
  font-family: var(--font-mono);
  color: var(--c-text-2);
  white-space: nowrap;
}
.ts-model-cost {
  min-width: 56px;
  text-align: right;
  color: var(--c-text);
}

/* 总量明细 */
.ts-totals {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(96px, 1fr));
  gap: var(--sp-2);
  padding-top: var(--sp-3);
  border-top: 1px solid var(--c-border);
}
.ts-total {
  display: flex;
  flex-direction: column;
}
.ts-total-label {
  color: var(--c-text-3);
}
.ts-total-value {
  font-family: var(--font-mono);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--c-text);
}

.ts-expand-enter-active,
.ts-expand-leave-active {
  transition: opacity var(--dur-base) var(--ease);
}
.ts-expand-enter-from,
.ts-expand-leave-to {
  opacity: 0;
}

@media (max-width: 768px) {
  .ts-period,
  .ts-muted {
    display: none;
  }
}
</style>
