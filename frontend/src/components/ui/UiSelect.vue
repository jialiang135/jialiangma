<script setup>
/**
 * 下拉选择
 *
 * 改造前是评测页里裸写的 `.eval-field select`，没有任何样式约定。
 */
import { computed } from 'vue'
import UiIcon from './UiIcon.vue'

const props = defineProps({
  modelValue: { type: [String, Number], default: '' },
  /** [{ value, label }] */
  options: { type: Array, default: () => [] },
  size: {
    type: String,
    default: 'md',
    validator: (v) => ['sm', 'md'].includes(v),
  },
  disabled: { type: Boolean, default: false },
})

const emit = defineEmits(['update:modelValue'])

const value = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})
</script>

<template>
  <div class="ui-select" :class="`is-${size}`">
    <select v-model="value" class="ui-select-native" :disabled="disabled">
      <option v-for="opt in options" :key="opt.value" :value="opt.value">
        {{ opt.label }}
      </option>
    </select>
    <UiIcon name="chevron-down" :size="14" class="ui-select-arrow" />
  </div>
</template>

<style scoped>
.ui-select {
  position: relative;
  display: inline-flex;
  align-items: center;
}
.ui-select.is-md {
  min-width: 160px;
}
.ui-select.is-sm {
  min-width: 120px;
}

.ui-select-native {
  width: 100%;
  appearance: none; /* 去掉浏览器原生箭头，换成自己的图标 */
  color: var(--c-text);
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--r-md);
  cursor: pointer;
  transition: border-color var(--dur-fast) var(--ease);
}
.ui-select.is-md .ui-select-native {
  height: 36px;
  padding: 0 var(--sp-8) 0 var(--sp-3);
  font-size: var(--fs-base);
}
.ui-select.is-sm .ui-select-native {
  height: 28px;
  padding: 0 var(--sp-6) 0 var(--sp-2);
  font-size: var(--fs-sm);
}

.ui-select-native:hover:not(:disabled) {
  border-color: var(--c-border-strong);
}
.ui-select-native:focus {
  outline: none;
  border-color: var(--c-accent);
  box-shadow: 0 0 0 3px var(--c-accent-soft);
}
.ui-select-native:disabled {
  color: var(--c-text-3);
  background: var(--c-surface-2);
  cursor: not-allowed;
}

.ui-select-arrow {
  position: absolute;
  right: var(--sp-2);
  color: var(--c-text-3);
  pointer-events: none;
}
</style>
