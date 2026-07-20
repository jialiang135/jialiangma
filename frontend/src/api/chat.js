import { get } from './index.js'

/**
 * SSE 流式对话 — 使用 fetch + ReadableStream（支持 POST）
 * @param {string} message 用户消息
 * @param {object} callbacks { onReasoning, onAnswer, onDone, onError }
 * @param {string|null} conversationId 续接已有对话时传入，新对话留空
 * @returns {AbortController} 用于停止生成
 */
export function streamChat(message, callbacks = {}, conversationId = null) {
  const token = localStorage.getItem('token')
  const controller = new AbortController()

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

        for (const line of lines) {
          if (!line.startsWith('data: ')) continue
          try {
            const event = JSON.parse(line.slice(6))
            const { type, content } = event

            if (type === 'reasoning') callbacks.onReasoning?.(content)
            else if (type === 'answer') callbacks.onAnswer?.(content)
            else if (type === 'done') {
              // 解析 done 事件中的 conversation_id（可能是 JSON 或纯字符串）
              let cid = null
              try {
                const doneData = typeof content === 'string' ? JSON.parse(content) : content
                cid = doneData.conversation_id || null
              } catch {
                // 旧版兼容：done 内容是纯字符串
              }
              callbacks.onDone?.(cid)
            }
            else if (type === 'error') callbacks.onError?.(content)
          } catch {
            // skip unparseable lines
          }
        }
      }
    })
    .catch((err) => {
      if (err.name !== 'AbortError') {
        callbacks.onError?.(err.message)
      }
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
