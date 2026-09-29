/**
 * 轻提示（Toast）
 * ===============
 *
 * 改造前"操作完给个反馈"这件事在知识库页和评测页各写了一遍 `showMsg`，
 * 两份代码**逐字节相同**（`statusMsg` + `statusType` + `setTimeout 5000`），
 * 连模板里的 `:class="['status-msg', statusType]"` 都一模一样；
 * 管理页又是另一套 `userMsg`/`userMsgType`。
 *
 * 这里用一个模块级单例：任何组件 import 后直接调 `toast.success('...')`，
 * 由 `<UiToastHost />` 在 App 根部统一渲染。
 *
 * 为什么不用 Pinia：toast 是纯 UI 瞬时状态，不参与业务数据流，
 * 没必要建 store。模块级 ref 在 Vue 里是合法的跨组件共享方式。
 */
import { ref } from 'vue'

const items = ref([])
let seq = 0

/** 同一条消息连续弹多次时，只保留最新那条，避免刷屏 */
function push(message, tone = 'info', duration = 4000) {
  const text = String(message ?? '').trim()
  if (!text) return

  const existing = items.value.find((t) => t.message === text && t.tone === tone)
  if (existing) {
    existing.createdAt = Date.now()
    // 重置计时：重新推入等于"这条又发生了一次"
    clearTimeout(existing.timer)
    existing.timer = setTimeout(() => dismiss(existing.id), duration)
    return existing.id
  }

  const id = ++seq
  const timer = setTimeout(() => dismiss(id), duration)
  items.value.push({ id, message: text, tone, timer, createdAt: Date.now() })
  return id
}

function dismiss(id) {
  const idx = items.value.findIndex((t) => t.id === id)
  if (idx === -1) return
  clearTimeout(items.value[idx].timer)
  items.value.splice(idx, 1)
}

export function useToast() {
  return {
    items,
    dismiss,
    success: (m, d) => push(m, 'success', d),
    error: (m, d) => push(m, 'danger', d),
    warning: (m, d) => push(m, 'warning', d),
    info: (m, d) => push(m, 'info', d),
  }
}
