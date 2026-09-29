<script setup>
/**
 * 行内提示条
 * ==========
 *
 * 改造前"绿=成功 / 红=失败 / 蓝=信息"这套被写了 3 遍：
 * 全局 `.status-msg.*`、AdminView 的 `.msg.*`、TokenStats 的 `.ts-error`。
 * 其中 AdminView 用的 `.msg` 还和聊天气泡的全局 `.msg` **类名撞车**，
 * 通知条被套上了气泡的 `display:flex; max-width:88%` 布局。
 *
 * 这里明确叫 UiAlert，和消息气泡彻底分家。
 */
import UiIcon from './UiIcon.vue'

defineProps({
  tone: {
    type: String,
    default: 'info',
    validator: (v) => ['info', 'success', 'warning', 'danger'].includes(v),
  },
  /** 关掉左侧图标；纯文字提示可以关 */
  hideIcon: { type: Boolean, default: false },
})

const emit = defineEmits(['close'])

// 图标按语义走，不按颜色 —— 红色既可能是"错误"也可能是"危险操作确认"
const ICON = { info: 'info', success: 'check', warning: 'alert', danger: 'alert' }
</script>

<template>
  <div class="ui-alert" :class="`is-${tone}`" role="alert">
    <UiIcon v-if="!hideIcon" :name="ICON[tone]" :size="16" class="ui-alert-icon" />
    <div class="ui-alert-body">
      <slot />
    </div>
    <button
      v-if="$slots.close || $attrs.dismissible !== undefined"
      class="ui-alert-close"
      type="button"
      aria-label="关闭"
      @click="emit('close')"
    >
      <UiIcon name="close" :size="14" />
    </button>
  </div>
</template>

<style scoped>
.ui-alert {
  display: flex;
  align-items: flex-start;
  gap: var(--sp-2);
  padding: var(--sp-3) var(--sp-4);
  font-size: var(--fs-sm);
  line-height: var(--lh-base);
  border: 1px solid transparent;
  border-radius: var(--r-md);
}

.ui-alert-icon {
  margin-top: 2px;
}
.ui-alert-body {
  flex: 1;
  min-width: 0;
}
.ui-alert-close {
  flex-shrink: 0;
  display: inline-flex;
  padding: 2px;
  color: inherit;
  opacity: 0.6;
  border-radius: var(--r-sm);
}
.ui-alert-close:hover {
  opacity: 1;
}

.ui-alert.is-info {
  color: var(--c-info-text);
  background: var(--c-info-soft);
  border-color: var(--c-info-border);
}
.ui-alert.is-success {
  color: var(--c-success-text);
  background: var(--c-success-soft);
  border-color: var(--c-success-border);
}
.ui-alert.is-warning {
  color: var(--c-warning-text);
  background: var(--c-warning-soft);
  border-color: var(--c-warning-border);
}
.ui-alert.is-danger {
  color: var(--c-danger-text);
  background: var(--c-danger-soft);
  border-color: var(--c-danger-border);
}
</style>
