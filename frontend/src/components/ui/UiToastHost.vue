<script setup>
/**
 * Toast 渲染宿主
 *
 * 挂在 App 根部一次，消费 useToast 的单例队列。
 * 用 TransitionGroup 让多条提示错开进出，而不是硬切。
 */
import { useToast } from '../../composables/useToast.js'
import UiAlert from './UiAlert.vue'

const { items, dismiss } = useToast()
</script>

<template>
  <Teleport to="body">
    <div class="ui-toast-host" role="region" aria-label="通知">
      <TransitionGroup name="ui-toast">
        <UiAlert
          v-for="t in items"
          :key="t.id"
          :tone="t.tone"
          class="ui-toast"
          @click="dismiss(t.id)"
        >
          {{ t.message }}
        </UiAlert>
      </TransitionGroup>
    </div>
  </Teleport>
</template>

<style scoped>
.ui-toast-host {
  position: fixed;
  top: calc(var(--header-h) + var(--sp-3));
  right: var(--sp-4);
  z-index: var(--z-toast);
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  width: min(360px, calc(100vw - var(--sp-8)));
  pointer-events: none;
}

.ui-toast {
  pointer-events: auto;
  cursor: pointer;
  box-shadow: var(--sh-3);
}

.ui-toast-enter-active,
.ui-toast-leave-active {
  transition: opacity var(--dur-base) var(--ease),
    transform var(--dur-base) var(--ease);
}
.ui-toast-enter-from {
  opacity: 0;
  transform: translateX(16px);
}
.ui-toast-leave-to {
  opacity: 0;
  transform: translateX(16px);
}
.ui-toast-leave-active {
  position: absolute;
  right: 0;
  left: 0;
}

@media (max-width: 480px) {
  .ui-toast-host {
    right: var(--sp-3);
    left: var(--sp-3);
    width: auto;
  }
}
</style>
