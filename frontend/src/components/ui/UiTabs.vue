<script setup>
/**
 * 标签页
 * ======
 *
 * 管理页改造前把 6 个 tab 的按钮和样式全写在视图里（含 6 个 emoji 图标）。
 * 抽出来之后，任何页面要用 tab 都走这套。
 *
 * 无障碍：用 role="tablist"/"tab" + aria-selected，
 * 原实现是 6 个裸 <button>，读屏软件不知道这是标签页。
 */
import UiIcon from './UiIcon.vue'

defineProps({
  modelValue: { type: String, required: true },
  /** [{ key, label, icon? }] */
  tabs: { type: Array, required: true },
  /** underline=下划线式（页面级） pill=胶囊式（卡片内） */
  variant: {
    type: String,
    default: 'underline',
    validator: (v) => ['underline', 'pill'].includes(v),
  },
})

const emit = defineEmits(['update:modelValue'])
</script>

<template>
  <div class="ui-tabs" :class="`is-${variant}`" role="tablist">
    <button
      v-for="tab in tabs"
      :key="tab.key"
      class="ui-tab"
      :class="{ 'is-active': tab.key === modelValue }"
      type="button"
      role="tab"
      :aria-selected="tab.key === modelValue"
      @click="emit('update:modelValue', tab.key)"
    >
      <UiIcon v-if="tab.icon" :name="tab.icon" :size="15" />
      <span>{{ tab.label }}</span>
      <span v-if="tab.badge != null" class="ui-tab-badge">{{ tab.badge }}</span>
    </button>
  </div>
</template>

<style scoped>
.ui-tabs {
  display: flex;
  align-items: center;
  gap: var(--sp-1);
}

.ui-tab {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-2);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--c-text-2);
  white-space: nowrap;
  transition: color var(--dur-fast) var(--ease),
    background var(--dur-fast) var(--ease), border-color var(--dur-fast) var(--ease);
}
.ui-tab:hover {
  color: var(--c-text);
}

.ui-tab-badge {
  padding: 0 6px;
  font-size: var(--fs-xs);
  font-family: var(--font-mono);
  color: var(--c-text-2);
  background: var(--c-surface-3);
  border-radius: var(--r-full);
}
.ui-tab.is-active .ui-tab-badge {
  color: var(--c-accent-text);
  background: var(--c-accent-soft);
}

/* 下划线式：靠 2px 底线表示选中，不靠底色 */
.ui-tabs.is-underline {
  gap: var(--sp-5);
  border-bottom: 1px solid var(--c-border);
}
.ui-tabs.is-underline .ui-tab {
  padding: var(--sp-3) 0;
  border-bottom: 2px solid transparent;
  margin-bottom: -1px;
}
.ui-tabs.is-underline .ui-tab.is-active {
  color: var(--c-accent-text);
  border-bottom-color: var(--c-accent);
}

/* 胶囊式：卡片内的次级切换 */
.ui-tabs.is-pill {
  padding: 2px;
  background: var(--c-surface-2);
  border-radius: var(--r-md);
}
.ui-tabs.is-pill .ui-tab {
  padding: var(--sp-1) var(--sp-3);
  border-radius: var(--r-sm);
}
.ui-tabs.is-pill .ui-tab.is-active {
  color: var(--c-text);
  background: var(--c-surface);
  box-shadow: var(--sh-1);
}
</style>
