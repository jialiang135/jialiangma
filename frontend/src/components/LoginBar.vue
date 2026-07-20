<template>
  <div class="login-bar">
    <template v-if="auth.isLoggedIn">
      <span class="user-badge">🟢 {{ auth.username }}</span>
      <button class="btn btn-sm btn-outline" @click="auth.logout()">退出</button>
    </template>
    <template v-else>
      <input v-model="username" class="input-sm" placeholder="用户名" @keydown.enter="doLogin" />
      <input v-model="password" class="input-sm" type="password" placeholder="密码" @keydown.enter="doLogin" />
      <button class="btn btn-sm btn-primary" :disabled="auth.loading" @click="doLogin">
        {{ auth.loading ? '登录中...' : '登录' }}
      </button>
      <span v-if="auth.error" class="error-msg">{{ auth.error }}</span>
    </template>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useAuthStore } from '../stores/auth.js'

const auth = useAuthStore()
const username = ref('admin')
const password = ref('admin123456')

async function doLogin() {
  await auth.login(username.value, password.value)
}
</script>
