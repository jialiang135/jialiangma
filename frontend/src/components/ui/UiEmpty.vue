<script setup>
/**
 * 空状态
 * ======
 *
 * 改造前每个页面各写一套：ChatView 的富空状态（emoji 徽章 + 标题 + 说明 +
 * 快捷卡片）、KnowledgeView 的 `.empty-state` 单行文字、EvalView 的
 * `.empty-state`、AdminView 的 `.empty`（四处文案还都不同）。
 * 更麻烦的是 `.empty-state` 在全局和 AdminView 局部**同名不同源**。
 *
 * 统一成一个组件：图标 + 标题 + 说明 + 可选的操作区。
 */
import { computed } from 'vue'

import { useHasSlot } from '../../composables/useSlots.js'
import UiIcon from './UiIcon.vue'

defineProps({
  icon: { type: String, default: 'file' },
  title: { type: String, required: true },
  description: { type: String, default: '' },
  /** compact 用于嵌在卡片里的小空状态（如某个 tab 里没数据） */
  compact: { type: Boolean, default: false },
})

/**
 * 只有当插槽**真的渲染出东西**时才显示操作区 ——
 * 不能只用 `v-if="$slots.default"`，那在条件为假时（渲染成注释节点）
 * 也会留下一个空的包裹层。这个坑是 UiTable 的 `#empty-action` 暴露出来的，
 * 判断逻辑已抽到 composables/useSlots.js 供各处复用。
 */
const slotHasActions = useHasSlot()
const hasActions = computed(() => slotHasActions())
</script>

<template>
  <div class="ui-empty" :class="{ 'is-compact': compact }">
    <div class="ui-empty-icon">
      <UiIcon :name="icon" :size="compact ? 20 : 26" />
    </div>
    <p class="ui-empty-title">{{ title }}</p>
    <p v-if="description" class="ui-empty-desc">{{ description }}</p>
    <div v-if="hasActions" class="ui-empty-actions">
      <slot />
    </div>
  </div>
</template>

<style scoped>
.ui-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: var(--sp-12) var(--sp-6);
  text-align: center;
}
.ui-empty.is-compact {
  padding: var(--sp-8) var(--sp-4);
}

.ui-empty-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 52px;
  height: 52px;
  margin-bottom: var(--sp-4);
  color: var(--c-text-3);
  background: var(--c-surface-2);
  border-radius: var(--r-lg);
}
.ui-empty.is-compact .ui-empty-icon {
  width: 40px;
  height: 40px;
  margin-bottom: var(--sp-3);
}

.ui-empty-title {
  font-size: var(--fs-md);
  font-weight: var(--fw-semibold);
  color: var(--c-text);
}
.ui-empty-desc {
  max-width: 44ch;
  margin-top: var(--sp-2);
  font-size: var(--fs-sm);
  color: var(--c-text-2);
  line-height: var(--lh-base);
}

.ui-empty-actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
  justify-content: center;
  margin-top: var(--sp-5);
}
</style>
