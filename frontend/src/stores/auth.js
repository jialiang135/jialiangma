import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { login as apiLogin, register as apiRegister } from '../api/auth.js'

export const useAuthStore = defineStore('auth', () => {
  const token = ref(localStorage.getItem('token') || '')
  const user = ref(JSON.parse(localStorage.getItem('user') || 'null'))
  const loading = ref(false)
  const error = ref('')

  const isLoggedIn = computed(() => !!token.value)
  const username = computed(() => user.value?.username || '')
  const ownerId = computed(() => user.value?.owner_id || user.value?.id || 0)
  const role = computed(() => user.value?.role || 'user')
  const isAdmin = computed(() => role.value === 'admin')

  /** 把签发的凭据落到 store 与 localStorage（登录、注册共用） */
  function applySession(res) {
    token.value = res.access_token
    const userData = {
      username: res.username,
      owner_id: res.owner_id,
      role: res.role || 'user',
    }
    user.value = userData
    localStorage.setItem('token', res.access_token)
    localStorage.setItem('user', JSON.stringify(userData))
  }

  async function login(username, password) {
    loading.value = true
    error.value = ''
    try {
      const res = await apiLogin(username, password)
      if (res.error || !res.access_token) {
        error.value = res.error || res.detail || '登录失败'
        return false
      }
      applySession(res)
      return true
    } catch (e) {
      // api 层现在对非 2xx 一律抛错，并把后端的 detail 带在 message 上，
      // 所以这里能直接展示"用户名或密码错误"这类具体原因
      error.value = e.message || '登录失败'
      return false
    } finally {
      loading.value = false
    }
  }

  /**
   * 注册并直接进入登录态（后端注册成功后会签发 token）。
   *
   * 这段逻辑原先写在 LoginBar 组件里，混着表单状态和手动写 localStorage ——
   * 现在收到 store 里，和 login 走同一套 applySession，行为不会分叉。
   */
  async function register(username, password) {
    loading.value = true
    error.value = ''
    try {
      const res = await apiRegister(username, password)
      if (res.error || !res.access_token) {
        error.value = res.error || res.detail || '注册失败'
        return false
      }
      applySession(res)
      return true
    } catch (e) {
      error.value = e.message || '注册失败'
      return false
    } finally {
      loading.value = false
    }
  }

  function logout() {
    token.value = ''
    user.value = null
    error.value = ''
    localStorage.removeItem('token')
    localStorage.removeItem('user')
  }

  // listen for auth-expired events
  if (typeof window !== 'undefined') {
    window.addEventListener('auth-expired', () => {
      logout()
    })
  }

  return {
    token,
    user,
    loading,
    error,
    isLoggedIn,
    username,
    ownerId,
    role,
    isAdmin,
    login,
    register,
    logout,
  }
})
