<template>
  <Teleport to="body">
    <div v-if="show" class="guide-overlay" @click.self="$emit('close')">
      <div class="guide-dialog">
        <button class="guide-close" @click="$emit('close')" aria-label="关闭">✕</button>
        <div class="guide-body">
          <h1 class="guide-title">📖 使用指南</h1>
          <p class="guide-subtitle">个人数字分身 · AI 面试助手</p>

          <!-- 身份说明 -->
          <section class="g-section">
            <h2>👤 用户身份</h2>
            <div class="g-roles">
              <div class="g-role role-admin"><b>🔴 管理员</b>：可上传/删除/清空知识库文档，管理所有数据</div>
              <div class="g-role role-user"><b>🟢 普通用户</b>：可提问对话，无法管理知识库</div>
            </div>
            <p class="g-note">💡 知识库内容由管理员统一维护，普通用户直接使用即可。</p>
          </section>

          <!-- 快速开始 -->
          <section class="g-section">
            <h2>⚡ 两步上手</h2>
            <div class="g-steps">
              <div class="g-step"><span class="g-num">1</span> 右上角输入用户名和密码，点击<b>注册</b>（密码至少 8 位，含数字和字母），然后<b>登录</b></div>
              <div class="g-step"><span class="g-num">2</span> 在「💬 对话」页面输入问题，按回车发送，等待 AI 回答</div>
            </div>
          </section>

          <!-- 对话技巧 -->
          <section class="g-section">
            <h2>💬 对话技巧</h2>
            <div class="g-tips">
              <div class="g-tip"><b>✅ 问具体：</b>「我做过哪些 Java 项目？」比「介绍你自己」更精准</div>
              <div class="g-tip"><b>✅ 可追问：</b>回答后继续追问，系统保留上下文</div>
              <div class="g-tip"><b>✅ 快捷问题：</b>输入框上方有预设问题按钮，一键提问</div>
              <div class="g-tip"><b>🧠 查看思考过程：</b>回答下方有「查看完整推理过程」，可展开查看 AI 的检索和推理步骤</div>
              <div class="g-tip"><b>⚠️ 仅基于知识库：</b>系统只回答管理员已上传文档中的内容，不会编造信息</div>
            </div>
          </section>

          <!-- 手机端 -->
          <section class="g-section">
            <h2>📱 手机端使用</h2>
            <p>浏览器打开即可，界面自动适配手机屏幕。左上角 <b>☰</b> 打开导航菜单。</p>
          </section>

          <div class="g-footer">
            🌐 服务器地址：<code>http://39.106.191.98</code>
          </div>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup>
defineProps({ show: Boolean })
defineEmits(['close'])
</script>

<style scoped>
.guide-overlay {
  position: fixed; inset: 0; z-index: 10000;
  background: rgba(0,0,0,0.45);
  display: flex; align-items: center; justify-content: center;
  animation: fadeIn 0.2s ease;
}
.guide-dialog {
  background: #fff; border-radius: 16px;
  width: 90vw; max-width: 680px; max-height: 85vh;
  position: relative; overflow: hidden;
  box-shadow: 0 20px 60px rgba(0,0,0,0.2);
  animation: scaleIn 0.2s ease;
  display: flex; flex-direction: column;
}
.guide-close {
  position: absolute; top: 12px; right: 12px; z-index: 1;
  width: 36px; height: 36px; border: none; background: #f1f5f9;
  border-radius: 50%; font-size: 18px; cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  color: #64748b; transition: all 0.15s;
}
.guide-close:hover { background: #fee2e2; color: #dc2626; }
.guide-body {
  padding: 32px 28px 24px;
  overflow-y: auto; flex: 1;
  -webkit-overflow-scrolling: touch;
}
.guide-title { font-size: 26px; margin-bottom: 4px; }
.guide-subtitle { color: #64748b; font-size: 14px; margin-bottom: 24px; }

.g-section { margin-bottom: 20px; }
.g-section h2 { font-size: 17px; margin-bottom: 10px; padding-bottom: 6px; border-bottom: 1px solid #e2e8f0; }
.g-section p { font-size: 13px; color: #475569; line-height: 1.7; }

.g-roles { display: flex; flex-direction: column; gap: 8px; }
.g-role { font-size: 13px; padding: 8px 12px; border-radius: 8px; line-height: 1.6; }
.role-admin { background: #fef2f2; border: 1px solid #fecaca; }
.role-user { background: #f0fdf4; border: 1px solid #bbf7d0; }
.g-note { margin-top: 10px; font-size: 12px; color: #64748b; }

.g-steps { display: flex; flex-direction: column; gap: 10px; }
.g-step { font-size: 14px; display: flex; align-items: center; gap: 10px; }
.g-num {
  width: 28px; height: 28px; border-radius: 50%;
  background: #4f46e5; color: #fff; font-weight: 700; font-size: 14px;
  display: inline-flex; align-items: center; justify-content: center; flex-shrink: 0;
}

.g-tips { display: flex; flex-direction: column; gap: 8px; }
.g-tip { font-size: 13px; color: #475569; line-height: 1.6; padding: 8px 12px; background: #f8fafc; border-radius: 8px; }

.g-footer {
  margin-top: 16px; padding: 12px 16px;
  background: #f0f9ff; border: 1px solid #bae6fd;
  border-radius: 10px; text-align: center; font-size: 13px;
}
.g-footer code { background: #e0f2fe; padding: 2px 8px; border-radius: 4px; font-weight: 600; color: #0369a1; }

@keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
@keyframes scaleIn { from { opacity: 0; transform: scale(0.92); } to { opacity: 1; transform: scale(1); } }

@media (max-width: 480px) {
  .guide-dialog { width: 95vw; max-height: 90vh; border-radius: 16px 16px 0 0; align-self: flex-end; }
  .guide-body { padding: 24px 16px 16px; }
  .guide-title { font-size: 22px; }
}
</style>
