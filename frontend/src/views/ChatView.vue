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
          <div class="empty-icon">🤖</div>
          <h3>个人数字分身 · AI 面试助手</h3>
          <p>基于私有知识库的智能问答系统，输入你的问题开始对话</p>
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
            <div class="msg-text">{{ msg.content }}</div>
            <div v-if="msg.role === 'assistant' && msg.reasoning" class="msg-reasoning">
              <details>
                <summary>🧠 查看完整推理过程（{{ parseSteps(msg.reasoning).length }} 步）</summary>
                <div class="reasoning-timeline inline-timeline">
                  <div v-for="(step, si) in parseSteps(msg.reasoning)" :key="si" class="timeline-step">
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
            <div class="msg-text">{{ streamingText }}<span class="cursor">▌</span></div>
          </div>
        </div>
        <div ref="msgEnd"></div>
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
        <button class="btn btn-sm" @click="clearChat">清空</button>
        <div class="export-dropdown" ref="exportMenuRef" style="position:relative;display:inline-block;">
          <button class="btn btn-sm" @click.stop="showExportMenu = !showExportMenu">📥 导出</button>
          <div v-if="showExportMenu"
               style="position:absolute;bottom:100%;right:0;margin-bottom:4px;background:#fff;border:1px solid #d9d9d9;border-radius:8px;box-shadow:0 4px 16px rgba(0,0,0,0.12);z-index:1000;min-width:150px;overflow:hidden;">
            <button
              style="display:flex;align-items:center;gap:6px;width:100%;padding:10px 16px;border:none;background:none;cursor:pointer;font-size:13px;color:#333;transition:background 0.15s;"
              @click="exportMarkdown"
              @mouseenter="$event.target.style.background='#f5f5f5'"
              @mouseleave="$event.target.style.background='none'"
            >📝 导出 Markdown</button>
            <button
              style="display:flex;align-items:center;gap:6px;width:100%;padding:10px 16px;border:none;background:none;cursor:pointer;font-size:13px;color:#333;transition:background 0.15s;border-top:1px solid #f0f0f0;"
              @click="exportJSON"
              @mouseenter="$event.target.style.background='#f5f5f5'"
              @mouseleave="$event.target.style.background='none'"
            >📦 导出 JSON</button>
          </div>
        </div>
        <button v-if="reasoningSteps.length" class="btn btn-sm reasoning-toggle-btn"
                @click="showReasoning = !showReasoning">
          🧠 {{ showReasoning ? '隐藏推理' : '推理' }}{{ streaming ? ` (${reasoningSteps.length})` : '' }}
        </button>
      </div>

      <!-- Token 统计面板 -->
      <TokenStats v-if="auth.isLoggedIn" />
    </div>

    <!-- 右侧：实时推理时间线（对话进行中显示） -->
    <div v-if="reasoningSteps.length && showReasoning" class="chat-reasoning-panel">
      <div class="panel-header">
        <h4>🧠 推理过程</h4>
        <span class="step-count-badge">{{ reasoningSteps.length }}</span>
      </div>
      <div class="reasoning-timeline">
        <div v-for="(step, i) in reasoningSteps" :key="i" :class="['timeline-step', { latest: i === reasoningSteps.length - 1 && streaming }]">
          <span class="step-icon">{{ step.icon }}</span>
          <span class="step-text">{{ step.text }}</span>
        </div>
      </div>
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
import { ref, computed, nextTick, onMounted, onUnmounted } from 'vue'
import { streamChat, getConversations, getConversation, deleteConversation } from '../api/chat.js'
import { exportChatMarkdown, exportChatJson } from '../api/export.js'
import { useAuthStore } from '../stores/auth.js'
import TokenStats from '../components/TokenStats.vue'

const auth = useAuthStore()
const messages = ref([])
const input = ref('')
const streaming = ref(false)
const streamingText = ref('')
const reasoningLog = ref('')
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

// 推理面板
const showReasoning = ref(false)

// 导出下拉菜单
const showExportMenu = ref(false)
const exportMenuRef = ref(null)

// 删除确认弹窗
const showDeleteConfirm = ref(false)
const pendingDelete = ref(null)  // 待删的 conversation 对象

function onClickAway(e) {
  if (exportMenuRef.value && !exportMenuRef.value.contains(e.target)) {
    showExportMenu.value = false
  }
}

function exportMarkdown() {
  showExportMenu.value = false
  exportChatMarkdown('all', 50).catch(err => {
    console.error('导出 Markdown 失败:', err)
    alert('导出失败: ' + err.message)
  })
}

function exportJSON() {
  showExportMenu.value = false
  exportChatJson(50).catch(err => {
    console.error('导出 JSON 失败:', err)
    alert('导出失败: ' + err.message)
  })
}

// —— 推理步骤解析 ——
// 后端已经给每行带了 emoji 前缀，直接提取；不匹配的自动分配
const EMOJI_RE = /^([\u{1F300}-\u{1FAFF}\u{2700}-\u{27BF}\u{2600}-\u{26FF}\u{1F000}-\u{1F02F}\u{1F0A0}-\u{1F0FF}\u{1F100}-\u{1F64F}\u{1F680}-\u{1F6FF}\u{1F900}-\u{1F9FF}\u{200D}\u{FE0F}\u{20E3}\u{2000}-\u{206F}🛠️➕➖➡️〰️*️⃣#️⃣0️⃣1️⃣2️⃣3️⃣4️⃣5️⃣6️⃣7️⃣8️⃣9️⃣\u{1F7E0}-\u{1F7FF}]|⚠️|✅|❌|📚|📋|📝|📊|📭|💾|💬|🔍|🔀|🔧|🤔|🔄|✨|🎯|📌|🧩|📁|🏷️)/u

function parseSteps(raw) {
  if (!raw) return []
  return raw.split('\n').filter(Boolean).map(line => {
    const t = line.trim()
    // 如果文本以 emoji 开头，取它做图标
    const m = t.match(EMOJI_RE)
    if (m) {
      return { icon: m[0], text: t.slice(m[0].length).trim() }
    }
    // 没有 emoji 前缀的，按关键词自动分配
    let icon = '•'
    if (t.includes('工具') || t.includes('搜索'))        icon = '🔍'
    else if (t.includes('检索') || t.includes('知识库') || t.includes('匹配')) icon = '📚'
    else if (t.includes('拆解') || t.includes('分析') || t.includes('问题'))   icon = '🤔'
    else if (t.includes('分支') || t.includes('判断') || t.includes('路由'))   icon = '🔀'
    else if (t.includes('规划') || t.includes('方案'))                          icon = '📝'
    else if (t.includes('合规') || t.includes('校验') || t.includes('闭环'))    icon = '✅'
    else if (t.includes('生成') || t.includes('回答'))                          icon = '➡️'
    else if (t.includes('返回') || t.includes('结果'))                          icon = '📋'
    else if (t.includes('评测') || t.includes('报告'))                          icon = '📊'
    else if (t.includes('保存'))                                                icon = '💾'
    else if (t.includes('错误') || t.includes('失败') || t.includes('异常'))    icon = '⚠️'
    return { icon, text: t }
  })
}

const reasoningSteps = computed(() => parseSteps(reasoningLog.value))

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
  reasoningLog.value = ''
  showReasoning.value = true   // 发消息时自动打开推理面板
  let answerBuffer = ''

  abortController = streamChat(text, {
    onReasoning(r) {
      reasoningLog.value += (reasoningLog.value ? '\n' : '') + r
    },
    onAnswer(a) {
      answerBuffer += a
      streamingText.value = answerBuffer
    },
    onDone(returnedCid) {
      if (answerBuffer) {
        messages.value.push({ role: 'assistant', content: answerBuffer, reasoning: reasoningLog.value })
      }
      // 新对话：后端返回了 conversation_id，记录下来以便续接
      if (returnedCid && !currentConversationId.value) {
        currentConversationId.value = returnedCid
      }
      streaming.value = false
      streamingText.value = ''
      abortController = null
      scrollBottom()
      loadHistory()
    },
    onError(err) {
      messages.value.push({ role: 'assistant', content: `❌ ${err}` })
      streaming.value = false
      streamingText.value = ''
      abortController = null
    },
  }, currentConversationId.value)

  scrollBottom()
}

function stopStream() {
  if (abortController) {
    abortController.abort()
    if (streamingText.value) {
      messages.value.push({ role: 'assistant', content: streamingText.value, reasoning: reasoningLog.value })
    }
    streaming.value = false
    streamingText.value = ''
    abortController = null
    loadHistory()
  }
}

function clearChat() {
  messages.value = []
  streamingText.value = ''
  reasoningLog.value = ''
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
  // c 是 ConversationSummary: { group_id, first_log_id, turn_count, last_at, first_question }
  const gid = c.group_id
  if (!gid) return

  // 加载完整对话上下文
  try {
    const res = await getConversation(gid)
    const ctx = res.messages || []
    if (ctx.length > 0) {
      clearChat()
      for (const m of ctx) {
        messages.value.push({
          role: m.role,
          content: m.content,
          reasoning: m.role === 'assistant' ? m.reasoning : undefined,
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

onMounted(() => {
  if (auth.isLoggedIn) loadHistory()
  document.addEventListener('click', onClickAway)
})

onUnmounted(() => {
  if (abortController) abortController.abort()
  document.removeEventListener('click', onClickAway)
})
</script>
