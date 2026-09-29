import { toSpeechChunks } from '../utils/speech.js'

/**
 * TTS 语音合成 WebSocket 客户端
 * =============================
 *
 * 协议见服务端 api/routes/tts_routes.py 的模块 docstring。要点：
 * - **首条消息必须是 auth**（token 不放 query，避免落进 Nginx 访问日志）
 * - 服务端按收到的顺序**串行**合成，所以这里只管顺序发、不用管并发
 * - 音频以**二进制帧**回来，文本帧都是 JSON 控制消息
 *
 * 一次"朗读"的生命周期：
 *   connect() → ready → 逐句 speak() → 每句 started/音频/done → finish 后 close()
 */

// 与服务端一致：连上后必须在该时间内发出 auth
const CONNECT_TIMEOUT_MS = 10000

/** 拼出 WebSocket 地址（走当前页面的 host，部署后由 Nginx 转发）。 */
export function ttsWsUrl() {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${window.location.host}/api/tts/stream`
}

export class TtsClient {
  /**
   * @param {object} handlers
   * @param {(chunk: ArrayBuffer) => void} handlers.onAudio 音频块
   * @param {(id: any) => void} handlers.onSentenceStart
   * @param {(id: any) => void} handlers.onSentenceEnd
   * @param {(message: string) => void} handlers.onError
   * @param {() => void} handlers.onClose 连接关闭（含异常断开）
   */
  constructor(handlers = {}) {
    this.handlers = handlers
    this.ws = null
    this.closedByUs = false
  }

  /**
   * 建立连接并完成认证。
   * @returns {Promise<void>} 认证通过（收到 ready）时 resolve；失败时 reject
   */
  connect() {
    if (this.ws) return Promise.resolve()

    const token = localStorage.getItem('token')
    if (!token) return Promise.reject(new Error('未登录'))

    return new Promise((resolve, reject) => {
      let settled = false
      let ws
      try {
        ws = new WebSocket(ttsWsUrl())
      } catch (e) {
        reject(new Error(`WebSocket 创建失败：${e.message}`))
        return
      }
      ws.binaryType = 'arraybuffer'
      this.ws = ws

      const timer = setTimeout(() => {
        if (settled) return
        settled = true
        try { ws.close() } catch { /* 已在关闭中 */ }
        reject(new Error('连接语音服务超时'))
      }, CONNECT_TIMEOUT_MS)

      const fail = (msg) => {
        if (settled) return
        settled = true
        clearTimeout(timer)
        reject(new Error(msg))
      }

      ws.onopen = () => {
        ws.send(JSON.stringify({ type: 'auth', token }))
      }

      ws.onmessage = (ev) => {
        if (typeof ev.data !== 'string') {
          // 认证之前不该有音频帧；真收到也交给上层，不丢数据
          this.handlers.onAudio?.(ev.data)
          return
        }
        let msg
        try {
          msg = JSON.parse(ev.data)
        } catch {
          return
        }
        switch (msg.type) {
          case 'ready':
            if (!settled) {
              settled = true
              clearTimeout(timer)
              resolve()
            }
            break
          case 'started':
            this.handlers.onSentenceStart?.(msg.id)
            break
          case 'done':
            this.handlers.onSentenceEnd?.(msg.id)
            break
          case 'error':
            // 认证失败/服务不可用都会走这里：先 reject（若还在连接阶段），再上报
            fail(msg.message || '语音服务返回错误')
            this.handlers.onError?.(msg.message || '语音服务返回错误')
            break
          default:
            break
        }
      }

      ws.onerror = () => {
        // onerror 不带原因，具体信息在 onclose；这里只在连接阶段报错
        fail('语音服务连接失败')
      }

      ws.onclose = (ev) => {
        clearTimeout(timer)
        this.ws = null
        if (!settled) {
          settled = true
          reject(new Error(`语音服务连接被关闭（code ${ev.code}）`))
          return
        }
        if (!this.closedByUs) this.handlers.onClose?.()
      }
    })
  }

  get connected() {
    return !!this.ws && this.ws.readyState === WebSocket.OPEN
  }

  /** 送一句去合成。服务端会剥离 Markdown 后再合成。 */
  speak(id, text) {
    if (!this.connected) return
    this.ws.send(JSON.stringify({ type: 'speak', id, text }))
  }

  /**
   * 朗读整段回答：切句后逐句送出（服务端串行合成）。
   * @returns {number} 送出的句数
   */
  speakAll(text) {
    const chunks = toSpeechChunks(text)
    chunks.forEach((chunk, i) => this.speak(i, chunk))
    return chunks.length
  }

  /** 停止当前合成并丢弃排队中的句子。 */
  cancel() {
    if (!this.connected) return
    this.ws.send(JSON.stringify({ type: 'cancel' }))
  }

  /** 主动关闭（用户停止朗读 / 组件卸载）。 */
  close() {
    this.closedByUs = true
    if (!this.ws) return
    try {
      this.ws.close()
    } catch {
      /* 已在关闭中 */
    }
    this.ws = null
  }
}
