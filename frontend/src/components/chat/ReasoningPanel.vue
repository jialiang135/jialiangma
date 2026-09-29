<script setup>
/**
 * 推理过程面板
 * ============
 *
 * 两条数据来源，语义完全不同，改造前混在一个滚动区里：
 * - `steps`    —— 流程步骤（检索到了什么、调了哪个工具、生成完成），是**给用户看的**
 * - `thinking` —— 推理模型的内心独白，通常是英文、又长又碎，是**给调试看的**
 *
 * 所以这里把 thinking 收进自己的二级折叠里，默认不展开 ——
 * 它不该在默认视图里和步骤抢注意力（改造前面板里一大块英文思考流，
 * 占的垂直空间比答案本身还多）。
 *
 * 关于「展开 / 收起」：
 * 改造前这个文案是用 CSS `content:` + `transform: rotate(180deg)` 做的，
 * 结果把文字本身也旋转了 180°，显示出来是反的。
 * 现在由状态直接决定图标和文案，**不做任何 CSS 变换** —— 从根上杜绝那类 bug。
 */
import { ref } from 'vue'

import UiIcon from '../ui/UiIcon.vue'

const props = defineProps({
  /** [{ iconName, text }] */
  steps: { type: Array, default: () => [] },
  thinking: { type: String, default: '' },
  /** 流式中：面板默认展开，让用户看到"正在干什么" */
  streaming: { type: Boolean, default: false },
  /** 最新一条消息默认展开 */
  defaultOpen: { type: Boolean, default: false },
})

const open = ref(props.defaultOpen || props.streaming)
const thinkingOpen = ref(false)

const onToggle = (e) => {
  open.value = e.target.open
}
</script>

<template>
  <details class="reasoning" :open="open" @toggle="onToggle">
    <summary class="reasoning-summary">
      <UiIcon name="zap" :size="15" class="reasoning-icon" />
      <span class="reasoning-label">
        {{ open ? '收起推理过程' : '查看推理过程' }}
        <span v-if="steps.length" class="reasoning-count">{{ steps.length }} 步</span>
      </span>
      <UiIcon :name="open ? 'chevron-down' : 'chevron-right'" :size="15" class="reasoning-chevron" />
    </summary>

    <div class="reasoning-body">
      <!-- 模型思考：默认收起，避免又长又碎的英文独白淹没步骤 -->
      <div v-if="thinking" class="thinking">
        <button class="thinking-toggle" type="button" @click="thinkingOpen = !thinkingOpen">
          <UiIcon :name="thinkingOpen ? 'chevron-down' : 'chevron-right'" :size="13" />
          <span>模型思考</span>
        </button>
        <pre v-if="thinkingOpen" class="thinking-text">{{ thinking }}</pre>
      </div>

      <ol v-if="steps.length" class="steps">
        <li v-for="(step, i) in steps" :key="i" class="step">
          <UiIcon :name="step.iconName" :size="14" class="step-icon" />
          <span class="step-text">{{ step.text }}</span>
        </li>
      </ol>
      <p v-else class="steps-empty">本次没有记录到步骤。</p>
    </div>
  </details>
</template>

<style scoped>
.reasoning {
  margin-top: var(--sp-3);
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--r-md);
  overflow: hidden;
}

.reasoning-summary {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--c-text-2);
  cursor: pointer;
  list-style: none;
  transition: background var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease);
}
/* 去掉浏览器给 summary 的默认三角，用自己的图标 */
.reasoning-summary::-webkit-details-marker {
  display: none;
}
.reasoning-summary:hover {
  color: var(--c-text);
  background: var(--c-surface-2);
}
.reasoning[open] .reasoning-summary {
  color: var(--c-reasoning);
  border-bottom: 1px solid var(--c-border);
}

.reasoning-icon {
  flex-shrink: 0;
}
.reasoning-label {
  flex: 1;
  min-width: 0;
}
.reasoning-count {
  margin-left: var(--sp-1);
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  color: var(--c-text-3);
}
.reasoning-chevron {
  color: var(--c-text-3);
}

.reasoning-body {
  padding: var(--sp-3);
}

/* —— 模型思考 —— */
.thinking {
  margin-bottom: var(--sp-3);
  padding-bottom: var(--sp-3);
  border-bottom: 1px dashed var(--c-border);
}
.thinking-toggle {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
  font-size: var(--fs-xs);
  color: var(--c-text-3);
}
.thinking-toggle:hover {
  color: var(--c-text-2);
}
.thinking-text {
  max-height: 220px;
  margin-top: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  overflow-y: auto;
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  line-height: var(--lh-base);
  color: var(--c-text-2);
  white-space: pre-wrap;
  word-break: break-word;
  background: var(--c-surface-2);
  border-radius: var(--r-sm);
}

/* —— 步骤时间线 —— */
.steps {
  position: relative;
  padding-left: var(--sp-3);
  border-left: 2px solid var(--c-border);
}
.step {
  display: flex;
  align-items: flex-start;
  gap: var(--sp-2);
  padding: var(--sp-1) 0;
  font-size: var(--fs-sm);
  color: var(--c-text-2);
  line-height: var(--lh-base);
}
.step-icon {
  margin-top: 3px;
  color: var(--c-text-3);
}
.step-text {
  min-width: 0;
  word-break: break-word;
}
.steps-empty {
  font-size: var(--fs-sm);
  color: var(--c-text-3);
}
</style>
