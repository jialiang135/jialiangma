import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { createRouter, createWebHistory } from 'vue-router'

import App from './App.vue'
import AdminView from './views/AdminView.vue'
import ChatView from './views/ChatView.vue'
import EvalView from './views/EvalView.vue'
import KnowledgeView from './views/KnowledgeView.vue'
import LoginView from './views/LoginView.vue'
import StatsView from './views/StatsView.vue'
import { useAuthStore } from './stores/auth.js'

// 样式分两层加载，顺序不能反：
//   1. tokens   —— 设计变量（其它样式全都引用它）
//   2. base     —— 归零 + 排版基线 + Markdown 正文
// 组件样式都在各自 SFC 的 <style scoped> 里，不在这里集中。
import './styles/tokens.css'
import './styles/base.css'

const routes = [
  { path: '/', redirect: '/chat' },
  { path: '/login', component: LoginView, name: 'login', meta: { public: true } },
  { path: '/chat', component: ChatView, name: 'chat' },
  { path: '/knowledge', component: KnowledgeView, name: 'knowledge' },
  { path: '/eval', component: EvalView, name: 'eval' },
  // 用量统计原先是对话页底部的一条折叠横条 —— 那是"偶尔看一眼"的信息，
  // 却常年占着一行高度，而且和"对话"本来就是两件事。现在独立成页。
  { path: '/stats', component: StatsView, name: 'stats' },
  { path: '/admin', component: AdminView, name: 'admin' },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

/**
 * 路由守卫：**没登录一律送到登录页**。
 *
 * 这个系统几乎所有接口都要 token（对话、知识库、朗读的 WebSocket），
 * 未登录留在页面里也只是看到一堆禁用的输入框。所以把登录页作为入口门禁，
 * 并把原本要去的地址记在 `redirect` 上，登录后直接回到那里。
 *
 * 注意：`useAuthStore()` 必须在这里**惰性调用**（守卫执行时 pinia 已装好），
 * 不能提到模块顶层，否则会在 pinia 安装前就求值。
 */
router.beforeEach((to) => {
  const auth = useAuthStore()

  if (to.name === 'login') {
    // 已登录就别停在登录页
    return auth.isLoggedIn ? { name: 'chat' } : true
  }

  if (!auth.isLoggedIn) {
    const redirect = to.fullPath && to.fullPath !== '/' ? { redirect: to.fullPath } : {}
    return { name: 'login', query: redirect }
  }

  return true
})

const app = createApp(App)
app.use(createPinia())
app.use(router)
app.mount('#app')
