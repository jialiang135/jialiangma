<template>
  <div class="chat-view">
    <!-- 左侧：历史对话 -->
    <div class="chat-sidebar" :class="{ collapsed: !showHistory }">
      <div v-if="showHistory" class="sidebar-header">
        <span>💬 历史对话</span>
        <button class="btn btn-sm sidebar-toggle" @click="showHistory = false" title="收起侧边栏">✕</button>
      </div>
      <div v-if="showHistory" class="sidebar-body">
        <button class="btn btn-sm sidebar-refresh" @click="loadHistory" :disabled="loadingHistory">
          🔄 刷新
        </button>
        <div v-if="historyList.length === 0 && !loadingHistory" class="empty-state">
          暂无历史对话
        </div>
        <div v-for="c in historyList" :key="c.group_id"
             :class="['history-item', { active: activeHistoryId === c.group_id }]"
             @click="loadConversation(c)">
          <div class="history-q">{{ (c.first_question || '新对话').slice(0, 40) }}{{ (c.first_question || '').length > 40 ? '...' : '' }}</div>
          <div class="history-meta">
            {{ c.turn_count }} 轮 · {{ formatTime(c.last_at) }}
            <button class="btn-delete-conv" @click.stop="delConversation(c)" title="删除此对话">×</button>
          </div>
        </div>
      </div>
    </div>

    <button v-if="!showHistory" class="sidebar-expand-btn" @click="showHistory = true" title="展开历史对话">
      💬
    </button>

    <!-- 中间：对话主体 -->
    <div class="chat-main">
      <div class="chat-messages" ref="msgContainer">
        <div v-if="messages.length === 0 && !currentConversationId" class="empty-chat">
          <div class="empty-badge">🤖</div>
          <h3>个人数字分身 · AI 面试助手</h3>
          <p>基于私有知识库的智能问答系统。直接提问，或从下面挑一个开始。</p>
          <!-- 快捷问题做成卡片放进空状态：原先它们在框外，与「开始对话」是脱开的 -->
          <div class="quick-start">
            <button v-for="q in quickQuestions" :key="q.label"
                    class="quick-card"
                    :disabled="streaming"
                    @click="askQuick(q.text)">
              <span class="quick-card-icon">{{ q.icon }}</span>
              <span class="quick-card-text">
                <span class="quick-card-label">{{ q.label }}</span>
                <span class="quick-card-hint">{{ q.hint }}</span>
              </span>
            </button>
          </div>
        </div>

        <!-- 续接对话提示条 -->
        <div v-if="currentConversationId" class="conversation-bar">
          <span>📌 继续对话</span>
          <span class="conversation-id">#{{ currentConversationId.slice(0, 8) }}...</span>
          <button class="btn btn-sm" @click="clearChat">＋ 新对话</button>
        </div>
        <div v-for="(msg, i) in messages" :key="i" :class="['msg', msg.role]">
          <div class="msg-avatar">{{ msg.role === 'user' ? '👤' : '🤖' }}</div>
          <div class="msg-content">
            <div class="msg-text" v-html="renderMarkdown(msg.content)"></div>
            <div v-if="msg.role === 'assistant' && (msg.steps?.length || msg.thinking)"
                 class="msg-reasoning">
              <!-- 最新一条默认展开：流式期间用户正看着实时思考，
                   答案完成后面板若自动收起，等于把他在看的内容突然藏掉。
                   历史消息仍默认收起，避免整页被推理内容撑长。 -->
              <details :open="i === messages.length - 1">
                <summary>🧠 查看完整推理过程（{{ msg.steps?.length || 0 }} 步）</summary>
                <div v-if="msg.thinking" class="thinking-stream">
                  <div class="thinking-label">💭 模型思考</div>
                  <div class="thinking-text">{{ msg.thinking }}</div>
                </div>
                <div class="reasoning-timeline inline-timeline">
                  <div v-for="(step, si) in (msg.steps || [])" :key="si" class="timeline-step">
                    <span class="step-icon">{{ step.icon }}</span>
                    <span class="step-text">{{ step.text }}</span>
                  </div>
                </div>
              </details>
            </div>
          </div>
        </div>
        <div v-if="streaming" class="msg assistant streaming">
          <div class="msg-avatar">🤖</div>
          <div class="msg-content">
            <div v-if="liveSteps.length || thinkingText" class="msg-reasoning streaming-reasoning">
              <details open>
                <summary>
                  🧠 {{ thinkingText ? '思考中' : '推理中' }}…（{{ liveSteps.length }} 步{{ thinkingText ? ` · 思考 ${thinkingText.length} 字` : '' }}）
                </summary>
                <!-- 模型真实思考流：推理模型的"内心独白"，与下面的流程步骤是两类信息 -->
                <div v-if="thinkingText" class="thinking-stream">
                  <div class="thinking-label">💭 模型思考</div>
                  <div class="thinking-text">{{ thinkingText }}</div>
                </div>
                <div class="reasoning-timeline inline-timeline">
                  <div v-for="(step, si) in liveSteps" :key="si"
                       :class="['timeline-step', { latest: si === liveSteps.length - 1 }]">
                    <span class="step-icon">{{ step.icon }}</span>
                    <span class="step-text">{{ step.text }}</span>
                  </div>
                </div>
              </details>
            </div>
            <div class="msg-text" v-html="renderMarkdown(streamingText)"></div>
          </div>
        </div>
        <div ref="msgEnd"></div>
      </div>

      <!-- 快捷问题（紧凑条）：空状态里已有卡片，这里只在已有对话时出现，
           作为「换个问题试试」的入口 -->
      <div v-if="messages.length > 0" class="quick-questions compact">
        <button v-for="q in quickQuestions" :key="q.label"
                class="quick-q-btn"
                :disabled="streaming"
                @click="askQuick(q.text)">
          {{ q.icon }} {{ q.label }}
        </button>
      </div>

      <div class="chat-input-area">
        <textarea
          v-model="input"
          class="chat-input"
          placeholder="输入你的问题后按回车发送..."
          rows="1"
          :disabled="streaming"
          @keydown.enter.exact.prevent="send"
          @input="autoResize"
        ></textarea>
        <button class="btn btn-primary" :disabled="streaming || !input.trim()" @click="send">发送</button>
        <button v-if="streaming" class="btn btn-stop" @click="stopStream">停止</button>
      </div>

      <!-- Token 统计面板 -->
      <TokenStats v-if="auth.isLoggedIn" />
    </div>


    <!-- 删除确认弹窗 -->
    <Teleport to="body">
      <div v-if="showDeleteConfirm" class="modal-overlay" @click.self="cancelDelete">
        <div class="modal-dialog modal-sm">
          <div class="modal-icon">⚠️</div>
          <h3 class="modal-title">确认删除对话</h3>
          <p class="modal-desc">
            “<strong>{{ (pendingDelete?.first_question || '新对话').slice(0, 50) }}{{ (pendingDelete?.first_question || '').length > 50 ? '...' : '' }}</strong>”
          </p>
          <p class="modal-detail">
            共 {{ pendingDelete?.turn_count || 0 }} 轮对话将被永久删除，不可恢复。
          </p>
          <div class="modal-actions">
            <button class="btn" @click="cancelDelete">取消</button>
            <button class="btn btn-danger" @click="confirmDelete">确认删除</button>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<script setup>
import { ref, nextTick, onMounted, onUnmounted } from 'vue'
import { streamChat, getConversations, getConversation, deleteConversation } from '../api/chat.js'
import { useAuthStore } from '../stores/auth.js'
// 推理数据的解析放共享模块：管理页也要用同一套，避免两处漂移
import { parseStepLine, parseStoredReasoning } from '../utils/reasoning.js'
import TokenStats from '../components/TokenStats.vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'

// 配置 marked。注意：marked v5 起移除了内置的 sanitize 选项，
// 它现在**不做任何净化**，输出必须自己处理后再交给 v-html。
marked.use({
  breaks: true,      // 单个换行也转 <br>
  gfm: true,         // GitHub Flavored Markdown（表格、任务列表、删除线等）
})

// 白名单式净化。
// 渲染的内容来自两个不可信来源：LLM 输出，以及被检索到的**用户上传文档**——
// 传一个含 `<img src=x onerror=...>` 的 md，不净化就会在对话区执行脚本。
// 采用白名单而非黑名单：不在列表里的标签/属性一律丢弃。
const PURIFY_CONFIG = {
  ALLOWED_TAGS: [
    'p', 'br', 'hr', 'strong', 'em', 'del', 'code', 'pre', 'blockquote',
    'ul', 'ol', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'table', 'thead', 'tbody', 'tr', 'th', 'td', 'a', 'span',
  ],
  ALLOWED_ATTR: ['href', 'title', 'class'],
  // 只允许安全协议，挡掉 javascript: / data: 这类伪协议
  ALLOWED_URI_REGEXP: /^(?:https?|mailto):/i,
}

const auth = useAuthStore()
const messages = ref([])
const input = ref('')
const streaming = ref(false)
const streamingText = ref('')
// 推理步骤：直接累积"已解析好的对象"，而不是每次渲染都重新解析整段文本
// （原实现每帧调用 3 次 parseSteps，逐 token 更新时是 O(n²) 的重复解析）
const liveSteps = ref([])
// 模型真实思考流（推理模型）。与 liveSteps 是两类信息：这是模型的内心独白
const thinkingText = ref('')
const msgContainer = ref(null)
const msgEnd = ref(null)
let abortController = null

// 历史对话
const showHistory = ref(true)
const historyList = ref([])
const loadingHistory = ref(false)

// 当前对话 ID（续接已有对话时设置，新对话为 null，由后端返回）
const currentConversationId = ref(null)
const activeHistoryId = ref(null)  // 高亮当前活跃的历史记录

// 删除确认弹窗
const showDeleteConfirm = ref(false)
const pendingDelete = ref(null)  // 待删的 conversation 对象

// —— 快捷问题 ——
const quickQuestions = [
  { icon: '👋', label: '介绍一下你自己', text: '介绍一下你自己', hint: '个人信息与背景' },
  { icon: '💻', label: '你熟悉哪些技术栈', text: '你熟悉哪些技术栈？', hint: '技能清单' },
  { icon: '🚀', label: '你做过哪些项目', text: '你做过哪些项目？', hint: '项目经验' },
]

function askQuick(text) {
  if (streaming.value) return
  input.value = text
  send()
}

function renderMarkdown(text) {
  if (!text) return ''
  // GFM 表格要求表头行前有空行，LLM 输出经常缺少，自动补齐
  const fixed = text.replace(/([^\n])\n(\|[^\n]+\|\s*\n\|[-| :]+\|)/g, '$1\n\n$2')
  // 净化后再交 v-html（marked 自身不再做净化）
  return DOMPurify.sanitize(marked(fixed), PURIFY_CONFIG)
}



function autoResize(e) {
  e.target.style.height = 'auto'
  e.target.style.height = Math.min(e.target.scrollHeight, 120) + 'px'
}

async function send() {
  const text = input.value.trim()
  if (!text || streaming.value) return
  input.value = ''

  messages.value.push({ role: 'user', content: text })
  streaming.value = true
  streamingText.value = ''
  liveSteps.value = []
  thinkingText.value = ''
  let answerBuffer = ''
  let errored = false

  /**
   * 结束本轮对话（幂等）。
   *
   * onDone 与 onClose 只会有一个触发（见 api/chat.js 的 settle），
   * 但两条路径都要走这里，否则"服务端没发 done 就断开"时
   * streaming 会永久为 true，输入框永久禁用。
   */
  function finalize(answer, cid) {
    if (!streaming.value) return
    const finalText = (answer || answerBuffer || '').trim()
    if (finalText) {
      messages.value.push({
        role: 'assistant',
        content: finalText,
        // 模板读的是已解析好的 steps / thinking（与 loadConversation 保持一致），
        // 直接引用副本，避免清空流式状态后把面板一起清掉
        steps: liveSteps.value.slice(),
        thinking: thinkingText.value,
      })
    } else if (!errored) {
      // 一个字都没生成：明确告诉用户，而不是留下空白
      messages.value.push({ role: 'assistant', content: '（本轮没有生成内容，请重试或换个问法）' })
    }
    if (cid && !currentConversationId.value) {
      currentConversationId.value = cid
    }
    streaming.value = false
    streamingText.value = ''
    liveSteps.value = []
    thinkingText.value = ''
    abortController = null
    scrollBottom()
    loadHistory()
    // 通知 Token 统计刷新：服务端在 done 之前已把用量落库，这里正好拿到新数字
    window.dispatchEvent(new CustomEvent('token-usage-updated'))
  }

  abortController = streamChat(text, {
    // 整行"步骤"（工具调用/检索结果）。后端可能附 icon，用于行首没有 emoji 的情况
    onReasoning(line, icon) {
      const step = parseStepLine(line)
      if (!step) return
      if (icon && step.icon === '•') step.icon = icon
      liveSteps.value.push(step)
    },
    // 模型真实思考的增量（推理模型）
    onReasoningDelta(delta) {
      thinkingText.value += delta
    },
    onAnswer(a) {
      answerBuffer += a
      streamingText.value = answerBuffer
    },
    // done 带权威全文：用它替换流式期间累积的文本，
    // 保证"界面显示的"与"落库的"完全一致（ReAct 中间轮次的过渡语不会混进答案）
    onDone(cid, answer) {
      finalize(answer, cid)
    },
    // 流结束但没有 done（网络中断/服务端异常/超时）—— 必须收尾，否则 UI 卡死
    onClose(reason) {
      if (reason !== 'aborted') {
        console.warn('[chat] 流未正常结束:', reason)
      }
      finalize('', null)
    },
    onError(err) {
      errored = true
      messages.value.push({ role: 'assistant', content: `❌ ${err}` })
    },
  }, currentConversationId.value)

  scrollBottom()
}

function stopStream() {
  if (!abortController) return
  // 后端在取消路径上会同步保存已生成的内容，这里只负责收尾前端状态
  abortController.abort()
  abortController = null
  streaming.value = false
  const partial = streamingText.value
  if (partial) {
    messages.value.push({ role: 'assistant', content: partial })
  }
  streamingText.value = ''
  liveSteps.value = []
  thinkingText.value = ''
  loadHistory()
}

function clearChat() {
  messages.value = []
  streamingText.value = ''
  liveSteps.value = []
  thinkingText.value = ''
  currentConversationId.value = null
  activeHistoryId.value = null
}

function delConversation(c) {
  pendingDelete.value = c
  showDeleteConfirm.value = true
}

async function confirmDelete() {
  const c = pendingDelete.value
  if (!c) return
  try {
    await deleteConversation(c.group_id)
    if (activeHistoryId.value === c.group_id) {
      clearChat()
    }
    await loadHistory()
  } catch (e) {
    console.error('删除对话失败:', e)
  } finally {
    showDeleteConfirm.value = false
    pendingDelete.value = null
  }
}

function cancelDelete() {
  showDeleteConfirm.value = false
  pendingDelete.value = null
}

function scrollBottom() {
  nextTick(() => {
    msgEnd.value?.scrollIntoView({ behavior: 'smooth' })
  })
}

async function loadHistory() {
  if (!auth.isLoggedIn) return
  loadingHistory.value = true
  try {
    const res = await getConversations(30)
    historyList.value = res.conversations || []
  } catch {
    // ignore
  } finally {
    loadingHistory.value = false
  }
}

async function loadConversation(c) {
  const gid = c.group_id
  if (!gid) return

  // 手机端选择对话后自动关闭侧边栏
  if (isMobile.value) showHistory.value = false

  try {
    const res = await getConversation(gid)
    const ctx = res.messages || []
    if (ctx.length > 0) {
      clearChat()
      for (const m of ctx) {
        // reasoning 字段落库有两种格式（新的 JSON 含 steps/thinking、旧的纯文本行），
        // 这里统一解析好再交给模板，避免模板里反复解析
        const parsed = m.role === 'assistant'
          ? parseStoredReasoning(m.reasoning)
          : { steps: [], thinking: '' }
        messages.value.push({
          role: m.role,
          content: m.content,
          steps: parsed.steps,
          thinking: parsed.thinking,
        })
      }
      // 仅真实 UUID 才支持续接，legacy '__single_' 前缀的不支持
      currentConversationId.value = gid.startsWith('__single_') ? null : gid
      activeHistoryId.value = gid
      scrollBottom()
    }
  } catch {
    // 接口不可用，忽略
  }
}

function formatTime(d) {
  if (!d) return ''
  return String(d).slice(5, 19)
}

// 移动端检测
const isMobile = ref(window.innerWidth <= 768)

function onResize() {
  isMobile.value = window.innerWidth <= 768
  if (isMobile.value) showHistory.value = false
}

onMounted(() => {
  if (isMobile.value) showHistory.value = false
  window.addEventListener('resize', onResize)
  if (auth.isLoggedIn) loadHistory()
})

onUnmounted(() => {
  if (abortController) abortController.abort()
})
</script>
