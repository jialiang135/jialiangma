import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { createRouter, createWebHistory } from 'vue-router'

import App from './App.vue'
import AdminView from './views/AdminView.vue'
import ChatView from './views/ChatView.vue'
import EvalView from './views/EvalView.vue'
import KnowledgeView from './views/KnowledgeView.vue'

// 样式分两层加载，顺序不能反：
//   1. tokens   —— 设计变量（其它样式全都引用它）
//   2. base     —— 归零 + 排版基线 + Markdown 正文
// 组件样式都在各自 SFC 的 <style scoped> 里，不在这里集中。
// （改造前是一个 1530 行的 style.css，其中 340 行是"后置覆盖层"，
//   同一批组件被写了两遍，靠书写顺序斗优先级。）
import './styles/tokens.css'
import './styles/base.css'

const routes = [
  { path: '/', redirect: '/chat' },
  { path: '/chat', component: ChatView, name: 'chat' },
  { path: '/knowledge', component: KnowledgeView, name: 'knowledge' },
  { path: '/eval', component: EvalView, name: 'eval' },
  { path: '/admin', component: AdminView, name: 'admin' },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

const app = createApp(App)
app.use(createPinia())
app.use(router)
app.mount('#app')
