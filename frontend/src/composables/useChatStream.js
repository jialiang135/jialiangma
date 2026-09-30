/**
 * 对话流
 * ======
 *
 * 从 ChatView 里抽出来的对话业务逻辑：发消息、消费 SSE、把流式增量收尾成
 * 一条完整消息、停止生成、清空、载入历史消息。
 *
 * 改造前这些（含嵌套的 `finalize`）全写在视图的 `<script setup>` 里，
 * 和输入框、侧栏开关、移动端检测等纯 UI 状态混作一团；视图因此有 578 行，
 * 想单独测一下"流断了会不会卡死"都无从下手。
 *
 * 关于两条收尾路径（原实现的注释里强调过的坑，保留）：
 * `onDone` 与 `onClose` 只会触发一个（见 api/chat.js 的 settle 守卫），
 * 但**两条都必须收尾**。否则服务端在发 done 之前断开时，
 * `streaming` 会永久为 true，输入框永久禁用，只能刷新页面。
 */
import { onBeforeUnmount, ref, shallowRef } from 'vue'

import { streamChat } from '../api/chat.js'
import { parseStepLine, parseStoredReasoning } from '../utils/reasoning.js'
import { normalizeEvidenceList } from './useEvidence.js'
import { bumpTokenUsage } from './useTokenUsage.js'

// 消息的稳定 id：v-for 的 key 和"正在朗读哪条"都靠它。
// 原来用数组下标当 key，列表一变（载入历史、追加消息）就指向错的那条。
let seq = 0
const nextMessageId = () => `m${++seq}`

export function useChatStream() {
  /** @type {import('vue').ShallowRef<Array>} */
  const messages = shallowRef([])
  const conversationId = ref(null)

  const streaming = ref(false)
  /** 流式期间逐步累积的答案文本（仅用于渲染"正在打字"的那条） */
  const draftAnswer = ref('')
  /** 已解析好的推理步骤对象，直接累积而不是每帧重解析整段（原实现是 O(n²)） */
  const liveSteps = ref([])
  /** 推理模型的"内心独白"增量 */
  const thinkingText = ref('')
  /** 本轮检索到的证据（原始数组），收尾时快照到消息上 */
  const evidence = shallowRef([])
  /**
   * 已落库的轮次计数。
   *
   * 存在的理由：一轮问答结束后服务端多了一条记录，**历史侧栏得知道**。
   * 原来侧栏只在页面挂载时拉一次列表，于是"问了两轮，侧栏还显示
   * 『还没有对话』"（实测 bug）。与其让侧栏去猜（watch 消息数组长度会在
   * 载入历史时误触发），不如由流这边**明确报一个数**：
   * `useConversationHistory` 监听它刷新即可。
   */
  const turnSeq = ref(0)

  let abort = null

  function appendMessage(msg) {
    messages.value = [...messages.value, { id: nextMessageId(), ...msg }]
  }

  /**
   * 结束本轮（幂等）。
   *
   * `onDone` 带权威全文：用它替换流式期间累积的文本，保证"界面显示的"
   * 与"落库的"完全一致 —— ReAct 中间轮次的过渡语不会混进最终答案。
   */
  function finalize(answer, cid, { errored = false } = {}) {
    if (!streaming.value) return

    const finalText = (answer || draftAnswer.value || '').trim()
    if (finalText) {
      appendMessage({
        role: 'assistant',
        content: finalText,
        steps: liveSteps.value.slice(),
        thinking: thinkingText.value,
        evidence: evidence.value.slice(),
      })
      // 有正文 = 服务端确实落了库，侧栏该刷新了
      turnSeq.value += 1
    } else if (!errored) {
      // 一个字都没生成：明确告诉用户，而不是留下一片空白
      appendMessage({
        role: 'assistant',
        content: '（本轮没有生成内容，请重试或换个问法）',
        steps: [],
        thinking: '',
        evidence: [],
      })
    }

    if (cid && !conversationId.value) conversationId.value = cid

    streaming.value = false
    draftAnswer.value = ''
    liveSteps.value = []
    thinkingText.value = ''
    evidence.value = []
    abort = null

    // 服务端在 done 之前已把用量落库，这里正好通知统计面板刷新
    bumpTokenUsage()
  }

  /** 发送一条消息并开始接收流 */
  function send(text) {
    const content = String(text ?? '').trim()
    if (!content || streaming.value) return

    appendMessage({ role: 'user', content })
    streaming.value = true
    draftAnswer.value = ''
    liveSteps.value = []
    thinkingText.value = ''
    evidence.value = []

    let errored = false

    abort = streamChat(
      content,
      {
        // 整行"步骤"（工具调用 / 检索结果），后端可能附 icon
        onReasoning(line, icon) {
          const step = parseStepLine(line)
          if (!step) return
          if (icon && step.icon === '•') step.icon = icon
          liveSteps.value.push(step)
        },
        onReasoningDelta(delta) {
          thinkingText.value += delta
        },
        onAnswer(chunk) {
          draftAnswer.value += chunk
        },
        // 证据轨数据：本轮回答依据的知识库片段。
        // 立刻规范化 —— 流式期渲染的和最终落地的必须是同一套结构，
        // 否则字打完的瞬间证据样式会跳一下。
        onEvidence(list) {
          evidence.value = normalizeEvidenceList(list)
        },
        onDone(cid, answer) {
          finalize(answer, cid)
        },
        // 流结束但没收到 done（网络中断 / 服务端异常 / 超时）：必须收尾
        onClose(reason) {
          if (reason !== 'aborted') {
            console.warn('[chat] 流未正常结束:', reason)
          }
          finalize('', null)
        },
        onError(err) {
          errored = true
          appendMessage({ role: 'assistant', content: err, steps: [], thinking: '', evidence: [] })
          streaming.value = false
          draftAnswer.value = ''
          liveSteps.value = []
          thinkingText.value = ''
          abort = null
        },
      },
      conversationId.value,
    )
  }

  /** 用户主动停止。后端在取消路径上会保存已生成的内容，这里只收尾前端状态。 */
  function stop() {
    if (!abort) return
    abort.abort()
    abort = null
    streaming.value = false

    const partial = draftAnswer.value.trim()
    if (partial) {
      appendMessage({
        role: 'assistant',
        content: partial,
        steps: liveSteps.value.slice(),
        thinking: thinkingText.value,
        evidence: evidence.value.slice(),
      })
      // 后端在取消路径上也会把已生成的内容落库（见 api/sse_stream.py），
      // 所以"停止"同样产生一条历史记录，侧栏一样要刷新
      turnSeq.value += 1
    }
    draftAnswer.value = ''
    liveSteps.value = []
    thinkingText.value = ''
    evidence.value = []
  }

  function clear() {
    stop()
    messages.value = []
    conversationId.value = null
    draftAnswer.value = ''
    liveSteps.value = []
    thinkingText.value = ''
    evidence.value = []
  }

  /**
   * 用历史记录替换当前消息列表。
   *
   * `reasoning` 字段落库有两种格式（新的 JSON 含 steps/thinking、
   * 旧的纯文本行），统一在这里解析好再交给模板，避免模板里反复解析。
   */
  function loadHistoryMessages(list, cid = null) {
    clear()
    messages.value = (list || []).map((m) => {
      const parsed =
        m.role === 'assistant'
          ? parseStoredReasoning(m.reasoning)
          : { steps: [], thinking: '' }
      const legacy = parsed.legacy
      return {
        id: nextMessageId(),
        role: m.role,
        content: m.content,
        steps: parsed.steps,
        thinking: parsed.thinking,
        evidence: legacy ? [] : parsed.evidence || [],
      }
    })
    conversationId.value = cid
  }

  onBeforeUnmount(() => {
    if (abort) abort.abort()
  })

  return {
    messages,
    conversationId,
    streaming,
    draftAnswer,
    liveSteps,
    thinkingText,
    evidence,
    turnSeq,
    send,
    stop,
    clear,
    loadHistoryMessages,
  }
}
