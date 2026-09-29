<script setup>
/**
 * 加载指示器
 *
 * 改造前"加载中"有 4 套写法（`.loading` 文字、`.ts-loading`、
 * 按钮内联的"上传中/刷新中/重建中"文字、`...`）。统一成一个转圈。
 */
defineProps({
  size: { type: [Number, String], default: 16 },
  label: { type: String, default: '' },
})
</script>

<template>
  <span class="ui-spinner" role="status" :aria-label="label || '加载中'">
    <svg class="ui-spinner-svg" :width="size" :height="size" viewBox="0 0 24 24" fill="none">
      <circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="2.5" opacity="0.2" />
      <path
        d="M21 12a9 9 0 0 0-9-9"
        stroke="currentColor"
        stroke-width="2.5"
        stroke-linecap="round"
      />
    </svg>
  </span>
</template>

<style scoped>
.ui-spinner {
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.ui-spinner-svg {
  animation: spin 0.7s linear infinite;
}
@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
