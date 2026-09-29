<script setup>
/**
 * 进度条
 * ======
 *
 * 抽出来的直接原因：知识库页（上传任务）和评测页（评测进度）**各自手写了一个**，
 * 类名不同（`.bar-fill` vs `.progress-fill`）、状态类的命名不同、shimmer 动画
 * 只有一边有 —— 典型的"东写一个西写一个"。
 *
 * 而且改造前评测页那个进度条是**坏的**：它用的 `.progress-bar-track` /
 * `.progress-bar-fill` 只定义在 KnowledgeView 的 `<style scoped>` 里，
 * Vue 的 scoped 选择器带 `[data-v-*]`，命中不了别的组件，所以那一版是裸 div。
 * 收成一个组件之后，这类问题从根上不会再出现。
 */
import { computed } from 'vue'

const props = defineProps({
  /** 0~100 */
  value: { type: Number, default: 0 },
  /** accent=进行中 success=完成 danger=失败 neutral=已跳过（终态但非失败） */
  tone: {
    type: String,
    default: 'accent',
    validator: (v) => ['accent', 'success', 'danger', 'neutral'].includes(v),
  },
  /** 正在跑：叠一层流动高光，让"没动"和"在动"在视觉上分得开 */
  active: { type: Boolean, default: false },
  size: {
    type: String,
    default: 'md',
    validator: (v) => ['sm', 'md'].includes(v),
  },
  /** 无障碍标签，例如"上传进度" */
  label: { type: String, default: '' },
})

/** 夹取到 0~100：后端偶尔会给超出范围或 NaN 的值 */
const pct = computed(() => {
  const n = Number(props.value)
  if (!Number.isFinite(n)) return 0
  return Math.min(100, Math.max(0, n))
})
</script>

<template>
  <div
    class="ui-progress"
    :class="[`is-${size}`, `is-${tone}`]"
    role="progressbar"
    :aria-valuenow="pct"
    aria-valuemin="0"
    aria-valuemax="100"
    :aria-label="label || undefined"
  >
    <div class="ui-progress-fill" :style="{ width: `${pct}%` }">
      <span v-if="active" class="ui-progress-shimmer" aria-hidden="true" />
    </div>
  </div>
</template>

<style scoped>
.ui-progress {
  width: 100%;
  overflow: hidden;
  background: var(--c-surface-3);
  border-radius: var(--r-full);
}
.ui-progress.is-md {
  height: 8px;
}
.ui-progress.is-sm {
  height: 4px;
}

.ui-progress-fill {
  position: relative;
  height: 100%;
  overflow: hidden;
  border-radius: var(--r-full);
  transition: width var(--dur-base) var(--ease);
}
.ui-progress.is-accent .ui-progress-fill {
  background: var(--c-accent);
}
.ui-progress.is-success .ui-progress-fill {
  background: var(--c-success);
}
.ui-progress.is-danger .ui-progress-fill {
  background: var(--c-danger);
}
.ui-progress.is-neutral .ui-progress-fill {
  background: var(--c-text-3);
}

/* 流动高光：透明 → 白 → 透明，从做到右扫过 */
.ui-progress-shimmer {
  position: absolute;
  inset: 0;
  background: linear-gradient(
    90deg,
    transparent 0%,
    rgba(255, 255, 255, 0.45) 50%,
    transparent 100%
  );
  animation: ui-progress-sweep 1.4s ease-in-out infinite;
}

@keyframes ui-progress-sweep {
  from {
    transform: translateX(-100%);
  }
  to {
    transform: translateX(100%);
  }
}

@media (prefers-reduced-motion: reduce) {
  .ui-progress-shimmer {
    animation: none;
  }
}
</style>
