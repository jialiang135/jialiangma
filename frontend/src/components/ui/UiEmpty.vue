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
import { Comment, Text, computed, useSlots } from 'vue'
import UiIcon from './UiIcon.vue'

defineProps({
  icon: { type: String, default: 'file' },
  title: { type: String, required: true },
  description: { type: String, default: '' },
  /** compact 用于嵌在卡片里的小空状态（如某个 tab 里没数据） */
  compact: { type: Boolean, default: false },
})

const slots = useSlots()

/**
 * 只有当插槽**真的渲染出东西**时才显示操作区。
 *
 * 不能只用 `v-if="$slots.default"`：插槽只要被**声明**了，`$slots.default`
 * 就是真值；内容里写 `v-if="isAdmin"` 而条件为假时，渲染结果是一个注释节点，
 * 于是外层 `.ui-empty-actions` 照样渲染，页面上就多出一块莫名其妙的空白。
 * （这是 UiTable 的 `#empty-action` 暴露出来的实际问题。）
 */
const hasActions = computed(() => {
  const render = slots.default
  if (!render) return false
  const isReal = (node) => {
    if (!node || typeof node !== 'object') return false
    // 注释节点（含 v-if 为假留下的占位）与空白文本都不算内容
    if (node.type === Comment) return false
    if (node.type === Text) return String(node.children ?? '').trim().length > 0
    // Fragment（v-if/v-for 的常见包裹）要往里看一层
    if (Array.isArray(node.children)) return node.children.some(isReal)
    return true
  }
  return render().some(isReal)
})
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
