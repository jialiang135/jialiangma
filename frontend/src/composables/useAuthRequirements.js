/**
 * 登录页的校验规则
 * ================
 *
 * 注册页的"实时校验清单"（密码至少 N 位 / 含数字 / 含字母）**不自己写规则**，
 * 而是照后端 `GET /api/auth/requirements` 返回的内容渲染。
 *
 * 为什么费这个事：原先的限制散在前端 `if (password.length < 8)` 和后端
 * `validate_password_strength()` 两处，改一处必漏另一处。而且规则只在**提交失败后**
 * 才通过报错暴露，用户得先撞一次墙才知道要求。现在规则是后端给的，
 * 边输入边显示满足情况。
 *
 * 关于字符类判定的一个细节
 * ------------------------
 * 后端的 `str.isdigit()` / `str.isalpha()` 是 **Unicode 感知**的
 * （全角数字、中文字符都算数）。所以这里用 `\p{Nd}` / `\p{L}` 而不是
 * `[0-9]` / `[a-zA-Z]`，尽量贴近后端语义。
 *
 * 但它**依然只是提示**：最终判定以后端为准（前端过了后端也可能拒）。
 * 这样即使两边有边缘差异，也不会出现"前端说行、提交后却失败还没提示"。
 */
import { computed } from 'vue'

import { getAuthRequirements } from '../api/auth.js'
import { useAsyncData } from './useAsyncData.js'

const HAS_DIGIT = /\p{Nd}/u
const HAS_LETTER = /\p{L}/u

export function useAuthRequirements() {
  const { data, loading, error, run } = useAsyncData(() => getAuthRequirements())

  const requirements = computed(() => data.value || null)
  /** 注册是否开放；拿不到规则时保守当作不开放，避免让用户白填一遍 */
  const allowRegistration = computed(() => data.value?.allow_registration === true)

  /**
   * 密码的实时校验清单。
   * 每项带 `test`，视图里逐项跑一遍就知道该打勾还是打叉。
   */
  const passwordRules = computed(() => {
    const r = data.value
    if (!r) return []

    const rules = [
      {
        key: 'length',
        label: `至少 ${r.password_min_length} 位字符`,
        test: (pwd) => pwd.length >= r.password_min_length,
      },
    ]
    if (r.password_require_digit) {
      rules.push({ key: 'digit', label: '包含至少一个数字', test: (pwd) => HAS_DIGIT.test(pwd) })
    }
    if (r.password_require_letter) {
      rules.push({ key: 'letter', label: '包含至少一个字母', test: (pwd) => HAS_LETTER.test(pwd) })
    }
    return rules
  })

  /** 用户名长度规则（后端 schema 里也是同一组常量） */
  const usernameError = computed(() => {
    const r = data.value
    if (!r) return ''
    return `用户名需 ${r.username_min_length}~${r.username_max_length} 位`
  })

  function checkUsername(value) {
    const r = data.value
    if (!r) return true
    const n = String(value ?? '').length
    return n >= r.username_min_length && n <= r.username_max_length
  }

  /** 逐项评估密码，返回 [{key, label, ok}] */
  function evaluatePassword(pwd) {
    const p = String(pwd ?? '')
    return passwordRules.value.map((rule) => ({
      key: rule.key,
      label: rule.label,
      ok: rule.test(p),
    }))
  }

  return {
    requirements,
    loading,
    error,
    load: run,
    allowRegistration,
    passwordRules,
    usernameError,
    checkUsername,
    evaluatePassword,
  }
}
