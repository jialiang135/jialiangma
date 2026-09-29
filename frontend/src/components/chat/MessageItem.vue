<script setup>
/**
 * 单条消息
 * ========
 *
 * 改造前消息的渲染、朗读按钮、推理面板、快捷问题全平铺在 ChatView 的模板里，
 * 一个 100 多行的 v-for 块里套了三层条件。拆出来之后每一块都能单独看、单独改。
 *
 * 顺序是刻意的：**答案 → 证据 → 推理**。
 * 用户最关心的是答案，其次是"这个答案的依据是什么"（证据轨），
 * 最后才是我怎么想出来的（推理过程）。改造前推理面板紧跟答案、
 * 默认展开、占掉半屏，把主次完全弄反了。
 */
import { ref } from 'vue'

import { renderMarkdown } from '../../utils/markdown.js'
import UiIcon from '../ui/UiIcon.vue'
import EvidenceRail from './EvidenceRail.vue'
import ReasoningPanel from './ReasoningPanel.vue'

const props = defineProps({
  message: { type: Object, required: true },
  /** 这条正在朗读 */
  speaking: { type: Boolean, default: false },
  /** 朗读失败的原因（只在该条正在朗读时有值） */
  speakError: { type: String, default: '' },
  /** 是否默认展开推理面板（给最新一条用） */
  expandReasoning: { type: Boolean, default: false },
  /** 正在流式生成中的临时消息：隐藏操作按钮、显示光标 */
  draft: { type: Boolean, default: false },
})

const emit = defineEmits(['speak', 'stop-speak'])

const copied = ref(false)
let copyTimer = null

async function copyContent() {
  try {
    await navigator.clipboard.writeText(props.message.content || '')
    copied.value = true
    clearTimeout(copyTimer)
    copyTimer = setTimeout(() => {
      copied.value = false
    }, 1600)
  } catch {
    // 剪贴板不可用（非 HTTPS 或用户拒绝）：静默失败，不值得打断对话
  }
}

const isAssistant = () => props.message.role === 'assistant'
</script>

<template>
  <article class="msg" :class="`is-${message.role}`">
    <div class="avatar">
      <UiIcon :name="isAssistant() ? 'zap' : 'user'" :size="16" />
    </div>

    <div class="body">
      <!-- 助手消息渲染 Markdown；用户消息按纯文本显示，
           保留换行但**不解析** —— 用户输入里的星号/井号是字面意思 -->
      <template v-if="isAssistant()">
        <div
          v-if="message.content"
          class="md-body"
          :class="{ 'is-streaming': draft }"
          v-html="renderMarkdown(message.content)"
        />
        <!-- 一个字都还没出来时给个进度感，而不是一片空白 -->
        <div v-else-if="draft" class="pending">
          <span class="pending-dot" />
          <span>正在生成…</span>
        </div>
      </template>
      <p v-else class="user-text">{{ message.content }}</p>

      <!-- 证据轨：产品差异化的地方，放在答案正下方 -->
      <EvidenceRail
        v-if="isAssistant() && message.evidence?.length"
        :items="message.evidence"
      />

      <div v-if="isAssistant() && !draft" class="actions">
        <button
          class="action"
          :class="{ 'is-active': speaking }"
          type="button"
          @click="speaking ? emit('stop-speak') : emit('speak')"
        >
          <UiIcon :name="speaking ? 'stop' : 'volume'" :size="14" />
          <span>{{ speaking ? '停止' : '朗读' }}</span>
        </button>

        <button class="action" type="button" @click="copyContent">
          <UiIcon :name="copied ? 'check' : 'copy'" :size="14" />
          <span>{{ copied ? '已复制' : '复制' }}</span>
        </button>

        <span v-if="speakError" class="speak-error">
          <UiIcon name="alert" :size="13" />
          {{ speakError }}
        </span>
      </div>

      <ReasoningPanel
        v-if="isAssistant() && (message.steps?.length || message.thinking)"
        :steps="message.steps || []"
        :thinking="message.thinking || ''"
        :default-open="expandReasoning"
      />
    </div>
  </article>
</template>

<style scoped>
.msg {
  display: flex;
  gap: var(--sp-3);
  margin-bottom: var(--sp-6);
}
.msg.is-user {
  flex-direction: row-reverse;
}

/* 头像用统一图标而不是 emoji：emoji 头像在不同系统上长得不一样，
   而且和"数字分身"这个身份没有语义关联 */
.avatar {
  display: flex;
  flex-shrink: 0;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  color: var(--c-text-2);
  background: var(--c-surface-2);
  border: 1px solid var(--c-border);
  border-radius: var(--r-md);
}
.msg.is-assistant .avatar {
  color: var(--c-accent-text);
  background: var(--c-accent-soft);
  border-color: var(--c-accent-border);
}

.body {
  min-width: 0;
  max-width: 100%;
}
.msg.is-user .body {
  max-width: 80%;
}

/* 用户消息给个气泡；助手消息**不给底色** ——
   回答是要读的长文，套在灰盒子里会压迫阅读。这是成熟对话产品的通用做法。 */
.user-text {
  padding: var(--sp-2) var(--sp-3);
  font-size: var(--fs-base);
  line-height: var(--lh-base);
  color: var(--c-white);
  white-space: pre-wrap;
  word-break: break-word;
  background: var(--c-accent);
  border-radius: var(--r-lg) var(--r-lg) var(--r-sm) var(--r-lg);
}

.actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-1);
  margin-top: var(--sp-2);
}

/* 流式生成中：末尾跟一个闪烁光标，表示"还在出字" */
.md-body.is-streaming > :last-child::after {
  display: inline-block;
  width: 2px;
  height: 1em;
  margin-left: 2px;
  vertical-align: text-bottom;
  content: '';
  background: var(--c-accent);
  animation: caret 1s step-end infinite;
}
@keyframes caret {
  50% {
    opacity: 0;
  }
}

.pending {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-2);
  font-size: var(--fs-sm);
  color: var(--c-text-3);
}
.pending-dot {
  width: 7px;
  height: 7px;
  background: var(--c-accent);
  border-radius: 50%;
  animation: pulse 1.2s ease-in-out infinite;
}
@keyframes pulse {
  0%,
  100% {
    opacity: 0.35;
    transform: scale(0.85);
  }
  50% {
    opacity: 1;
    transform: scale(1);
  }
}

.action {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
  padding: var(--sp-1) var(--sp-2);
  font-size: var(--fs-xs);
  color: var(--c-text-3);
  border-radius: var(--r-sm);
  transition: color var(--dur-fast) var(--ease), background var(--dur-fast) var(--ease);
}
.action:hover {
  color: var(--c-text);
  background: var(--c-surface-2);
}
/* 播放中：用强调色实心，一眼看出"在播的是这条" */
.action.is-active {
  color: var(--c-white);
  background: var(--c-accent);
}

.speak-error {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
  margin-left: var(--sp-2);
  font-size: var(--fs-xs);
  color: var(--c-danger-text);
}

@media (max-width: 480px) {
  .msg {
    gap: var(--sp-2);
  }
  .avatar {
    width: 26px;
    height: 26px;
  }
  .msg.is-user .body {
    max-width: 88%;
  }
}
</style>
