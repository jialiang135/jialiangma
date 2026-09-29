<script setup>
/**
 * 登录栏（顶栏右侧）
 * ==================
 *
 * 改造前的问题：
 * - 用 emoji 当角色指示（🔴 管理员 / 🟢 用户）—— 同一个 🔴 在导航里表示"管理"、
 *   在面板标题里表示"管理员"，语义已经漂移
 * - 角色标签的样式在**三处**各定义一遍且配色不一致
 *   （`style.css` 用 `#dcfce7`，AdminView 和 GuideModal 用 `#f0fdf4`，后者还多个边框）
 * - 未登录时是四个裸露的 `input-xs` + 两个小按钮挤成一行
 *
 * 现在：输入用 `UiInput`、按钮用 `UiButton`、角色用 `UiBadge`，
 * 角色配色只有一个来源。
 */
import { ref } from 'vue'

import { register as apiRegister } from '../api/auth.js'
import { useAuthStore } from '../stores/auth.js'
import UiBadge from './ui/UiBadge.vue'
import UiButton from './ui/UiButton.vue'
import UiIcon from './ui/UiIcon.vue'
import UiInput from './ui/UiInput.vue'

const auth = useAuthStore()
const username = ref('')
const password = ref('')

async function doLogin() {
  if (!username.value || !password.value) return
  await auth.login(username.value, password.value)
}

async function doRegister() {
  if (!username.value || !password.value) return
  // 后端要求至少 8 位且含数字和字母，这里先给个即时反馈，
  // 免得用户提交后才在服务端被拒（原实现只校验了 6 位，和服务端不一致）
  if (password.value.length < 8) {
    auth.error = '密码至少 8 位'
    return
  }
  auth.loading = true
  auth.error = ''
  try {
    const res = await apiRegister(username.value, password.value)
    if (res.error || !res.access_token) {
      auth.error = res.error || res.detail || '注册失败'
      return
    }
    auth.token = res.access_token
    auth.user = {
      username: res.username,
      owner_id: res.owner_id,
      role: res.role || 'user',
    }
    localStorage.setItem('token', res.access_token)
    localStorage.setItem('user', JSON.stringify(auth.user))
  } catch (e) {
    auth.error = e.message
  } finally {
    auth.loading = false
  }
}
</script>

<template>
  <div class="login-bar">
    <template v-if="auth.isLoggedIn">
      <span class="user">
        <UiIcon name="user" :size="15" class="user-icon" />
        <span class="user-name">{{ auth.username }}</span>
      </span>
      <UiBadge :tone="auth.isAdmin ? 'accent' : 'neutral'">
        {{ auth.isAdmin ? '管理员' : '用户' }}
      </UiBadge>
      <UiButton size="sm" variant="ghost" @click="auth.logout()">退出</UiButton>
    </template>

    <template v-else>
      <form class="login-form" @submit.prevent="doLogin">
        <UiInput v-model="username" size="sm" placeholder="用户名" class="field" />
        <UiInput
          v-model="password"
          size="sm"
          type="password"
          placeholder="密码"
          class="field"
          @enter="doLogin"
        />
        <UiButton size="sm" variant="primary" :loading="auth.loading" @click="doLogin">
          登录
        </UiButton>
        <UiButton size="sm" variant="secondary" :disabled="auth.loading" @click="doRegister">
          注册
        </UiButton>
      </form>
    </template>

    <p v-if="auth.error" class="error">{{ auth.error }}</p>
  </div>
</template>

<style scoped>
.login-bar {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}

.user {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-2);
  font-size: var(--fs-sm);
  color: var(--c-text-2);
}
.user-icon {
  color: var(--c-text-3);
}
.user-name {
  font-weight: var(--fw-medium);
  color: var(--c-text);
}

.login-form {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}
.field {
  width: 108px;
}

/* 错误提示压在顶栏下方，绝对定位避免把顶栏撑高 */
.error {
  position: absolute;
  top: var(--header-h);
  right: var(--sp-5);
  padding: var(--sp-1) var(--sp-3);
  font-size: var(--fs-xs);
  color: var(--c-danger-text);
  background: var(--c-danger-soft);
  border: 1px solid var(--c-danger-border);
  border-radius: var(--r-sm);
  box-shadow: var(--sh-1);
}

@media (max-width: 480px) {
  .field {
    width: 84px;
  }
  .user-name {
    display: none;
  }
}
</style>
