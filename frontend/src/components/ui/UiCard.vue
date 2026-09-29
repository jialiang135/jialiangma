<script setup>
/**
 * 卡片
 * ====
 *
 * 改造前全项目**没有 `.card` 类**，但同样的"白底 + 1px 描边 + 圆角 + 极浅阴影"
 * 被复制成了 9 个不同的类（.kb-upload-area / .chat-messages / .eval-panel /
 * .metric-card / .stat-card / .quick-card / .log-card / .ts-total-card…）。
 * 改一次圆角要改九个地方。
 */
defineProps({
  /** 是否加内边距（列表类卡片常自己做 padding，所以可关） */
  padded: { type: Boolean, default: true },
  /** 可点击：加 hover 反馈和指针 */
  interactive: { type: Boolean, default: false },
})
</script>

<template>
  <div class="ui-card" :class="{ 'is-padded': padded, 'is-interactive': interactive }">
    <header v-if="$slots.header" class="ui-card-header">
      <slot name="header" />
    </header>
    <slot />
    <footer v-if="$slots.footer" class="ui-card-footer">
      <slot name="footer" />
    </footer>
  </div>
</template>

<style scoped>
.ui-card {
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--r-lg);
  box-shadow: var(--sh-1);
}
.ui-card.is-padded {
  padding: var(--sp-5);
}
.ui-card.is-interactive {
  cursor: pointer;
  transition: border-color var(--dur-fast) var(--ease),
    box-shadow var(--dur-fast) var(--ease);
}
.ui-card.is-interactive:hover {
  border-color: var(--c-border-strong);
  box-shadow: var(--sh-2);
}

.ui-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-3);
  margin-bottom: var(--sp-4);
}
.ui-card-footer {
  margin-top: var(--sp-4);
  padding-top: var(--sp-4);
  border-top: 1px solid var(--c-border);
}
</style>
