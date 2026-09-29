/**
 * 朗读文本预处理
 * ==============
 *
 * 前端只做**切句**和**丢代码块**两件事，不做完整的 Markdown 剥离 ——
 * 剥离由服务端统一负责（见 core/tts.py 的 strip_markdown）。
 *
 * 那为什么还要在这儿丢代码块？因为切句必须**先**丢：
 * 一个 ``` 围起来的代码块如果被切句切开，前半段的 ``` 和后半段的 ``` 就分处
 * 两个片段，服务端的 code-fence 正则会两侧都匹配不上，结果是代码被逐字念出来。
 * 所以顺序是「先整体丢代码块 → 再切句 → 再交给服务端剥离剩余标记」。
 */

// 围栏代码块 ```...```（含语言标识行），整块丢弃
const FENCED_CODE_RE = /```[\s\S]*?```/g
// 句子边界：中文句末标点 / 英文句末标点 / 换行
const SENTENCE_SPLIT_RE = /(?<=[。！？；!?;])\s*|\n+/

/**
 * 把一段（可能含 Markdown 的）回答切成适合逐句合成的片段。
 *
 * @param {string} text 原始回答文本（Markdown 原文即可）
 * @param {object} [options]
 * @param {number} [options.minLength=4]  短于该长度的片段与下一句合并，
 *        避免"嗯。""对。"这种碎片各占一次合成请求（每次都有网络开销）
 * @param {number} [options.maxLength=300] 超过该长度强制在逗号/空格处断开，
 *        避免整段没有句号时长句顶到服务端 1000 字上限被截断
 * @returns {string[]} 片段列表
 */
export function toSpeechChunks(text, { minLength = 4, maxLength = 300 } = {}) {
  if (!text) return []

  const withoutCode = String(text).replace(FENCED_CODE_RE, '\n')
  const raw = withoutCode
    .split(SENTENCE_SPLIT_RE)
    .map((s) => s.trim())
    .filter(Boolean)

  // 先按 minLength 合并碎片
  const merged = []
  for (const piece of raw) {
    if (merged.length && merged[merged.length - 1].length < minLength) {
      merged[merged.length - 1] += piece
    } else {
      merged.push(piece)
    }
  }

  // 再把过长的片段按标点拆开
  const result = []
  for (const piece of merged) {
    if (piece.length <= maxLength) {
      result.push(piece)
      continue
    }
    let rest = piece
    while (rest.length > maxLength) {
      // 优先在标点处断，其次空格，最后硬切
      const window = rest.slice(0, maxLength)
      let cut = Math.max(
        window.lastIndexOf('，'),
        window.lastIndexOf(','),
        window.lastIndexOf('、'),
        window.lastIndexOf(' '),
      )
      if (cut < maxLength * 0.5) cut = maxLength - 1
      result.push(rest.slice(0, cut + 1).trim())
      rest = rest.slice(cut + 1)
    }
    if (rest.trim()) result.push(rest.trim())
  }

  return result.filter(Boolean)
}
