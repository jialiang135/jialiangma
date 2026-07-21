import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { createRouter, createWebHistory } from 'vue-router'
import App from './App.vue'
import ChatView from './views/ChatView.vue'
import KnowledgeView from './views/KnowledgeView.vue'
import './style.css'

const routes = [
  { path: '/', redirect: '/chat' },
  { path: '/chat', component: ChatView, name: 'chat' },
  { path: '/knowledge', component: KnowledgeView, name: 'knowledge' },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

const app = createApp(App)
app.use(createPinia())
app.use(router)
app.mount('#app')
