<template>
  <div class="app">
    <header class="app-header">
      <button class="hamburger-btn" @click="showMobileNav = true" aria-label="打开菜单">
        ☰
      </button>

      <div class="header-left">
        <span class="logo">🤖</span>
        <h1>个人数字分身</h1>
        <span class="subtitle">AI 面试助手</span>
      </div>

      <nav class="header-nav">
        <a class="nav-link" @click="showGuide = true" href="javascript:void(0)">📖 使用指南</a>
        <router-link to="/chat" class="nav-link" active-class="active">💬 对话</router-link>
        <router-link to="/knowledge" class="nav-link" active-class="active">📚 知识库</router-link>
      </nav>

      <div class="header-right">
        <LoginBar />
      </div>
    </header>

    <!-- 移动端侧滑导航 -->
    <Teleport to="body">
      <div v-if="showMobileNav" class="mobile-nav-overlay">
        <div class="mobile-nav-backdrop" @click="showMobileNav = false"></div>
        <nav class="mobile-nav-panel">
          <a class="nav-link" @click="showGuide = true; showMobileNav = false" href="javascript:void(0)">📖 使用指南</a>
          <router-link to="/chat" class="nav-link" active-class="active" @click="showMobileNav = false">💬 对话</router-link>
          <router-link to="/knowledge" class="nav-link" active-class="active" @click="showMobileNav = false">📚 知识库</router-link>
          <div class="mobile-nav-divider"></div>
          <LoginBar />
        </nav>
      </div>
    </Teleport>

    <!-- 使用指南弹窗 -->
    <GuideModal :show="showGuide" @close="closeGuide" />

    <main class="app-main">
      <router-view />
    </main>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import LoginBar from './components/LoginBar.vue'
import GuideModal from './components/GuideModal.vue'

const showMobileNav = ref(false)
// 首次访问自动弹出使用指南
const showGuide = ref(!localStorage.getItem('guide_seen'))
function closeGuide() {
  showGuide.value = false
  localStorage.setItem('guide_seen', '1')
}
</script>
