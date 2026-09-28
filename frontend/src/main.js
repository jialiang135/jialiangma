import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { createRouter, createWebHistory } from 'vue-router'
import App from './App.vue'
import ChatView from './views/ChatView.vue'
import KnowledgeView from './views/KnowledgeView.vue'
import AdminView from './views/AdminView.vue'
import EvalView from './views/EvalView.vue'
import './style.css'

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
