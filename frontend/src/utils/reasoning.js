/**
 * 推理数据的解析工具
 *
 * 抽成共享模块的原因：`chat_logs.reasoning` 字段有两种历史格式，
 * 而在两处渲染（对话页的推理面板、管理页的对话日志详情）各写一份解析，
 * 已经导致过一次不一致 —— 存储格式改成 JSON 后，对话页做了兼容解析，
 * 管理页漏改，展开后直接显示 1500 多字的原始 JSON。
 *
 * 两种格式：
 *   新：JSON 字符串 `{"steps": ["🔍 ...", ...], "thinking": "..."}`
 *   旧：以换行分隔的步骤纯文本（更早的历史数据）
 */

// 行首 emoji（后端下发的步骤行都带前缀；旧数据可能没有，走关键词兜底）
const EMOJI_RE =
  /^([\u{1F300}-\u{1FAFF}\u{2700}-\u{27BF}\u{2600}-\u{26FF}\u{1F000}-\u{1F02F}\u{1F0A0}-\u{1F0FF}\u{1F100}-\u{1F64F}\u{1F680}-\u{1F6FF}\u{1F900}-\u{1F9FF}\u{200D}\u{FE0F}\u{20E3}\u{2000}-\u{206F}🛠️➕➖➡️〰️*️⃣#️⃣0️⃣1️⃣2️⃣3️⃣4️⃣5️⃣6️⃣7️⃣8️⃣9️⃣\u{1F7E0}-\u{1F7FF}]|⚠️|✅|❌|📚|📋|📝|📊|📭|💾|💬|🔍|🔀|🔧|🤔|🔄|✨|🎯|📌|🧩|📁|🏷️)/u

/** 把一整行步骤文本解析成 {icon, text}。 */
export function parseStepLine(line) {
  const t = (line || '').trim()
  if (!t) return null
  const m = t.match(EMOJI_RE)
  if (m) return { icon: m[0], text: t.slice(m[0].length).trim() }
  // 没有 emoji 前缀时按关键词猜一个图标（兼容旧数据）
  let icon = '•'
  if (t.includes('工具') || t.includes('搜索')) icon = '🔍'
  else if (t.includes('检索') || t.includes('知识库') || t.includes('匹配')) icon = '📚'
  else if (t.includes('拆解') || t.includes('分析') || t.includes('问题')) icon = '🤔'
  else if (t.includes('分支') || t.includes('判断') || t.includes('路由')) icon = '🔀'
  else if (t.includes('规划') || t.includes('方案')) icon = '📝'
  else if (t.includes('合规') || t.includes('校验') || t.includes('闭环')) icon = '✅'
  else if (t.includes('生成') || t.includes('回答')) icon = '➡️'
  else if (t.includes('返回') || t.includes('结果')) icon = '📋'
  else if (t.includes('评测') || t.includes('报告')) icon = '📊'
  else if (t.includes('保存')) icon = '💾'
  else if (t.includes('错误') || t.includes('失败') || t.includes('异常')) icon = '⚠️'
  return { icon, text: t }
}

/** 把多行步骤文本解析成 [{icon, text}]。 */
export function parseSteps(raw) {
  if (!raw) return []
  return raw.split('\n').filter(Boolean).map(parseStepLine).filter(Boolean)
}

/**
 * 解析落库的 reasoning 字段，返回 { steps, thinking }。
 * 新旧两种格式都能吃，见文件头说明。
 */
export function parseStoredReasoning(raw) {
  if (!raw) return { steps: [], thinking: '' }
  try {
    const obj = JSON.parse(raw)
    if (obj && typeof obj === 'object') {
      return {
        steps: parseSteps((obj.steps || []).join('\n')),
        thinking: obj.thinking || '',
      }
    }
  } catch {
    // 不是 JSON —— 按旧格式（纯文本行）处理
  }
  return { steps: parseSteps(raw), thinking: '' }
}
