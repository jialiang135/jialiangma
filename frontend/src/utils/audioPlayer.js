/**
 * 流式音频播放器（MSE + 降级）
 * ============================
 *
 * 目标：TTS 是"一句一句"流回来的 mp3 块，要边收边播、句与句之间不断档。
 *
 * 两条实现路径
 * ------------
 * 1. **MSE（MediaSource Extensions）—— 首选**
 *    把 mp3 块直接 append 进 ``SourceBuffer``，浏览器边解码边播。
 *    首句合成到一半就能出声，句间无缝 —— 这是"像豆包那样"的体感来源。
 *
 * 2. **逐句 Blob 队列 —— 降级**
 *    ``MediaSource.isTypeSupported('audio/mpeg')`` 在 Firefox / Safari 上为 false，
 *    此时退回：攒齐一句的 mp3 → Blob → 排队依次播放。
 *    代价是每句要等约 1~2 秒合成完才出声，但**不会没声音**。
 *
 * 降级是必要的：这条链路一旦在 Safari 上静默无声，用户只会认为"功能坏了"。
 */

const MSE_MIME = 'audio/mpeg'

/** 浏览器是否支持用 MSE 播放 mp3。 */
export function mseSupportsMp3() {
  try {
    return (
      typeof MediaSource !== 'undefined' &&
      typeof MediaSource.isTypeSupported === 'function' &&
      MediaSource.isTypeSupported(MSE_MIME)
    )
  } catch {
    return false
  }
}

export class SpeechPlayer {
  /**
   * @param {object} handlers
   * @param {() => void} handlers.onEnded 全部音频播放完毕
   * @param {(err: Error) => void} handlers.onError
   */
  constructor({ onEnded, onError } = {}) {
    this.onEnded = onEnded
    this.onError = onError

    this.mode = mseSupportsMp3() ? 'mse' : 'buffer'
    this.audio = null
    this.mediaSource = null
    this.sourceBuffer = null
    this.objectUrl = null

    this.pendingBuffers = [] // MSE：待 append 的块（SourceBuffer 一次只能 append 一个）
    this.sentenceChunks = [] // 降级：当前句已收到的块
    this.playlist = []       // 降级：待播的 blob URL 队列
    this.currentUrl = null   // 降级：正在播的 blob URL（null = 当前没有在播）

    this.stopped = false
    this.finished = false
    this.endedFired = false
  }

  /**
   * 开始一次播放。**必须在用户点击的调用栈里调用** ——
   * 浏览器的自动播放策略要求 play() 与用户手势在同一任务内，否则被拒。
   */
  async start() {
    this.stopped = false
    this.finished = false
    this.endedFired = false
    this.pendingBuffers = []
    this.sentenceChunks = []
    this.playlist = []
    this.currentUrl = null

    this.audio = new Audio()
    this.audio.preload = 'auto'
    this.audio.addEventListener('ended', () => this._handleEnded())
    this.audio.addEventListener('error', () => {
      // 降级模式下单个 blob 解码失败不该中断整段朗读
      if (this.mode === 'buffer' && this.playlist.length) {
        this._playNext()
        return
      }
      this.onError?.(new Error('音频播放失败'))
    })

    if (this.mode === 'mse') {
      try {
        await this._startMse()
      } catch (e) {
        // MSE 链路起不来（浏览器实现差异）→ 就地降级，别让用户没声音
        this.mode = 'buffer'
        this.onError?.(new Error(`流式播放不可用，已降级：${e.message}`))
      }
      // 此时可能还没有数据，play() 会 reject —— 属正常，数据到了会再试
      this._tryPlay()
    } else {
      this._unlockAudio()
    }
    return this.mode
  }

  /**
   * 降级模式下"解锁" audio 元素。
   *
   * 为什么需要：第一句音频要等 1~2 秒合成完才到，那时早已不在用户点击的
   * 调用栈里，直接 ``play()`` 会被自动播放策略拒绝 —— 表现就是**点了没声音**。
   * 先在手势内播一段极短静音，把这个元素的播放权限拿到手，
   * 之后再换 src 播放就顺理成章了。
   */
  _unlockAudio() {
    const SILENT_WAV =
      'data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQAAAAA='
    this.audio.src = SILENT_WAV
    this._tryPlay()
  }

  async _startMse() {
    this.mediaSource = new MediaSource()
    this.objectUrl = URL.createObjectURL(this.mediaSource)
    this.audio.src = this.objectUrl

    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('MediaSource 打开超时')), 5000)
      this.mediaSource.addEventListener(
        'sourceopen',
        () => {
          clearTimeout(timer)
          resolve()
        },
        { once: true },
      )
    })

    this.sourceBuffer = this.mediaSource.addSourceBuffer(MSE_MIME)
    this.sourceBuffer.addEventListener('updateend', () => this._drain())
    this.sourceBuffer.addEventListener('error', () => {
      this.onError?.(new Error('音频解码出错'))
    })
    // 直播式流：不设成 Infinity 的话，浏览器要等拿到足够时长才肯开播
    try {
      this.mediaSource.duration = Infinity
    } catch {
      /* 部分实现不接受 Infinity，忽略即可 */
    }
  }

  _tryPlay() {
    if (this.stopped || !this.audio) return
    const p = this.audio.play()
    if (p && typeof p.catch === 'function') {
      p.catch(() => {
        // 数据不足被拒：不是致命错误，等 _drain 里有数据了再试
      })
    }
  }

  /** 收到一个音频块。 */
  feed(arrayBuffer) {
    if (this.stopped) return
    if (this.mode === 'buffer') {
      this.sentenceChunks.push(arrayBuffer)
      return
    }
    this.pendingBuffers.push(new Uint8Array(arrayBuffer))
    this._drain()
  }

  _drain() {
    if (this.stopped || this.mode !== 'mse') return
    const sb = this.sourceBuffer
    if (!sb || sb.updating) return
    const next = this.pendingBuffers.shift()
    if (!next) return
    try {
      sb.appendBuffer(next)
    } catch {
      // QuotaExceededError：源缓冲满了。浏览器播完会自动腾出空间，
      // 把块放回去，等下一次 updateend 再试。
      this.pendingBuffers.unshift(next)
      return
    }
    if (this.audio && this.audio.paused) this._tryPlay()
  }

  /** 一句话合成完毕（降级模式下用它把当前句打包进播放队列）。 */
  endSentence() {
    if (this.stopped || this.mode !== 'buffer') return
    if (!this.sentenceChunks.length) return
    const blob = new Blob(this.sentenceChunks, { type: MSE_MIME })
    this.sentenceChunks = []
    this.playlist.push(URL.createObjectURL(blob))
    // 判断依据是"当前有没有在播"这个显式状态，不能用 audio.paused ——
    // 调过一次 play() 之后即使还没有 src，paused 也是 false，条件会永远不成立
    if (!this.currentUrl) this._playNext()
  }

  _playNext() {
    if (this.stopped) return
    // 上一句播完了，先把它的 blob URL 释放掉，否则整段朗读会持续泄漏内存
    if (this.currentUrl) {
      URL.revokeObjectURL(this.currentUrl)
      this.currentUrl = null
    }
    const url = this.playlist.shift()
    if (!url) {
      // 队列空了：整段若已结束就收尾，否则等下一句到来时再续上
      if (this.finished) this._fireEnded()
      return
    }
    this.currentUrl = url
    this.audio.src = url
    this._tryPlay()
  }

  /** 服务端已把整段回答发完。 */
  finish() {
    this.finished = true
    if (this.mode !== 'mse') {
      // 降级模式：末句可能还在攒，先打包；队列空且没有在播即整段结束
      this.endSentence()
      if (!this.playlist.length && !this.currentUrl) this._fireEnded()
      return
    }
    const tryEnd = () => {
      if (this.stopped) return
      if (this.pendingBuffers.length) {
        this._drain()
        setTimeout(tryEnd, 50)
        return
      }
      if (this.sourceBuffer?.updating) {
        setTimeout(tryEnd, 50)
        return
      }
      try {
        if (this.mediaSource?.readyState === 'open') this.mediaSource.endOfStream()
      } catch {
        /* 已关闭 */
      }
    }
    tryEnd()
  }

  _handleEnded() {
    if (this.mode === 'buffer') {
      // 释放当前句、接下一句；没有下一句且整段已结束则收尾
      this._playNext()
      return
    }
    // MSE：只有一个音轨，ended 即整段播完
    this._fireEnded()
  }

  _fireEnded() {
    if (this.endedFired) return
    this.endedFired = true
    this.onEnded?.()
  }

  /** 立即停止并释放资源。 */
  stop() {
    this.stopped = true
    this.pendingBuffers = []
    this.sentenceChunks = []
    for (const url of this.playlist) URL.revokeObjectURL(url)
    this.playlist = []
    if (this.currentUrl) {
      URL.revokeObjectURL(this.currentUrl)
      this.currentUrl = null
    }

    if (this.audio) {
      try {
        this.audio.pause()
        this.audio.removeAttribute('src')
        this.audio.load()
      } catch {
        /* 忽略释放期的异常 */
      }
      this.audio = null
    }
    if (this.mediaSource && this.mediaSource.readyState === 'open') {
      try {
        this.mediaSource.endOfStream()
      } catch {
        /* 已关闭 */
      }
    }
    if (this.objectUrl) {
      URL.revokeObjectURL(this.objectUrl)
      this.objectUrl = null
    }
    this.mediaSource = null
    this.sourceBuffer = null
  }
}
