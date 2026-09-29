/**
 * 格式化工具
 * ==========
 *
 * 抽出来的原因（原有的）：Token 统计面板与管理面板原先各写一份费用格式化，
 * 结果同一个数字在两处显示成 $0.00000 和 $0.0000 —— 不一致且都不可读。
 *
 * 后续新增（本次重构）：日期和时间。原先四个视图对同一个 `created_at`
 * 各切各的字符串 —— 对话页 `slice(5,19)`、管理页 `slice(0,19)`、
 * 知识库页 `slice(0,10)`、评测页 `slice(5,16)`，同一个时间戳四处显示不一致。
 */

/** 千分位整数，例如 3427 → "3,427" */
export function formatTokens(n) {
  return (Number(n) || 0).toLocaleString()
}

/**
 * 费用格式化：成本通常极小，固定小数位会显示成一串零。
 * 按量级选择精度，既读得出来又不占宽度。
 */
/**
 * 费用格式化。
 *
 * **带货币符号，且符号只在这里出现一次** —— 原来各处各写各的（有写 `$` 的、
 * 有忘了写的），改币种要满仓库找。
 *
 * 单位是**人民币**：DeepSeek 官方按元计价，分空闲/高峰两档。
 * 成本通常极小，固定小数位会显示成一串零，所以按量级选精度。
 */
export function formatCost(v) {
  const n = Number(v) || 0
  if (n === 0) return '¥0'
  if (n < 0.01) return `¥${n.toFixed(4)}`
  if (n < 1) return `¥${n.toFixed(3)}`
  return `¥${n.toFixed(2)}`
}

/**
 * 文件大小，例如 1536 → "1.5 KB"
 */
export function formatSize(bytes) {
  const n = Number(bytes) || 0
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

/**
 * 解析后端时间戳。
 *
 * 后端返回的多是 `"2026-09-29 05:52:29"` 这种带空格的格式，
 * 而 `new Date("2026-09-29 05:52:29")` 在 Safari/Firefox 上会得到 Invalid Date
 * （规范只保证 ISO 8601 的 `T` 分隔符）。所以统一把空格换成 `T` 再解析。
 * 原实现靠 `slice()` 切字符串绕过了这个问题，代价是各处格式不一致。
 */
function toDate(value) {
  if (!value) return null
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value
  const normalized = String(value).trim().replace(' ', 'T')
  const d = new Date(normalized)
  return Number.isNaN(d.getTime()) ? null : d
}

const pad = (n) => String(n).padStart(2, '0')

/** "2026-09-29" */
export function formatDate(value) {
  const d = toDate(value)
  if (!d) return ''
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

/** "09-29 05:52" */
export function formatDateTime(value) {
  const d = toDate(value)
  if (!d) return ''
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/**
 * 相对时间。
 *
 * 列表里用相对时间比绝对时间戳好读得多 —— 这是成熟产品的常见做法：
 * "3 分钟前" 一眼就知道新旧，而 "09-29 05:52" 还要在脑子里做减法。
 * 超过 7 天则退回具体日期，因为"37 天前"这种表述反而更难换算。
 */
export function formatRelativeTime(value) {
  const d = toDate(value)
  if (!d) return ''

  const diffMs = Date.now() - d.getTime()
  // 时间在未来（时钟漂移/服务端时区问题）时不显示"负几分钟前"
  if (diffMs < 0) return formatDateTime(value)

  const min = Math.floor(diffMs / 60000)
  if (min < 1) return '刚刚'
  if (min < 60) return `${min} 分钟前`

  const hour = Math.floor(min / 60)
  if (hour < 24) return `${hour} 小时前`

  const day = Math.floor(hour / 24)
  if (day < 7) return `${day} 天前`

  return formatDateTime(value)
}

/**
 * 截断长文本，超出部分以省略号结尾。
 * 原先是各页面内联的 slice(0, 40) / slice(0, 50)，阈值和省略号写法都不统一。
 */
export function truncate(text, max = 40) {
  const s = String(text ?? '')
  return s.length > max ? `${s.slice(0, max)}…` : s
}
