<template>
  <div class="app">
    <header class="app-header">
      <!-- 汉堡按钮（手机端） -->
      <button class="hamburger-btn" @click="showMobileNav = true" aria-label="打开菜单">
        ☰
      </button>

      <div class="header-left">
        <span class="logo">🤖</span>
        <h1>个人数字分身</h1>
        <span class="subtitle">AI 面试助手</span>
      </div>

      <!-- 桌面端导航 -->
      <nav class="header-nav">
        <router-link to="/chat" class="nav-link" active-class="active" @click="showMobileNav = false">
          💬 对话
        </router-link>
        <router-link to="/knowledge" class="nav-link" active-class="active" @click="showMobileNav = false">
          📚 知识库
        </router-link>
      </nav>

      <div class="header-right">
        <LoginBar />
      </div>
    </header>

    <!-- 移动端侧滑导航 -->
    <Teleport to="body">
      <div v-if="showMobileNav" class="mobile-nav-overlay">
        <div class="mobile-nav-backdrop" @click="showMobileNav = false"></div>
        <nav class="mobile-nav-panel" @click.self="showMobileNav = false">
          <router-link
            to="/chat"
            class="nav-link"
            active-class="active"
            @click="showMobileNav = false"
          >
            💬 对话
          </router-link>
          <router-link
            to="/knowledge"
            class="nav-link"
            active-class="active"
            @click="showMobileNav = false"
          >
            📚 知识库
          </router-link>
          <div class="mobile-nav-divider"></div>
          <LoginBar />
        </nav>
      </div>
    </Teleport>

    <main class="app-main">
      <router-view />
    </main>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import LoginBar from './components/LoginBar.vue'

const showMobileNav = ref(false)
</script>
