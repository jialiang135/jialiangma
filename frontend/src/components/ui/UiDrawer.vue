<script setup>
/**
 * 抽屉（右侧滑出）
 * ================
 *
 * 为什么不复用 UiModal：模态是"打断式"的 —— 盖住页面、要求你先处理它。
 * 而"在列表里点开某个文件看一眼内容"是**查阅式**的：你要一边看列表一边看内容，
 * 看完随手关掉。用模态会把列表整个盖住，关掉又得重新滚回去。
 *
 * 无障碍要点与 UiModal 一致（这套是改造时定的规范，新组件照办）：
 * - 挂到 body 下，避免被父级 overflow/transform 裁掉
 * - Esc 关闭、点遮罩关闭（可关）
 * - 打开时锁滚动
 * - 打开后焦点移进抽屉，关闭后还给触发元素
 * - role="dialog" + aria-modal，读屏软件才知道这是弹层
 */
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'

import UiIcon from './UiIcon.vue'

const props = defineProps({
  open: { type: Boolean, default: false },
  title: { type: String, default: '' },
  /** 宽度档位：内容型（切片列表）用 lg，短表单用 md */
  size: {
    type: String,
    default: 'md',
    validator: (v) => ['md', 'lg'].includes(v),
  },
  closeOnOverlay: { type: Boolean, default: true },
})

const emit = defineEmits(['close'])
const drawerRef = ref(null)
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
      drawerRef.value?.querySelector(
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
    <Transition name="ui-drawer">
      <div
        v-if="open"
        class="ui-drawer-overlay"
        @click.self="closeOnOverlay && emit('close')"
      >
        <aside
          ref="drawerRef"
          class="ui-drawer"
          :class="`is-${size}`"
          role="dialog"
          aria-modal="true"
          :aria-label="title || undefined"
        >
          <header v-if="title || $slots.header" class="ui-drawer-header">
            <slot name="header">
              <h3 class="ui-drawer-title">{{ title }}</h3>
            </slot>
            <button
              class="ui-drawer-close"
              type="button"
              aria-label="关闭"
              @click="emit('close')"
            >
              <UiIcon name="close" :size="16" />
            </button>
          </header>
          <div class="ui-drawer-body">
            <slot />
          </div>
          <footer v-if="$slots.footer" class="ui-drawer-footer">
            <slot name="footer" />
          </footer>
        </aside>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.ui-drawer-overlay {
  position: fixed;
  inset: 0;
  z-index: var(--z-drawer);
  display: flex;
  justify-content: flex-end;
  background: rgba(24, 24, 27, 0.45);
}

.ui-drawer {
  display: flex;
  flex-direction: column;
  width: 100%;
  height: 100%;
  background: var(--c-surface);
  box-shadow: var(--sh-4);
  /* 左侧圆角，视觉上"贴"在屏幕右边 */
  border-radius: var(--r-xl) 0 0 var(--r-xl);
}
.ui-drawer.is-md {
  max-width: 480px;
}
.ui-drawer.is-lg {
  max-width: 720px;
}

.ui-drawer-header {
  display: flex;
  flex-shrink: 0;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-4);
  padding: var(--sp-4) var(--sp-5);
  border-bottom: 1px solid var(--c-border);
}
.ui-drawer-title {
  min-width: 0;
  overflow: hidden;
  font-size: var(--fs-lg);
  font-weight: var(--fw-semibold);
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ui-drawer-close {
  display: inline-flex;
  flex-shrink: 0;
  padding: var(--sp-1);
  color: var(--c-text-3);
  border-radius: var(--r-sm);
  transition: color var(--dur-fast) var(--ease), background var(--dur-fast) var(--ease);
}
.ui-drawer-close:hover {
  color: var(--c-text);
  background: var(--c-surface-2);
}

.ui-drawer-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}

.ui-drawer-footer {
  display: flex;
  flex-shrink: 0;
  justify-content: flex-end;
  gap: var(--sp-2);
  padding: var(--sp-4) var(--sp-5);
  border-top: 1px solid var(--c-border);
}

/* 过渡：遮罩淡入 + 抽屉从右滑入 */
.ui-drawer-enter-active,
.ui-drawer-leave-active {
  transition: opacity var(--dur-base) var(--ease);
}
.ui-drawer-enter-active .ui-drawer,
.ui-drawer-leave-active .ui-drawer {
  transition: transform var(--dur-base) var(--ease);
}
.ui-drawer-enter-from,
.ui-drawer-leave-to {
  opacity: 0;
}
.ui-drawer-enter-from .ui-drawer,
.ui-drawer-leave-to .ui-drawer {
  transform: translateX(16px);
}

/* 窄屏：铺满整屏，否则内容区被压得只剩几百像素 */
@media (max-width: 768px) {
  .ui-drawer.is-md,
  .ui-drawer.is-lg {
    max-width: none;
    border-radius: 0;
  }
}
</style>
