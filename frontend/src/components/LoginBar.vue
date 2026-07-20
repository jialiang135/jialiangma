<template>
  <div class="login-bar">
    <template v-if="auth.isLoggedIn">
      <span class="user-badge">{{ auth.isAdmin ? '🔴' : '🟢' }} {{ auth.username }}</span>
      <span class="role-tag" :class="auth.isAdmin ? 'role-admin' : 'role-user'">{{ auth.isAdmin ? '管理员' : '用户' }}</span>
      <button class="btn btn-sm btn-outline" @click="auth.logout()">退出</button>
    </template>
    <template v-else>
      <div class="login-row">
        <input v-model="loginUsername" class="input-xs" placeholder="用户名" @keydown.enter="doLogin" />
        <input v-model="loginPassword" class="input-xs" type="password" placeholder="密码" @keydown.enter="doLogin" />
        <button class="btn btn-xs btn-primary" :disabled="auth.loading" @click="doLogin">
          {{ auth.loading ? '...' : '登录' }}
        </button>
        <button class="btn btn-xs btn-register" :disabled="auth.loading" @click="doRegister">
          注册
        </button>
      </div>
      <div v-if="auth.error" class="error-msg">{{ auth.error }}</div>
    </template>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useAuthStore } from '../stores/auth.js'
import { register as apiRegister } from '../api/auth.js'

const auth = useAuthStore()
const loginUsername = ref('')
const loginPassword = ref('')

async function doLogin() {
  if (!loginUsername.value || !loginPassword.value) return
  await auth.login(loginUsername.value, loginPassword.value)
}

async function doRegister() {
  if (!loginUsername.value || !loginPassword.value) return
  if (loginPassword.value.length < 6) {
    auth.error = '密码至少6位'
    return
  }
  auth.loading = true
  auth.error = ''
  try {
    const res = await apiRegister(loginUsername.value, loginPassword.value)
    if (res.error || !res.access_token) {
      auth.error = res.error || res.detail || '注册失败'
    } else {
      // 注册成功，直接设置登录态
      auth.token = res.access_token
      auth.user = { username: res.username, owner_id: res.owner_id, role: res.role || 'user' }
      localStorage.setItem('token', res.access_token)
      localStorage.setItem('user', JSON.stringify(auth.user))
    }
  } catch (e) {
    auth.error = e.message
  } finally {
    auth.loading = false
  }
}
</script>
