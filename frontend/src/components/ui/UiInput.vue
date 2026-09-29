<script setup>
/**
 * 文本输入框
 * ==========
 *
 * 改造前有 4 套：`.input` / `.input-sm`、`.input-xs`（AdminView 又覆盖一遍
 * 宽度和边框色）、`.chat-input`（历史对话栏里的行内输入）、
 * 以及评测页 `.eval-field input`。同一个"输入框"四种高度和边框。
 */
import { computed } from 'vue'

const props = defineProps({
  modelValue: { type: [String, Number], default: '' },
  size: {
    type: String,
    default: 'md',
    validator: (v) => ['sm', 'md'].includes(v),
  },
  type: { type: String, default: 'text' },
  placeholder: { type: String, default: '' },
  disabled: { type: Boolean, default: false },
  /** 等宽字体：文件名、ID 这类要看清字符的输入 */
  mono: { type: Boolean, default: false },
})

const emit = defineEmits(['update:modelValue', 'enter'])

const value = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})
</script>

<template>
  <input
    v-model="value"
    class="ui-input"
    :class="[`is-${size}`, { 'is-mono': mono }]"
    :type="type"
    :placeholder="placeholder"
    :disabled="disabled"
    @keydown.enter="emit('enter')"
  />
</template>

<style scoped>
.ui-input {
  width: 100%;
  color: var(--c-text);
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--r-md);
  transition: border-color var(--dur-fast) var(--ease),
    box-shadow var(--dur-fast) var(--ease);
}
.ui-input.is-md {
  height: 36px;
  padding: 0 var(--sp-3);
  font-size: var(--fs-base);
}
.ui-input.is-sm {
  height: 28px;
  padding: 0 var(--sp-2);
  font-size: var(--fs-sm);
}
.ui-input.is-mono {
  font-family: var(--font-mono);
  font-size: var(--fs-sm);
}

.ui-input::placeholder {
  color: var(--c-text-3);
}

.ui-input:hover:not(:disabled) {
  border-color: var(--c-border-strong);
}
.ui-input:focus {
  outline: none;
  border-color: var(--c-accent);
  box-shadow: 0 0 0 3px var(--c-accent-soft);
}
.ui-input:disabled {
  color: var(--c-text-3);
  background: var(--c-surface-2);
  cursor: not-allowed;
}
</style>
