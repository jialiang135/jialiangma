import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { login as apiLogin } from '../api/auth.js'

export const useAuthStore = defineStore('auth', () => {
  const token = ref(localStorage.getItem('token') || '')
  const user = ref(JSON.parse(localStorage.getItem('user') || 'null'))
  const loading = ref(false)
  const error = ref('')

  const isLoggedIn = computed(() => !!token.value)
  const username = computed(() => user.value?.username || '')
  const ownerId = computed(() => user.value?.owner_id || user.value?.id || 0)

  async function login(username, password) {
    loading.value = true
    error.value = ''
    try {
      const res = await apiLogin(username, password)
      if (res.error || !res.access_token) {
        error.value = res.error || res.detail || '登录失败'
        return false
      }
      token.value = res.access_token
      const userData = { username: res.username, owner_id: res.owner_id }
      user.value = userData
      localStorage.setItem('token', res.access_token)
      localStorage.setItem('user', JSON.stringify(userData))
      return true
    } catch (e) {
      error.value = e.message
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

  return { token, user, loading, error, isLoggedIn, username, ownerId, login, logout }
})
