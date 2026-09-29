<script setup>
/**
 * 对话页
 * ======
 *
 * 改造前这个文件 578 行，`<script setup>` 里塞着对话流、SSE 回调、TTS 接线、
 * 历史加载、markdown 配置、移动端检测……几乎所有东西。模板里一个 100 多行的
 * v-for 块里套了三层条件。
 *
 * 现在这里只剩**编排**：把组合式函数和子组件接起来。
 * 具体逻辑都在各自该在的地方：
 *   useChatStream           发消息 / 消费 SSE / 收尾
 *   useConversationHistory  历史列表 / 载入 / 删除
 *   useSpeech               朗读
 *   utils/markdown          渲染
 *   components/chat/*       消息、证据轨、推理面板、输入区、侧栏
 */
import { computed, nextTick, onMounted, ref, watch } from 'vue'

import Composer from '../components/chat/Composer.vue'
import ConversationSidebar from '../components/chat/ConversationSidebar.vue'
import MessageItem from '../components/chat/MessageItem.vue'
import TokenStats from '../components/TokenStats.vue'
import UiButton from '../components/ui/UiButton.vue'
import UiEmpty from '../components/ui/UiEmpty.vue'
import UiIcon from '../components/ui/UiIcon.vue'
import UiModal from '../components/ui/UiModal.vue'
import { useChatStream } from '../composables/useChatStream.js'
import { useConversationHistory } from '../composables/useConversationHistory.js'
import { useSpeech } from '../composables/useSpeech.js'
import { useToast } from '../composables/useToast.js'
import { useAuthStore } from '../stores/auth.js'

const auth = useAuthStore()
const toast = useToast()

const session = useChatStream()
// 解构出各个 ref：这样模板里直接写 `messages` 就行，不必层层 `.value`
const { messages, streaming, draftAnswer, liveSteps, thinkingText, evidence, stop: stopStream } =
  session

const conversations = useConversationHistory(session)
const {
  list: historyList,
  loading: historyLoading,
  activeId: activeConversationId,
  load: loadHistory,
  open: openConversation,
  remove: removeConversation,
} = conversations

const speech = useSpeech()
const { speakingId, error: speakError, toggle: toggleSpeech, stop: stopSpeech } = speech

const input = ref('')
const scroller = ref(null)
const pendingDelete = ref(null)
const showSidebar = ref(false)

const quickQuestions = [
  { icon: 'user', label: '介绍一下你自己', text: '介绍一下你自己', hint: '个人信息与背景' },
  { icon: 'sliders', label: '你熟悉哪些技术栈', text: '你熟悉哪些技术栈？', hint: '技能清单' },
  { icon: 'layers', label: '你做过哪些项目', text: '你做过哪些项目？', hint: '项目经验' },
]

/**
 * 流式生成中的临时消息。
 *
 * 做成一个"和真实消息同构"的对象交给 MessageItem 渲染，而不是在模板里
 * 再写一份 —— 否则流式态和完成态会各写一套样式，过一阵子必然长得不一样。
 * （改造前推理面板的"双写"就是这么来的。）
 */
const draftMessage = computed(() => ({
  id: '__draft__',
  role: 'assistant',
  content: draftAnswer.value,
  steps: liveSteps.value,
  thinking: thinkingText.value,
  evidence: evidence.value,
}))

const showEmptyState = computed(() => !messages.value.length && !streaming.value)

/* ---------- 滚动 ---------- */

// 用户往上翻历史时不要把他拽回底部 —— 只在"贴着底"时才自动跟随
let stick = true

function onScroll() {
  const el = scroller.value
  if (!el) return
  stick = el.scrollHeight - el.scrollTop - el.clientHeight < 80
}

async function scrollToBottom(smooth = true) {
  await nextTick()
  scroller.value?.scrollTo({
    top: scroller.value.scrollHeight,
    behavior: smooth ? 'smooth' : 'auto',
  })
}

watch([() => messages.value.length, draftAnswer], () => {
  if (stick) scrollToBottom(false)
})

/* ---------- 操作 ---------- */

function send(text) {
  const content = String(text ?? '').trim()
  if (!content || streaming.value) return
  // 新提问时停掉正在朗读的上一条：新旧声音叠在一起听不清，也白烧合成配额
  stopSpeech()
  session.send(content)
  stick = true
  scrollToBottom()
}

function pickQuick(q) {
  input.value = ''
  send(q.text)
}

function newChat() {
  session.clear()
  showSidebar.value = false
}

async function onSelectConversation(item) {
  stopSpeech()
  const ok = await openConversation(item)
  if (ok) {
    stick = true
    scrollToBottom(false)
    showSidebar.value = false
  } else if (conversations.error.value) {
    toast.error(conversations.error.value)
  }
}

async function confirmDelete() {
  const item = pendingDelete.value
  pendingDelete.value = null
  if (!item) return

  const ok = await removeConversation(item)
  if (ok) {
    toast.success('已删除对话')
  } else {
    toast.error(conversations.error.value || '删除失败')
  }
}

function onSpeak(msg) {
  // 用消息的稳定 id，不用数组下标 —— 下标在列表变动时会指到别人身上
  toggleSpeech(msg.id, msg.content)
}

onMounted(() => {
  if (auth.isLoggedIn) loadHistory()
})
</script>

<template>
  <div class="chat">
    <ConversationSidebar
      class="chat-sidebar"
      :class="{ 'is-open': showSidebar }"
      :items="historyList"
      :loading="historyLoading"
      :active-id="activeConversationId"
      @select="onSelectConversation"
      @delete="pendingDelete = $event"
      @refresh="loadHistory"
      @new="newChat"
    />

    <div class="chat-main">
      <header class="chat-toolbar">
        <button
          class="toolbar-btn"
          type="button"
          title="历史对话"
          @click="showSidebar = !showSidebar"
        >
          <UiIcon name="menu" :size="16" />
        </button>
        <span class="toolbar-title">
          {{ activeConversationId ? '继续对话' : '新对话' }}
        </span>
        <UiButton v-if="messages.length" size="sm" variant="ghost" @click="newChat">
          <template #icon><UiIcon name="plus" :size="14" /></template>
          新对话
        </UiButton>
      </header>

      <div ref="scroller" class="chat-scroll" @scroll.passive="onScroll">
        <UiEmpty
          v-if="showEmptyState"
          icon="layers"
          title="个人数字分身 · AI 面试助手"
          description="基于私有知识库的智能问答。每个回答都会标注它依据了知识库里的哪些片段 —— 句句有据可查。"
        >
          <button
            v-for="q in quickQuestions"
            :key="q.label"
            class="quick-card"
            type="button"
            @click="pickQuick(q)"
          >
            <UiIcon :name="q.icon" :size="16" class="quick-icon" />
            <span class="quick-text">
              <span class="quick-label">{{ q.label }}</span>
              <span class="quick-hint">{{ q.hint }}</span>
            </span>
          </button>
        </UiEmpty>

        <div v-else class="chat-stream">
          <MessageItem
            v-for="(msg, i) in messages"
            :key="msg.id"
            :message="msg"
            :speaking="speakingId === msg.id"
            :speak-error="speakingId === msg.id ? speakError : ''"
            :expand-reasoning="i === messages.length - 1"
            @speak="onSpeak(msg)"
            @stop-speak="stopSpeech"
          />

          <MessageItem
            v-if="streaming"
            :message="draftMessage"
            draft
            expand-reasoning
          />
        </div>

        <div class="scroll-anchor" />
      </div>

      <div class="chat-foot">
        <Composer
          v-model="input"
          :streaming="streaming"
          :disabled="!auth.isLoggedIn"
          :placeholder="auth.isLoggedIn ? '输入你的问题，回车发送' : '请先登录后再提问'"
          @send="send"
          @stop="stopStream"
        />
        <TokenStats v-if="auth.isLoggedIn" />
      </div>
    </div>

    <UiModal
      :open="!!pendingDelete"
      size="sm"
      title="删除这条对话？"
      :close-on-overlay="false"
      @close="pendingDelete = null"
    >
      <p class="confirm-text">
        「{{ pendingDelete?.first_question || '新对话' }}」共
        {{ pendingDelete?.turn_count || 0 }} 轮对话将被永久删除，不可恢复。
      </p>
      <template #footer>
        <UiButton variant="secondary" @click="pendingDelete = null">取消</UiButton>
        <UiButton variant="danger" @click="confirmDelete">确认删除</UiButton>
      </template>
    </UiModal>
  </div>
</template>

<style scoped>
.chat {
  display: flex;
  height: 100%;
  min-height: 0;
}

.chat-main {
  display: flex;
  flex: 1;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
}

/* 工具条：窄屏才需要（汉堡按钮 + 当前状态） */
.chat-toolbar {
  display: none;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  border-bottom: 1px solid var(--c-border);
}
.toolbar-btn {
  display: inline-flex;
  padding: var(--sp-1);
  color: var(--c-text-2);
  border-radius: var(--r-sm);
}
.toolbar-btn:hover {
  background: var(--c-surface-2);
}
.toolbar-title {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.chat-scroll {
  flex: 1;
  padding: var(--sp-6) var(--sp-5) var(--sp-4);
  overflow-y: auto;
}

.chat-stream {
  width: 100%;
  max-width: var(--content-max);
  margin: 0 auto;
}

/* 快捷问题做成卡片放在空状态里，而不是飘在输入框上方 */
.quick-card {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  padding: var(--sp-3) var(--sp-4);
  text-align: left;
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--r-lg);
  box-shadow: var(--sh-1);
  transition: border-color var(--dur-fast) var(--ease),
    box-shadow var(--dur-fast) var(--ease), transform var(--dur-fast) var(--ease);
}
.quick-card:hover {
  border-color: var(--c-accent-border);
  box-shadow: var(--sh-2);
  transform: translateY(-1px);
}
.quick-icon {
  color: var(--c-accent);
}
.quick-text {
  display: flex;
  flex-direction: column;
}
.quick-label {
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--c-text);
}
.quick-hint {
  font-size: var(--fs-xs);
  color: var(--c-text-3);
}

.chat-foot {
  flex-shrink: 0;
  padding: 0 var(--sp-5) var(--sp-3);
}
/* 输入区与消息区宽度对齐，视觉上是一条中轴线 */
.chat-foot :deep(.composer),
.chat-foot :deep(.token-stats) {
  max-width: var(--content-max);
  margin: 0 auto;
}
.chat-foot :deep(.composer) {
  margin-bottom: var(--sp-2);
}

.scroll-anchor {
  height: 1px;
}

.confirm-text {
  font-size: var(--fs-sm);
  line-height: var(--lh-base);
  color: var(--c-text-2);
}

/* ── 窄屏 ── */
@media (max-width: 768px) {
  .chat-toolbar {
    display: flex;
  }
  .chat-scroll {
    padding: var(--sp-4) var(--sp-3) var(--sp-3);
  }
  .chat-foot {
    padding: 0 var(--sp-3) var(--sp-2);
  }
  /* 侧栏改抽屉式：默认移出屏幕，靠工具条的汉堡按钮拉出 */
  .chat-sidebar {
    position: fixed;
    top: var(--header-h);
    bottom: 0;
    left: 0;
    z-index: var(--z-dropdown);
    display: flex;
    box-shadow: var(--sh-3);
    transform: translateX(-100%);
    transition: transform var(--dur-base) var(--ease);
  }
  .chat-sidebar.is-open {
    transform: translateX(0);
  }
}
</style>
