/**
 * Markdown 渲染
 * ==============
 *
 * 从 ChatView 里抽出来。原先 marked 的配置、净化白名单、以及"表格补空行"
 * 那个修补正则全写在视图的 `<script setup>` 里，别的页面（比如要看日志正文的
 * 管理页）想渲染 markdown 只能复制一份，或者干脆不渲染。
 *
 * 安全模型
 * --------
 * 渲染的内容来自两个**不可信来源**：LLM 输出，以及被检索到的**用户上传文档**。
 * 传一个含 `<img src=x onerror=...>` 的 md，不净化就会在对话区执行脚本。
 * 所以这里必须走 DOMPurify，而且用**白名单**而不是黑名单 ——
 * 不在列表里的标签/属性一律丢弃，新出现的攻击面默认是关的。
 *
 * 注意 marked v5 起**移除了内置的 sanitize 选项**，它现在不做任何净化，
 * 输出必须自己处理后再交给 `v-html`。
 */
import DOMPurify from 'dompurify'
import { marked } from 'marked'

marked.use({
  breaks: true, // 单个换行也转 <br>
  gfm: true, // 表格、任务列表、删除线等
})

/** 白名单式净化配置 */
const PURIFY_CONFIG = {
  ALLOWED_TAGS: [
    'p', 'br', 'hr', 'strong', 'em', 'del', 'code', 'pre', 'blockquote',
    'ul', 'ol', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'table', 'thead', 'tbody', 'tr', 'th', 'td', 'a', 'span',
  ],
  ALLOWED_ATTR: ['href', 'title', 'class'],
  // 只允许安全协议，挡掉 javascript: / data: 这类伪协议
  ALLOWED_URI_REGEXP: /^(?:https?|mailto):/i,
}

/**
 * GFM 表格要求表头行前有空行，而 LLM 输出经常紧贴着上一段
 * （`上一句\n| a | b |\n|---|---|`），不补空行整张表就退化成一行文字。
 */
const TABLE_HEADER_FIX = /([^\n])\n(\|[^\n]+\|\s*\n\|[-| :]+\|)/g

/* ============================================================
   中文粗体修补
   ------------------------------------------------------------
   CommonMark 的强调（emphasis）规则对中文标点不友好，实测：

     **「NWNU智问」**校园      → 不渲染，** 原样泄漏出来
     **「NWNU智问」** 校园     → 正常（多了个空格）
     **NWNU智问**校园          → 正常（没有中文标点）
     **"NWNU智问"**校园        → 也不渲染

   原因在 flanking 规则：闭合的 `**` 若**前面是标点**、
   **后面是字母**，就不满足 right-flanking，不能闭合强调。
   而"粗体结尾是引号/书名号，紧接着继续写中文"在中文里极其常见，
   所以 LLM 的回答里这会**频繁出现** —— 用户看到的就是一串多余的星号。

   修法：绕过 marked 的强调解析，直接把 `**x**` 换成 `<strong>x</strong>`。
   因为最终还要过 DOMPurify 白名单（strong 在名单里），这样替换是安全的。

   两个约束：
   1. `[^*\n]+?` —— 内容里不含星号、不跨行，避免把真正的字面星号
      （比如 Python 项目里常见的递归通配写法）错配成长强调
   2. **必须跳过代码** —— 代码里出现星号太正常了，改了就是破坏代码
   ============================================================ */

const FENCED_CODE_RE = /```[\s\S]*?```/g
const INLINE_CODE_RE = /`[^`\n]*`/g
const BOLD_RE = /\*\*([^*\n]+?)\*\*/g

/**
 * 把文本里的代码片段抽出来占位，避免后续的文本处理误伤代码。
 * 用 \u0000 包住序号 —— 这个字符不会出现在正常文本里。
 */
function stashCode(text) {
  const slots = []
  const stashed = text
    .replace(FENCED_CODE_RE, (m) => `\u0000${slots.push(m) - 1}\u0000`)
    .replace(INLINE_CODE_RE, (m) => `\u0000${slots.push(m) - 1}\u0000`)
  return { stashed, slots }
}

function restoreCode(text, slots) {
  return text.replace(/\u0000(\d+)\u0000/g, (_, i) => slots[Number(i)])
}

function fixCjkBold(text) {
  const { stashed, slots } = stashCode(text)
  const fixed = stashed.replace(BOLD_RE, '<strong>$1</strong>')
  return restoreCode(fixed, slots)
}

/**
 * 把 Markdown 渲染成**已净化**的 HTML。
 *
 * 返回值只应交给 `v-html`。调用方要保证外层有 `.md-body` 类，
 * 否则排版样式（`src/styles/base.css` 里那套）不会生效。
 */
export function renderMarkdown(text) {
  if (!text) return ''
  const withTables = String(text).replace(TABLE_HEADER_FIX, '$1\n\n$2')
  const withBold = fixCjkBold(withTables)
  return DOMPurify.sanitize(marked(withBold), PURIFY_CONFIG)
}

/* ============================================================
   反向操作：把 Markdown 剥成纯文本
   ------------------------------------------------------------
   用途是**证据轨的片段展示**。那些片段是知识库文件原文，
   直接显示会露出 `## 标题`、`**加粗**`、表格竖线 —— 面试官看到的是噪声，
   而这里只需要"这段文字讲了什么"。

   注意与 renderMarkdown 的区别：那个是**渲染**（交给 v-html），
   这个是**剥离**（得到可安全插值的纯字符串），返回内容不含任何 HTML。
   ============================================================ */

const IMAGE_RE = /!\[[^\]]*\]\([^)]*\)/g
const LINK_RE = /\[([^\]]*)\]\([^)]*\)/g
const TABLE_SEP_RE = /^\s*\|?[\s:\-|]+\|[\s:\-|]*$/gm
const HEADING_RE = /^\s{0,3}#{1,6}\s*/gm
const BULLET_RE = /^\s{0,3}(?:[-*+]|\d+\.)\s+/gm
const QUOTE_RE = /^\s{0,3}>\s?/gm
const HR_RE = /^\s*(?:[-*_]\s*){3,}$/gm
const EMPHASIS_RE = /(\*\*|__|\*|_)/g
const TABLE_PIPE_RE = /\s*\|\s*/g

/**
 * 把 Markdown 剥成可读纯文本。
 *
 * 与后端 `core/tts.py` 的 `strip_markdown` 思路一致（那边是为了朗读、
 * 这边是为了展示），但**代码块保留内容**而不是整块丢弃 ——
 * 朗读代码没意义，而展示证据时"这里有一段代码"本身是有信息量的。
 */
export function plainTextFromMarkdown(text) {
  if (!text) return ''

  let out = String(text)
    // 围栏代码块：去掉 ``` 行和语言标识，保留代码正文
    .replace(FENCED_CODE_RE, (block) =>
      block.replace(/^```[^\n]*\n?/, '').replace(/\n?```$/, ''),
    )
    .replace(IMAGE_RE, '')
    .replace(LINK_RE, '$1')
    .replace(INLINE_CODE_RE, '$1')
    .replace(HR_RE, '')
    .replace(TABLE_SEP_RE, '')
    .replace(HEADING_RE, '')
    .replace(BULLET_RE, '')
    .replace(QUOTE_RE, '')

  // 表格竖线换成中点：比逗号更像"并列"，也不会和正文标点混在一起
  out = out.replace(TABLE_PIPE_RE, ' · ')
  out = out.replace(EMPHASIS_RE, '')

  return out
    .replace(/[ \t]+/g, ' ')
    .replace(/\n{2,}/g, '\n')
    .replace(/ ·\s*\n/g, '\n') // 行尾多余的分隔符
    .replace(/(?:·\s*){2,}/g, '· ')
    .trim()
}
