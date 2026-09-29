<script setup>
/**
 * 徽章 / 状态标签
 * ================
 *
 * 改造前有 7 套徽章（.count-badge / .eval-badge.* / .role-tag / .role-badge /
 * .status-badge.* / .progress-status.* / .metric-chip），
 * 同一个"圆角胶囊 + 语义浅底"重写 7 次，配色还互不一致
 * （光是管理员那个绿就有 #dcfce7 和 #f0fdf4 两种写法）。
 */
defineProps({
  tone: {
    type: String,
    default: 'neutral',
    validator: (v) =>
      ['neutral', 'accent', 'success', 'warning', 'danger', 'info'].includes(v),
  },
  /** dot=前面加个小圆点，用于"运行中/已完成"这类状态 */
  dot: { type: Boolean, default: false },
  /** 等宽数字，用于分数、计数这类需要对齐的值 */
  mono: { type: Boolean, default: false },
})
</script>

<template>
  <span class="ui-badge" :class="[`is-${tone}`, { 'is-mono': mono }]">
    <span v-if="dot" class="ui-badge-dot" />
    <slot />
  </span>
</template>

<style scoped>
.ui-badge {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
  padding: 2px var(--sp-2);
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  line-height: 1.6;
  border: 1px solid transparent;
  border-radius: var(--r-full);
  white-space: nowrap;
}
.ui-badge.is-mono {
  font-family: var(--font-mono);
  letter-spacing: 0;
}

.ui-badge-dot {
  width: 6px;
  height: 6px;
  background: currentColor;
  border-radius: 50%;
  flex-shrink: 0;
}

.ui-badge.is-neutral {
  color: var(--c-text-2);
  background: var(--c-surface-2);
  border-color: var(--c-border);
}
.ui-badge.is-accent {
  color: var(--c-accent-text);
  background: var(--c-accent-soft);
  border-color: var(--c-accent-border);
}
.ui-badge.is-success {
  color: var(--c-success-text);
  background: var(--c-success-soft);
  border-color: var(--c-success-border);
}
.ui-badge.is-warning {
  color: var(--c-warning-text);
  background: var(--c-warning-soft);
  border-color: var(--c-warning-border);
}
.ui-badge.is-danger {
  color: var(--c-danger-text);
  background: var(--c-danger-soft);
  border-color: var(--c-danger-border);
}
.ui-badge.is-info {
  color: var(--c-info-text);
  background: var(--c-info-soft);
  border-color: var(--c-info-border);
}
</style>
