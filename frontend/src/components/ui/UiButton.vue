<script setup>
/**
 * 按钮
 * ====
 *
 * 改造前按钮有三套并行实现：全局 `.btn` 体系、AdminView 里覆盖一遍
 * `.btn-xs` 和 `.modal-actions .btn`、以及独立的 `.btn-speak`。
 * 这里收成一处，用 props 表达差异。
 */
import UiSpinner from './UiSpinner.vue'

defineProps({
  /** primary=主操作 secondary=次操作 ghost=无边框 danger=破坏性 */
  variant: {
    type: String,
    default: 'secondary',
    validator: (v) => ['primary', 'secondary', 'ghost', 'danger'].includes(v),
  },
  size: {
    type: String,
    default: 'md',
    validator: (v) => ['sm', 'md'].includes(v),
  },
  disabled: { type: Boolean, default: false },
  /** 加载中：按钮变灰、显示 spinner、自动禁用点击 */
  loading: { type: Boolean, default: false },
  block: { type: Boolean, default: false },
  type: { type: String, default: 'button' },
})
</script>

<template>
  <button
    :type="type"
    class="ui-btn"
    :class="[`is-${variant}`, `is-${size}`, { 'is-loading': loading, 'is-block': block }]"
    :disabled="disabled || loading"
  >
    <UiSpinner v-if="loading" :size="14" />
    <slot name="icon" />
    <span v-if="$slots.default" class="ui-btn-label"><slot /></span>
  </button>
</template>

<style scoped>
.ui-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: var(--sp-2);
  font-weight: var(--fw-medium);
  border: 1px solid transparent;
  border-radius: var(--r-md);
  white-space: nowrap;
  transition: background var(--dur-fast) var(--ease),
    border-color var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease);
}

.ui-btn.is-md {
  height: 36px;
  padding: 0 var(--sp-4);
  font-size: var(--fs-base);
}
.ui-btn.is-sm {
  height: 28px;
  padding: 0 var(--sp-3);
  font-size: var(--fs-sm);
}
.ui-btn.is-block {
  width: 100%;
}

/* —— 变体 —— */
.ui-btn.is-primary {
  color: var(--c-white);
  background: var(--c-accent);
}
.ui-btn.is-primary:hover:not(:disabled) {
  background: var(--c-accent-hover);
}

.ui-btn.is-secondary {
  color: var(--c-text);
  background: var(--c-surface);
  border-color: var(--c-border);
}
.ui-btn.is-secondary:hover:not(:disabled) {
  background: var(--c-surface-2);
  border-color: var(--c-border-strong);
}

.ui-btn.is-ghost {
  color: var(--c-text-2);
  background: transparent;
}
.ui-btn.is-ghost:hover:not(:disabled) {
  color: var(--c-text);
  background: var(--c-surface-2);
}

.ui-btn.is-danger {
  color: var(--c-danger-text);
  background: var(--c-danger-soft);
  border-color: var(--c-danger-border);
}
.ui-btn.is-danger:hover:not(:disabled) {
  color: var(--c-white);
  background: var(--c-danger);
  border-color: var(--c-danger);
}

.ui-btn:disabled {
  opacity: 0.5;
}
/* 加载中保持原色（只是不可点），比整体变灰更像"正在干活" */
.ui-btn.is-loading {
  opacity: 1;
  cursor: progress;
}

.ui-btn-label {
  display: inline-block;
}
</style>
