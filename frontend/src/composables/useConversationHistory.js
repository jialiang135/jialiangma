/**
 * 历史对话
 * ========
 *
 * 侧边栏列表 + 载入某条历史 + 删除。从 ChatView 里抽出来。
 *
 * 列表会在每轮问答落库后自动刷新（监听 `session.turnSeq`），
 * 不需要视图层再操心"发送完记得刷侧栏"。
 *
 * 改造前这些逻辑和消息状态、TTS 状态、输入框状态全挤在一个 578 行的视图里，
 * 而"载入历史"这件事本质上和渲染对话是两回事。
 */
import { computed, ref, watch } from 'vue'

import { deleteConversation, getConversation, getConversations } from '../api/chat.js'

/**
 * @param {object} session useChatStream() 的返回值 —— 载入历史需要它来替换消息列表，
 *   同时靠它的 `turnSeq` 感知"又落库了一轮"
 */
export function useConversationHistory(session) {
  const list = ref([])
  const loading = ref(false)
  const error = ref('')
  /** 当前高亮的历史记录 group_id */
  const activeId = ref(null)

  /** @param {{silent?: boolean}} [opts] silent = 后台刷新，不翻 loading（不给渲染好的列表加闪烁） */
  async function load({ silent = false } = {}) {
    if (!silent) loading.value = true
    error.value = ''
    try {
      const res = await getConversations(30)
      list.value = res?.conversations || []
    } catch (e) {
      error.value = e?.message || '加载历史失败'
    } finally {
      if (!silent) loading.value = false
    }
  }

  // 每完成一轮问答，服务端就多一条记录 —— 侧栏必须自己跟上。
  // 原先只在挂载时拉一次，于是"问了两轮，侧栏仍显示『还没有对话』"（实测 bug）。
  watch(
    () => session.turnSeq.value,
    (n) => {
      if (n) load({ silent: true })
    },
  )

  /**
   * 载入某条历史对话到消息区。
   *
   * `__single_` 前缀是历史遗留的"单轮对话"伪 ID，不支持续接 ——
   * 传下去会让后端找不到会话，所以这里统一置为 null（开启新对话）。
   */
  async function open(item) {
    const gid = item?.group_id
    if (!gid) return false

    try {
      const res = await getConversation(gid)
      const ctx = res?.messages || []
      if (!ctx.length) return false

      const continuable = !gid.startsWith('__single_')
      session.loadHistoryMessages(ctx, continuable ? gid : null)
      activeId.value = gid
      return true
    } catch (e) {
      error.value = e?.message || '载入对话失败'
      return false
    }
  }

  /** 删除一条对话；若删的正是当前打开的那条，顺带清空消息区 */
  async function remove(item) {
    const gid = item?.group_id
    if (!gid) return false
    try {
      await deleteConversation(gid)
      if (activeId.value === gid) {
        session.clear()
        activeId.value = null
      }
      await load()
      return true
    } catch (e) {
      error.value = e?.message || '删除对话失败'
      return false
    }
  }

  const isEmpty = computed(() => !loading.value && list.value.length === 0)

  return { list, loading, error, activeId, load, open, remove, isEmpty }
}
