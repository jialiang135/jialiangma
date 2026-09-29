<script setup>
/**
 * 应用外壳
 * ========
 *
 * 顶栏 + 导航 + 内容区 + Toast 宿主。
 *
 * 改动要点：
 * - 导航图标从 emoji 换成统一的 UiIcon（原来 🔴 同时表示"管理导航项"
 *   "管理员角色""管理员面板"三种意思，语义已经漂移了）
 * - 内容区高度不再靠各页面自己算 `calc(100dvh - 60px)` 这类魔法数字，
 *   统一用 `--header-h` 变量，并且**对话页独占高度、其余页面自然滚动**
 * - Toast 宿主挂在这里（一次），各页面调 `useToast()` 即可，不用自己渲染提示条
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import GuideModal from './components/GuideModal.vue'
import LoginBar from './components/LoginBar.vue'
import UiIcon from './components/ui/UiIcon.vue'
import UiToastHost from './components/ui/UiToastHost.vue'
import { useAuthStore } from './stores/auth.js'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const showMobileNav = ref(false)

/**
 * 使用指南改为**登录之后**才自动弹。
 *
 * 原先是"首次访问就弹"，而登录页现在是入口 —— 那样访客一进来
 * 先被一个"如何使用"的弹窗盖住，而他连账号都还没有。
 * 现在进到应用内（已登录）且没看过时再弹。
 */
const showGuide = ref(false)

const navItems = computed(() => [
  { to: '/chat', icon: 'chat', label: '对话' },
  { to: '/knowledge', icon: 'book', label: '知识库' },
  { to: '/eval', icon: 'chart', label: '评测' },
  ...(auth.isAdmin ? [{ to: '/admin', icon: 'shield', label: '管理' }] : []),
])

// 对话页自己管滚动（消息区独立滚动、输入框固定在底部），
// 其余页面由外壳提供滚动容器
const isFullHeight = computed(() => route.name === 'chat')
/** 登录页是独立整页，不套应用外壳 */
const isAuthPage = computed(() => route.name === 'login')

/**
 * 登录态失效时立刻回登录页。
 *
 * 路由守卫只在**导航时**执行，而 401 是在页面停留期间发生的 ——
 * 没有这个 watch 的话，token 过期后用户会停在一个所有请求都失败、
 * 但看起来仍然"已登录"的页面上。
 *
 * 两个细节：
 * - **不加 `immediate`**。初始加载时登录态本来就是 false，此刻 router 还没
 *   解析出路由，贸然 replace 会把路由守卫刚写上的 `?redirect=...` 覆盖掉
 *   （踩过：登录后回不到原本要去的页面）。初始跳转交给守卫。
 * - 只在**从已登录变为未登录**时跳，避免无谓的重复导航。
 */
watch(
  () => auth.isLoggedIn,
  (loggedIn, wasLoggedIn) => {
    if (wasLoggedIn && !loggedIn) {
      router.replace({ name: 'login' })
    }
  },
)

// 使用指南：进到应用内（已登录）且没看过时弹一次。
// 放在 onMounted 而不是 watch 里，理由同上 —— 不想让初始态触发路由动作。
onMounted(() => {
  if (auth.isLoggedIn && !localStorage.getItem('guide_seen')) {
    showGuide.value = true
  }
})

function closeGuide() {
  showGuide.value = false
  localStorage.setItem('guide_seen', '1')
}
</script>

<template>
  <div class="app">
    <header v-if="!isAuthPage" class="app-header">
      <div class="header-inner">
        <button
          class="hamburger"
          type="button"
          title="菜单"
          @click="showMobileNav = true"
        >
          <UiIcon name="menu" :size="18" />
        </button>

        <RouterLink to="/chat" class="brand">
          <span class="brand-mark"><UiIcon name="layers" :size="16" /></span>
          <span class="brand-text">
            <span class="brand-name">个人数字分身</span>
            <span class="brand-sub">AI 面试助手</span>
          </span>
        </RouterLink>

        <nav class="nav">
          <RouterLink
            v-for="item in navItems"
            :key="item.to"
            :to="item.to"
            class="nav-link"
          >
            <UiIcon :name="item.icon" :size="15" />
            <span>{{ item.label }}</span>
          </RouterLink>
          <button class="nav-link" type="button" @click="showGuide = true">
            <UiIcon name="help" :size="15" />
            <span>使用指南</span>
          </button>
        </nav>

        <div class="header-right">
          <LoginBar />
        </div>
      </div>
    </header>

    <main class="app-main" :class="{ 'is-full': isFullHeight }">
      <RouterView />
    </main>

    <!-- 移动端导航抽屉 -->
    <Teleport to="body">
      <Transition name="drawer">
        <div
          v-if="!isAuthPage && showMobileNav"
          class="drawer-overlay"
          @click.self="showMobileNav = false"
        >
          <nav class="drawer">
            <button
              v-for="item in navItems"
              :key="item.to"
              class="drawer-link"
              type="button"
              @click="showMobileNav = false; $router.push(item.to)"
            >
              <UiIcon :name="item.icon" :size="17" />
              <span>{{ item.label }}</span>
            </button>
            <button class="drawer-link" type="button" @click="showMobileNav = false; showGuide = true">
              <UiIcon name="help" :size="17" />
              <span>使用指南</span>
            </button>
          </nav>
        </div>
      </Transition>
    </Teleport>

    <GuideModal v-if="!isAuthPage" :show="showGuide" @close="closeGuide" />
    <UiToastHost />
  </div>
</template>

<style scoped>
.app {
  display: flex;
  flex-direction: column;
  height: 100%;
}

.app-header {
  position: sticky;
  top: 0;
  z-index: var(--z-sticky);
  flex-shrink: 0;
  height: var(--header-h);
  background: var(--c-surface);
  border-bottom: 1px solid var(--c-border);
}

.header-inner {
  display: flex;
  align-items: center;
  gap: var(--sp-4);
  height: 100%;
  padding: 0 var(--sp-5);
}

.hamburger {
  display: none;
  padding: var(--sp-1);
  color: var(--c-text-2);
  border-radius: var(--r-sm);
}
.hamburger:hover {
  background: var(--c-surface-2);
}

.brand {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  color: var(--c-text);
  text-decoration: none;
}
.brand:hover {
  text-decoration: none;
}
.brand-mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  color: var(--c-white);
  background: var(--c-accent);
  border-radius: var(--r-md);
}
.brand-text {
  display: flex;
  flex-direction: column;
  line-height: 1.15;
}
.brand-name {
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
}
.brand-sub {
  font-size: var(--fs-xs);
  color: var(--c-text-3);
}

.nav {
  display: flex;
  align-items: center;
  gap: var(--sp-1);
  margin-left: var(--sp-2);
}

.nav-link {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--c-text-2);
  text-decoration: none;
  border-radius: var(--r-md);
  transition: color var(--dur-fast) var(--ease), background var(--dur-fast) var(--ease);
}
.nav-link:hover {
  color: var(--c-text);
  background: var(--c-surface-2);
  text-decoration: none;
}
/* 选中态用浅底 + 主色字，不用整块实心 —— 实心导航在浅色界面里太重 */
.nav-link.router-link-active {
  color: var(--c-accent-text);
  background: var(--c-accent-soft);
}

.header-right {
  margin-left: auto;
}

.app-main {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}
/* 对话页自己管滚动：消息区滚、输入框固定，所以外壳不能再滚 */
.app-main.is-full {
  overflow: hidden;
}

/* ── 移动端抽屉 ── */
.drawer-overlay {
  position: fixed;
  inset: 0;
  z-index: var(--z-modal);
  background: rgba(24, 24, 27, 0.4);
}
.drawer {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  width: 240px;
  height: 100%;
  padding: var(--sp-4) var(--sp-3);
  background: var(--c-surface);
}
.drawer-link {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  padding: var(--sp-3);
  font-size: var(--fs-base);
  color: var(--c-text);
  text-align: left;
  border-radius: var(--r-md);
}
.drawer-link:hover {
  background: var(--c-surface-2);
}

.drawer-enter-active,
.drawer-leave-active {
  transition: opacity var(--dur-base) var(--ease);
}
.drawer-enter-active .drawer,
.drawer-leave-active .drawer {
  transition: transform var(--dur-base) var(--ease);
}
.drawer-enter-from,
.drawer-leave-to {
  opacity: 0;
}
.drawer-enter-from .drawer,
.drawer-leave-to .drawer {
  transform: translateX(-100%);
}

@media (max-width: 768px) {
  .hamburger {
    display: inline-flex;
  }
  .nav {
    display: none;
  }
  .header-inner {
    padding: 0 var(--sp-3);
  }
  .brand-sub {
    display: none;
  }
}
</style>
