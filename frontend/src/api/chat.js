import { get } from './index.js'

/**
 * SSE 流式对话 — 使用 fetch + ReadableStream（支持 POST）
 *
 * 回调约定：
 *   onReasoning(line)        整行"步骤"（工具调用/检索结果），可能带 icon
 *   onReasoningDelta(text)   模型真实思考的 token 增量（推理模型）
 *   onAnswer(text)           答案 token 增量
 *   onUsage(usage)           真实 token 用量对象
 *   onDone(cid, answer)      流正常结束；answer 是权威全文，用它替换流式期间显示的文本
 *   onClose(reason)          流结束但**没收到 done**（网络中断/服务端异常/超时）
 *   onError(message)         错误
 *
 * 注意 onClose 的存在意义：若只有 onDone，一旦服务端在 done 之前断开，
 * 前端不会收到任何结束回调，streaming 状态就会永久为 true，
 * 输入框和发送按钮永久禁用，只能刷新页面。
 *
 * @param {string} message 用户消息
 * @param {object} callbacks 见上
 * @param {string|null} conversationId 续接已有对话时传入，新对话留空
 * @returns {AbortController} 用于停止生成
 */
export function streamChat(message, callbacks = {}, conversationId = null) {
  const token = localStorage.getItem('token')
  const controller = new AbortController()

  let settled = false
  const settle = (fn, ...args) => {
    // 保证 onDone / onClose 只触发一次
    if (settled) return
    settled = true
    fn?.(...args)
  }

  const handleLine = (line) => {
    if (!line.startsWith('data: ')) return
    let event
    try {
      event = JSON.parse(line.slice(6))
    } catch {
      return // 跳过无法解析的行
    }
    const { type, content } = event

    if (type === 'reasoning') callbacks.onReasoning?.(content, event.icon)
    else if (type === 'reasoning_delta') callbacks.onReasoningDelta?.(content)
    else if (type === 'answer') callbacks.onAnswer?.(content)
    else if (type === 'usage') {
      try {
        callbacks.onUsage?.(typeof content === 'string' ? JSON.parse(content) : content)
      } catch {
        /* 用量解析失败不影响主流程 */
      }
    } else if (type === 'done') {
      let cid = null
      let answer = ''
      try {
        const doneData = typeof content === 'string' ? JSON.parse(content) : content
        cid = doneData.conversation_id || null
        answer = doneData.answer || ''
      } catch {
        // 旧版兼容：done 内容是纯字符串
      }
      settle(callbacks.onDone, cid, answer)
    } else if (type === 'error') {
      callbacks.onError?.(content)
    }
  }

  fetch('/api/chat/stream', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify({
      message,
      agent_mode: 'chat',
      ...(conversationId ? { conversation_id: conversationId } : {}),
    }),
    signal: controller.signal,
  })
    .then(async (response) => {
      if (!response.ok) {
        const err = await response.text()
        callbacks.onError?.(`HTTP ${response.status}: ${err}`)
        settle(callbacks.onClose, 'http_error')
        return
      }

      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() || ''

        for (const line of lines) handleLine(line)
      }

      // 流结束时 buffer 里可能还留着最后一条没有换行结尾的事件，不能丢
      if (buffer.trim()) handleLine(buffer)

      // 走到这里说明流自然结束了；若从未收到 done，也必须解除前端的等待状态
      settle(callbacks.onClose, 'stream_ended_without_done')
    })
    .catch((err) => {
      if (err.name === 'AbortError') {
        // 用户主动停止：交给调用方处理，不算异常
        settle(callbacks.onClose, 'aborted')
        return
      }
      callbacks.onError?.(err.message)
      settle(callbacks.onClose, 'network_error')
    })

  return controller
}

/**
 * 获取对话摘要列表（侧边栏用，按对话分组）
 */
export function getConversations(limit = 30) {
  return get('/chat/conversations', { limit })
}

/**
 * @deprecated 旧接口，保留兼容
 */
export function getChatHistory(limit = 50, offset = 0) {
  return get('/chat/history', { limit, offset })
}

/**
 * 获取指定 conversation 的完整对话上下文（点击历史后加载全部消息）
 */
export function getConversation(conversationId) {
  return get(`/chat/conversation/${conversationId}`)
}

/**
 * 删除指定对话组（侧边栏删除按钮用）
 */
export async function deleteConversation(groupId) {
  const token = localStorage.getItem('token')
  const resp = await fetch(`/api/chat/conversation/${groupId}`, {
    method: 'DELETE',
    headers: {
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
  })
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
  return resp.json()
}
