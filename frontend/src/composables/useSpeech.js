/**
 * 朗读（TTS）
 * ===========
 *
 * 从 ChatView 里抽出来。原先这 100 多行（TtsClient 与 SpeechPlayer 的
 * 构造、接线、错误处理、停止逻辑）直接内联在视图的 `<script setup>` 里，
 * 和对话逻辑混在一起 —— 换任何页面想加朗读都得复制一遍。
 *
 * 关键顺序（原实现踩过的坑，注释保留下来）：
 * **必须先 `player.start()` 再 `client.connect()`**。播放器的 `play()` 要和
 * 用户点击处在同一个任务里，否则被浏览器的自动播放策略拒绝 —— 表现就是
 * "点了没声音"。而 `await connect()` 会把手势用掉。
 */
import { onBeforeUnmount, ref } from 'vue'

import { TtsClient } from '../api/tts.js'
import { SpeechPlayer } from '../utils/audioPlayer.js'

/**
 * @param {object} opts
 * @param {(text: string) => void} [opts.onUnreadable] 整段都不可朗读时的回调
 */
export function useSpeech({ onUnreadable } = {}) {
  /**
   * 正在朗读的消息 id。
   * 用消息的稳定 id 而不是数组下标 —— 下标在列表变动（加载历史、
   * 追加新消息）时会指向别的消息，按钮的"停止"态就会跑到别人身上。
   */
  const speakingId = ref(null)
  const error = ref('')

  let client = null
  let player = null

  /** 立即停止并释放连接与音频资源（幂等） */
  function stop() {
    if (client) {
      // 先让服务端丢弃排队中的句子，再断开 —— 否则它会继续合成没人听的音频
      client.cancel()
      client.close()
      client = null
    }
    if (player) {
      player.stop()
      player = null
    }
    speakingId.value = null
    error.value = ''
  }

  /** 朗读 / 停止同一条消息 */
  async function toggle(id, text) {
    if (speakingId.value === id) {
      stop()
      return
    }
    stop()
    speakingId.value = id

    let lastId = -1

    const p = new SpeechPlayer({
      onEnded: () => {
        if (speakingId.value === id) stop()
      },
      onError: (e) => {
        error.value = e?.message || '播放失败'
      },
    })
    player = p

    const c = new TtsClient({
      onAudio: (chunk) => p.feed(chunk),
      onSentenceEnd: (sid) => {
        p.endSentence()
        if (sid === lastId) p.finish()
      },
      onError: (m) => {
        error.value = m
      },
      onClose: () => {
        // 非主动关闭（服务端断开）：收尾，避免按钮卡在"停止"态
        if (speakingId.value === id) stop()
      },
    })
    client = c

    try {
      await p.start()
      await c.connect()
      lastId = c.speakAll(text) - 1
      if (lastId < 0) {
        // 整段都是代码块之类不可朗读的内容
        disconnect()
        speakingId.value = id
        error.value = '这条回答没有可朗读的内容'
        onUnreadable?.(text)
      }
    } catch (e) {
      // 失败原因要留住：stop() 会清空 error，所以这里手动释放
      disconnect()
      speakingId.value = id
      error.value = e?.message || '语音服务不可用'
    }
  }

  /** 只释放资源、不动 speakingId / error（给错误分支用） */
  function disconnect() {
    if (player) {
      player.stop()
      player = null
    }
    if (client) {
      client.close()
      client = null
    }
  }

  onBeforeUnmount(stop)

  return { speakingId, error, toggle, stop }
}
