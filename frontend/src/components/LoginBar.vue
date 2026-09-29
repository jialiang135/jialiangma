<script setup>
/**
 * 顶栏的用户信息
 * ==============
 *
 * 改造前这里挤着一排登录输入框（两个 `input-xs` + 注册/登录按钮），
 * 还要自己处理注册流程、手写 localStorage。登录页独立出去之后，
 * 顶栏只在**已登录**时出现（路由守卫保证），所以这里只剩展示与退出。
 *
 * 注册/登录逻辑都收进 `stores/auth.js` 了，组件不再碰凭据的存取。
 */
import { useRouter } from 'vue-router'

import { useAuthStore } from '../stores/auth.js'
import UiBadge from './ui/UiBadge.vue'
import UiButton from './ui/UiButton.vue'
import UiIcon from './ui/UiIcon.vue'

const auth = useAuthStore()
const router = useRouter()

function logout() {
  auth.logout()
  // 清完凭据主动回登录页 —— 不必等路由守卫（那要等下一次导航才触发）
  router.replace({ name: 'login' })
}
</script>

<template>
  <div class="login-bar">
    <span class="user">
      <UiIcon name="user" :size="15" class="user-icon" />
      <span class="user-name">{{ auth.username }}</span>
    </span>
    <UiBadge :tone="auth.isAdmin ? 'accent' : 'neutral'">
      {{ auth.isAdmin ? '管理员' : '用户' }}
    </UiBadge>
    <UiButton size="sm" variant="ghost" @click="logout">退出</UiButton>
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

@media (max-width: 480px) {
  .user-name {
    display: none;
  }
}
</style>
