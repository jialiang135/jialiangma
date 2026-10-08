/**
 * HTTP 封装
 * ==========
 *
 * 改造前的问题：
 * - `uploadFiles` / `uploadFile` 没有 401 处理 —— 登录态过期时会对错误响应体
 *   直接 `res.json()`，用户看到的是解析报错而不是"请重新登录"
 * - 缺少 `put`，于是 admin.js 手写了两处裸 `fetch`，那两处又各自漏了 401 处理
 * - `kb.js` 的 `rebuildKb` 把 401 处理**复制粘贴**了一遍
 *
 * 现在把鉴权相关的三件事收敛到这里：**取 token**、**拼请求头**、
 * **处理 401**。任何走裸 fetch 的地方（流式对话、文件上传）都复用它，
 * 避免"有的地方会跳登录、有的地方不会"。
 */

/**
 * ⚠️ 后端返回有**三种信封形状**，消费时必须知道自己在接哪一种。
 *
 * 这不是设计得漂亮，是历史遗留 —— 但它是"界面永远显示空"这类 bug 的高发区：
 * 读错一层就拿到 undefined，而构建、控制台、接口全都不报错。
 * （已经踩过一次：用量页读 `res.totals`，而接口实际返回
 *   `{success, message, data:{totals}}`，于是页面一直空着。）
 *
 *   A. **裸对象**（字段直接在顶层）
 *      /auth/*、/chat/history、/chat/conversation(s)、/kb/files
 *      → 消费：`res.files`、`res.conversations`
 *
 *   B. **success 平铺**（`{success, ...字段}`）
 *      /eval/testsets、/eval/reports、/eval/compare、
 *      /kb/upload-status、/tools/categories
 *      → 消费：`res.reports`、`res.data`（个别接口把结果放在 data 键下）
 *
 *   C. **data 嵌套**（`{success, message, data:{...}}`，即 APIResponse）
 *      /eval/run、/kb/chunking、/kb/chunking/preview、/kb/formats、
 *      /kb/files/{id}/chunks|content、/kb/upload、/kb/rebuild、/kb/clear、
 *      /kb/files/{id}(DELETE)、/token/stats
 *      → 消费：`res.data.xxx`
 *
 * 什么时候该统一：**等真要做一次前后端契约整理时**。现在改要动 30 个路由 +
 * 30 个消费点，收益只是"少一个心眼"，回归风险却是实打实的 —— 所以先记在这里，
 * 别为了整齐去动它。
 *
 * 另外注意 `useAsyncData` **原样存 res、不做拆包**，所以上面这条对每个
 * 消费点都适用。
 */

const BASE = '/api'

/** 读取当前 token */
export function getToken() {
  return localStorage.getItem('token')
}

/** 带鉴权的请求头 */
export function authHeaders(extra = {}) {
  const token = getToken()
  return {
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...extra,
  }
}

/**
 * 登录态失效：清掉本地凭据并广播事件。
 *
 * 唯一监听方是 `stores/auth.js`，它收到后把 store 里的登录状态也清掉，
 * 界面才会切回未登录态。**所有拿到 401 的地方都必须调用它**，
 * 否则界面会停在一个"看起来已登录但所有请求都失败"的状态。
 */
export function notifyAuthExpired() {
  localStorage.removeItem('token')
  localStorage.removeItem('user')
  window.dispatchEvent(new CustomEvent('auth-expired'))
}

/**
 * 从错误响应里挖出可读的说明。
 *
 * 后端有几种错误体形态：FastAPI 的 `{detail: "..."}`（detail 也可能是对象）、
 * 自定义的 `{error: "..."}`、以及 `{message: "..."}`。都试一遍。
 */
async function extractError(res) {
  let detail = ''
  try {
    const body = await res.json()
    const raw = body?.detail ?? body?.error ?? body?.message
    if (typeof raw === 'string') detail = raw
    else if (raw) detail = JSON.stringify(raw)
  } catch {
    // 响应体不是 JSON（比如 Nginx 返回的 HTML 错误页）
  }
  return detail || res.statusText || `HTTP ${res.status}`
}

/**
 * 非 2xx 一律抛错，并做 401 处理。`readJson` 与 `getBlob` 共用。
 *
 * 401 要分两种情况：
 * - **本来就有登录态** → 会话过期，清凭据并广播，界面切回未登录
 * - **本来没有登录态** → 这是登录接口自己返回的"用户名或密码错误"，
 *   不能清凭据、更不能广播 auth-expired，否则用户看到的是
 *   "登录已过期"而不是"密码错了"，一脸茫然
 */
async function ensureOk(res) {
  if (res.ok) return
  const message = await extractError(res)
  if (res.status === 401 && getToken()) {
    notifyAuthExpired()
  }
  const err = new Error(message)
  err.status = res.status
  throw err
}

/**
 * 统一处理响应。
 *
 * **非 2xx 一律抛错**。改造前这里只在 401 抛错，其余状态码（如 500）
 * 会把响应体当**正常数据**返回给调用方 —— 于是"请求失败了"被当成
 * "拿到了一个字段不全的成功响应"，界面既不报错也不重试，
 * 只是安静地显示空白。管理页那几个"加载失败 + 重试"的提示因此永远不触发。
 */
export async function readJson(res) {
  await ensureOk(res)
  return res.json()
}

/**
 * 取**二进制**响应（原文件预览这类）。
 *
 * 单独一个函数而不是复用 `get`：`readJson` 会 `res.json()`，二进制直接解析失败。
 * 401 处理必须同样走 `notifyAuthExpired`，否则预览失败时界面不会切回登录态。
 *
 * 注意为什么不能用 `<iframe src="/api/...">` 直接指：那个请求**带不上
 * Authorization 头**（JWT 存在 localStorage 里）。所以必须 fetch 成 blob，
 * 再用 `URL.createObjectURL` 给浏览器。用完记得 revoke，否则整个 blob 常驻内存。
 */
export async function getBlob(url, params = {}) {
  const qs = new URLSearchParams(params).toString()
  const res = await fetch(`${BASE}${qs ? `${url}?${qs}` : url}`, { headers: authHeaders() })
  await ensureOk(res)
  return res.blob()
}


async function request(url, options = {}) {
  const res = await fetch(`${BASE}${url}`, {
    ...options,
    headers: authHeaders({
      'Content-Type': 'application/json',
      ...options.headers,
    }),
  })
  return readJson(res)
}

export async function get(url, params = {}) {
  const qs = new URLSearchParams(params).toString()
  return request(qs ? `${url}?${qs}` : url)
}

export async function post(url, data = {}) {
  return request(url, { method: 'POST', body: JSON.stringify(data) })
}

export async function put(url, data = {}) {
  return request(url, { method: 'PUT', body: JSON.stringify(data) })
}

export async function del(url) {
  return request(url, { method: 'DELETE' })
}

/** multipart 上传：不能设 Content-Type，交给浏览器带 boundary */
async function uploadFetch(url, form) {
  const res = await fetch(`${BASE}${url}`, {
    method: 'POST',
    headers: authHeaders(),
    body: form,
  })
  return readJson(res)
}

export async function uploadFiles(url, files) {
  const form = new FormData()
  for (const f of files) form.append('files', f)
  return uploadFetch(url, form)
}

export async function uploadFile(url, file, fieldName = 'file') {
  const form = new FormData()
  form.append(fieldName, file)
  return uploadFetch(url, form)
}
