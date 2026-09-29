<script setup>
/**
 * 对话框
 * ======
 *
 * 改造前有两套：全局 `.modal-*`（对话页删除确认用）和 GuideModal 自成一套
 * `.guide-*`。连 `fadeIn`/`scaleIn` 两个关键帧都在两个文件里各定义一次
 * —— scoped 的 keyframes 不共享，浏览器里真的是两份。
 *
 * 无障碍要点（原实现缺的）：
 * - 挂到 body 下，避免被父级 overflow/transform 裁掉
 * - Esc 关闭
 * - 打开时锁滚动
 * - 打开后焦点移进对话框，关闭后还给触发元素
 */
import { onBeforeUnmount, ref, watch, nextTick } from 'vue'
import UiIcon from './UiIcon.vue'

const props = defineProps({
  open: { type: Boolean, default: false },
  title: { type: String, default: '' },
  size: {
    type: String,
    default: 'md',
    validator: (v) => ['sm', 'md', 'lg'].includes(v),
  },
  /** 点遮罩关闭；破坏性确认框建议关掉，避免误点走掉 */
  closeOnOverlay: { type: Boolean, default: true },
})

const emit = defineEmits(['close'])
const dialogRef = ref(null)
let lastFocused = null

function onKeydown(e) {
  if (e.key === 'Escape') emit('close')
}

watch(
  () => props.open,
  async (isOpen) => {
    if (isOpen) {
      lastFocused = document.activeElement
      document.body.style.overflow = 'hidden'
      window.addEventListener('keydown', onKeydown)
      await nextTick()
      // 焦点进到对话框内的第一个可聚焦元素，读屏软件才会读到标题
      dialogRef.value?.querySelector(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
      )?.focus()
    } else {
      document.body.style.overflow = ''
      window.removeEventListener('keydown', onKeydown)
      lastFocused?.focus?.()
      lastFocused = null
    }
  },
)

onBeforeUnmount(() => {
  document.body.style.overflow = ''
  window.removeEventListener('keydown', onKeydown)
})
</script>

<template>
  <Teleport to="body">
    <Transition name="ui-modal">
      <div
        v-if="open"
        class="ui-modal-overlay"
        @click.self="closeOnOverlay && emit('close')"
      >
        <div
          ref="dialogRef"
          class="ui-modal"
          :class="`is-${size}`"
          role="dialog"
          aria-modal="true"
          :aria-label="title || undefined"
        >
          <header v-if="title || $slots.header" class="ui-modal-header">
            <slot name="header">
              <h3 class="ui-modal-title">{{ title }}</h3>
            </slot>
            <button class="ui-modal-close" type="button" aria-label="关闭" @click="emit('close')">
              <UiIcon name="close" :size="16" />
            </button>
          </header>
          <div class="ui-modal-body">
            <slot />
          </div>
          <footer v-if="$slots.footer" class="ui-modal-footer">
            <slot name="footer" />
          </footer>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.ui-modal-overlay {
  position: fixed;
  inset: 0;
  z-index: var(--z-modal);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--sp-4);
  background: rgba(24, 24, 27, 0.45);
}

.ui-modal {
  display: flex;
  flex-direction: column;
  width: 100%;
  max-height: 88vh;
  background: var(--c-surface);
  border-radius: var(--r-xl);
  box-shadow: var(--sh-4);
}
.ui-modal.is-sm {
  max-width: 400px;
}
.ui-modal.is-md {
  max-width: 560px;
}
.ui-modal.is-lg {
  max-width: 720px;
}

.ui-modal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-4);
  padding: var(--sp-5) var(--sp-5) var(--sp-3);
}
.ui-modal-title {
  font-size: var(--fs-lg);
  font-weight: var(--fw-semibold);
}
.ui-modal-close {
  display: inline-flex;
  padding: var(--sp-1);
  color: var(--c-text-3);
  border-radius: var(--r-sm);
  transition: color var(--dur-fast) var(--ease), background var(--dur-fast) var(--ease);
}
.ui-modal-close:hover {
  color: var(--c-text);
  background: var(--c-surface-2);
}

.ui-modal-body {
  flex: 1;
  overflow-y: auto;
  padding: 0 var(--sp-5) var(--sp-5);
}
.ui-modal-footer {
  display: flex;
  justify-content: flex-end;
  gap: var(--sp-2);
  padding: var(--sp-4) var(--sp-5);
  border-top: 1px solid var(--c-border);
}

/* 过渡：遮罩淡入、对话框轻微上浮，比原来的缩放更稳 */
.ui-modal-enter-active,
.ui-modal-leave-active {
  transition: opacity var(--dur-base) var(--ease);
}
.ui-modal-enter-active .ui-modal,
.ui-modal-leave-active .ui-modal {
  transition: transform var(--dur-base) var(--ease);
}
.ui-modal-enter-from,
.ui-modal-leave-to {
  opacity: 0;
}
.ui-modal-enter-from .ui-modal,
.ui-modal-leave-to .ui-modal {
  transform: translateY(8px);
}

@media (max-width: 480px) {
  .ui-modal-overlay {
    align-items: flex-end;
    padding: 0;
  }
  .ui-modal {
    max-width: none;
    max-height: 92vh;
    border-radius: var(--r-xl) var(--r-xl) 0 0;
  }
}
</style>
