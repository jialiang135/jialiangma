<template>
  <div class="app">
    <header class="app-header">
      <div class="header-left">
        <span class="logo">🤖</span>
        <h1>个人数字分身</h1>
        <span class="subtitle">AI 面试助手</span>
      </div>
      <nav class="header-nav">
        <router-link to="/chat" class="nav-link" active-class="active">💬 对话</router-link>
        <router-link to="/knowledge" class="nav-link" active-class="active">📚 知识库</router-link>
        <router-link to="/eval" class="nav-link" active-class="active">📊 评测</router-link>
      </nav>
      <div class="header-right">
        <LoginBar />
      </div>
    </header>
    <main class="app-main">
      <router-view />
    </main>
  </div>
</template>

<script setup>
import LoginBar from './components/LoginBar.vue'
</script>
