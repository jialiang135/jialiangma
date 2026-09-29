<script setup>
/**
 * 输入区
 * ======
 *
 * 从 ChatView 里抽出来。要点：
 * - 回车发送、Shift+Enter 换行
 * - 高度随内容自动增长，但有上限（超过后内部滚动，不把消息区顶没）
 * - 生成中显示「停止」，替换发送按钮 —— 而不是额外并排一个按钮
 */
import { nextTick, ref } from 'vue'

import UiIcon from '../ui/UiIcon.vue'

const props = defineProps({
  modelValue: { type: String, default: '' },
  streaming: { type: Boolean, default: false },
  disabled: { type: Boolean, default: false },
  placeholder: { type: String, default: '输入你的问题，回车发送' },
})

const emit = defineEmits(['update:modelValue', 'send', 'stop'])

const textareaRef = ref(null)

const MAX_HEIGHT = 160

function autoResize() {
  const el = textareaRef.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = `${Math.min(el.scrollHeight, MAX_HEIGHT)}px`
}

async function onInput(e) {
  emit('update:modelValue', e.target.value)
  await nextTick()
  autoResize()
}

function submit() {
  const text = String(props.modelValue || '').trim()
  if (!text || props.streaming || props.disabled) return
  emit('send', text)
  // 清空后要重新量高度，否则输入框停在旧高度
  nextTick(() => {
    if (textareaRef.value) textareaRef.value.style.height = 'auto'
  })
}

/** 只有「回车且没按 Shift」才发送 */
function onKeydown(e) {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault()
    submit()
  }
}
</script>

<template>
  <div class="composer">
    <textarea
      ref="textareaRef"
      class="composer-input"
      :value="modelValue"
      :placeholder="placeholder"
      :disabled="disabled"
      rows="1"
      @input="onInput"
      @keydown="onKeydown"
    />
    <button
      v-if="streaming"
      class="composer-btn is-stop"
      type="button"
      title="停止生成"
      @click="emit('stop')"
    >
      <UiIcon name="stop" :size="16" />
    </button>
    <button
      v-else
      class="composer-btn is-send"
      type="button"
      title="发送"
      :disabled="!modelValue.trim() || disabled"
      @click="submit"
    >
      <UiIcon name="send" :size="16" />
    </button>
  </div>
</template>

<style scoped>
.composer {
  display: flex;
  align-items: flex-end;
  gap: var(--sp-2);
  padding: var(--sp-2);
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--r-lg);
  transition: border-color var(--dur-fast) var(--ease),
    box-shadow var(--dur-fast) var(--ease);
}
/* 焦点环落在整个容器上，而不是内层 textarea ——
   视觉上输入区是一个整体，只给 textarea 描边会显得碎 */
.composer:focus-within {
  border-color: var(--c-accent);
  box-shadow: 0 0 0 3px var(--c-accent-soft);
}

.composer-input {
  flex: 1;
  min-height: 24px;
  max-height: 160px;
  padding: var(--sp-2);
  font-size: var(--fs-base);
  line-height: var(--lh-base);
  color: var(--c-text);
  background: none;
  border: none;
  outline: none;
  resize: none;
}
.composer-input::placeholder {
  color: var(--c-text-3);
}
.composer-input:disabled {
  cursor: not-allowed;
}

.composer-btn {
  display: inline-flex;
  flex-shrink: 0;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  color: var(--c-white);
  border-radius: var(--r-md);
  transition: background var(--dur-fast) var(--ease), opacity var(--dur-fast) var(--ease);
}
.composer-btn.is-send {
  background: var(--c-accent);
}
.composer-btn.is-send:hover:not(:disabled) {
  background: var(--c-accent-hover);
}
.composer-btn.is-send:disabled {
  opacity: 0.4;
}
.composer-btn.is-stop {
  background: var(--c-text);
}
.composer-btn.is-stop:hover {
  background: var(--c-danger);
}
</style>
