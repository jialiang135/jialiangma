/**
 * 推理数据的解析工具
 * ==================
 *
 * 抽成共享模块的原因（原有）：`chat_logs.reasoning` 字段有两种历史格式，
 * 而在两处渲染（对话页的推理面板、管理页的对话日志详情）各写一份解析，
 * 已经导致过一次不一致 —— 存储格式改成 JSON 后，对话页做了兼容解析，
 * 管理页漏改，展开后直接显示 1500 多字的原始 JSON。
 *
 * 两种格式：
 *   新：JSON 字符串 `{"steps": [...], "thinking": "...", "evidence": [...]}`
 *   旧：以换行分隔的步骤纯文本（更早的历史数据）
 *
 * 本次重构的改动
 * --------------
 * 后端下发的步骤行**自带 emoji 前缀**（`"📚 检索到 5 条相关知识"`）。
 * 原来前端把 emoji 原样塞进 `reasoning.js` 的图标字段再渲染出来，
 * 于是推理面板里又是一堆 emoji。
 *
 * 现在改为**映射成 UiIcon 的名字**，在展示层把它翻译成统一的内联图标。
 * 这样后端的契约不用动（它继续发 emoji，语义也在），
 * 前端却能保证图标风格全站一致 —— 契约归契约，呈现归呈现。
 */

// 行首 emoji 前缀
const EMOJI_RE =
  /^([\u{1F300}-\u{1FAFF}\u{2700}-\u{27BF}\u{2600}-\u{26FF}\u{1F000}-\u{1F02F}\u{1F0A0}-\u{1F0FF}\u{1F100}-\u{1F64F}\u{1F680}-\u{1F6FF}\u{1F900}-\u{1F9FF}\u{200D}\u{FE0F}\u{20E3}\u{2000}-\u{206F}🛠️➕➖➡️〰️*️⃣#️⃣0️⃣1️⃣2️⃣3️⃣4️⃣5️⃣6️⃣7️⃣8️⃣9️⃣\u{1F7E0}-\u{1F7FF}]|⚠️|✅|❌|📚|📋|📝|📊|📭|💾|💬|🔍|🔀|🔧|🤔|🔄|✨|🎯|📌|🧩|📁|🏷️)/u

/**
 * 后端 emoji → 图标名。
 * 只映射 UiIcon 里真实存在的图标；找不到的落到默认的 'zap'（推理步骤）。
 */
const EMOJI_TO_ICON = {
  '🔍': 'search',
  '📚': 'book',
  '📁': 'file',
  '📄': 'file',
  '🤔': 'zap',
  '🔀': 'layers',
  '📝': 'list',
  '📋': 'list',
  '✅': 'check',
  '➡️': 'send',
  '📊': 'chart',
  '💾': 'database',
  '🔧': 'sliders',
  '🛠️': 'sliders',
  '⚠️': 'alert',
  '❌': 'close',
  '💬': 'message',
  '🔄': 'refresh',
  '✨': 'zap',
  '🎯': 'check',
  '📌': 'list',
  '🧩': 'layers',
  '🏷️': 'file',
  '📭': 'file',
}

/** 没有 emoji 前缀时的关键词兜底（旧数据），按语义猜图标名 */
function guessIcon(text) {
  if (text.includes('工具') || text.includes('搜索')) return 'search'
  if (text.includes('检索') || text.includes('知识库') || text.includes('匹配')) return 'book'
  if (text.includes('拆解') || text.includes('分析') || text.includes('问题')) return 'zap'
  if (text.includes('分支') || text.includes('判断') || text.includes('路由')) return 'layers'
  if (text.includes('规划') || text.includes('方案')) return 'list'
  if (text.includes('合规') || text.includes('校验') || text.includes('闭环')) return 'check'
  if (text.includes('生成') || text.includes('回答')) return 'send'
  if (text.includes('返回') || text.includes('结果')) return 'list'
  if (text.includes('评测') || text.includes('报告')) return 'chart'
  if (text.includes('保存')) return 'database'
  if (text.includes('错误') || text.includes('失败') || text.includes('异常')) return 'alert'
  return 'zap'
}

/**
 * 把一整行步骤文本解析成 `{ iconName, text }`。
 * emoji 前缀会被剥掉并换成图标名，正文里不会再有 emoji。
 */
export function parseStepLine(line) {
  const t = (line || '').trim()
  if (!t) return null

  const m = t.match(EMOJI_RE)
  if (m) {
    const rest = t.slice(m[0].length).trim()
    return { iconName: EMOJI_TO_ICON[m[0]] || guessIcon(rest), text: rest || t }
  }
  return { iconName: guessIcon(t), text: t }
}

/** 把多行步骤文本解析成 `[{iconName, text}]`。 */
export function parseSteps(raw) {
  if (!raw) return []
  return raw
    .split('\n')
    .filter(Boolean)
    .map(parseStepLine)
    .filter(Boolean)
}

function normalizeEvidence(list) {
  if (!Array.isArray(list)) return []
  return list
    .filter((e) => e && typeof e === 'object')
    .map((e, i) => ({
      key: `${e.source ?? 'unknown'}#${e.chunk_idx ?? i}`,
      source: String(e.source ?? '未知来源'),
      score: Number.isFinite(Number(e.score)) ? Number(e.score) : null,
      content: String(e.content ?? ''),
      chunkIdx: e.chunk_idx ?? null,
    }))
}

/**
 * 解析落库的 reasoning 字段。
 *
 * @returns {{steps: Array, thinking: string, evidence: Array, legacy: boolean}}
 *   `legacy=true` 表示这是旧格式（纯文本行），既没有 thinking 也没有 evidence，
 *   调用方据此决定要不要隐藏"证据轨"入口 —— 而不是显示一个永远空的面板。
 */
export function parseStoredReasoning(raw) {
  const empty = { steps: [], thinking: '', evidence: [], legacy: false }
  if (!raw) return empty

  try {
    const obj = JSON.parse(raw)
    if (obj && typeof obj === 'object') {
      return {
        steps: parseSteps((obj.steps || []).join('\n')),
        thinking: obj.thinking || '',
        evidence: normalizeEvidence(obj.evidence),
        legacy: false,
      }
    }
  } catch {
    // 不是 JSON —— 按旧格式（纯文本行）处理
  }
  return { steps: parseSteps(raw), thinking: '', evidence: [], legacy: true }
}
