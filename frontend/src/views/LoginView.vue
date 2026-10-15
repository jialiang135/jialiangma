<script setup>
/**
 * 登录 / 注册页
 * ==============
 *
 * **这是应用的入口门禁**：未登录进任何页面都会被路由守卫送到这里。
 *
 * 改造前登录只是顶栏右侧挤着的一排输入框（`input-xs` + 两个小按钮），
 * 密码要求只写在前端的 `if (length < 8)` 里，而且**只在提交失败后**
 * 才通过报错暴露 —— 用户得先撞一次墙才知道有什么要求。
 *
 * 现在：
 * - 独立整页，登录/注册分两栏（`.ui-tabs` 胶囊切换）
 * - 注册表单带**实时校验清单**，规则来自后端 `GET /api/auth/requirements`
 *   （见 composables/useAuthRequirements.js），边输入边打勾
 * - 注册关掉时（`allow_registration=false`）注册页直接说明"未开放"，
 *   而不是让人填完表单才收到 403
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import UiAlert from '../components/ui/UiAlert.vue'
import UiButton from '../components/ui/UiButton.vue'
import UiIcon from '../components/ui/UiIcon.vue'
import UiInput from '../components/ui/UiInput.vue'
import UiSpinner from '../components/ui/UiSpinner.vue'
import UiTabs from '../components/ui/UiTabs.vue'
import { useAuthRequirements } from '../composables/useAuthRequirements.js'
import { useAuthStore } from '../stores/auth.js'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

const {
  loading: rulesLoading,
  error: rulesError,
  load: loadRules,
  allowRegistration,
  evaluatePassword,
  usernameError,
  checkUsername,
} = useAuthRequirements()

const TABS = [
  { key: 'login', label: '登录', icon: 'user' },
  { key: 'register', label: '注册体验', icon: 'plus' },
]

const mode = ref('login')
const modeTouched = ref(false)

function setMode(next) {
  modeTouched.value = true
  mode.value = next
}

async function initializeAuthPage() {
  await loadRules()
  // 新访客优先看到注册；已有账号可切回登录。若用户已手动选择，则尊重他的选择。
  if (!modeTouched.value && allowRegistration.value) mode.value = 'register'
}
const username = ref('')
const password = ref('')
const showPassword = ref(false)

/** 实时校验结果：登录模式下不显示（那时不必校验强度） */
const checks = computed(() =>
  mode.value === 'register' ? evaluatePassword(password.value) : [],
)
const allChecksPass = computed(() => checks.value.every((c) => c.ok))

const usernameOk = computed(() => checkUsername(username.value))

const canSubmit = computed(() => {
  if (auth.loading) return false
  if (!username.value || !password.value) return false
  if (mode.value === 'register') return usernameOk.value && allChecksPass.value
  return true
})

/** 切模式时清掉上一次的错误，避免"注册失败的提示"留在登录表单上 */
watch(mode, () => {
  auth.error = ''
  showPassword.value = false
})

// 已登录就别停在登录页
watch(
  () => auth.isLoggedIn,
  (yes) => {
    if (yes) goAfterAuth()
  },
)

async function submit() {
  if (!canSubmit.value) return
  const ok =
    mode.value === 'login'
      ? await auth.login(username.value, password.value)
      : await auth.register(username.value, password.value)
  if (ok) goAfterAuth()
}

/** 登录成功后的去向：优先回到被拦下来的那一页 */
function goAfterAuth() {
  const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : ''
  // 只接受站内相对路径，挡掉 //evil.com 这类协议相对地址
  const safe = redirect.startsWith('/') && !redirect.startsWith('//') ? redirect : '/chat'
  router.replace(safe)
}

onMounted(initializeAuthPage)
</script>

<template>
  <div class="auth-page">
    <!-- 左栏：品牌、产品价值和界面示意。窄屏时收敛为紧凑品牌区。 -->
    <section class="auth-brand" aria-label="产品介绍">
      <div class="brand-top">
        <span class="brand-mark"><UiIcon name="layers" :size="20" /></span>
        <span class="brand-name">
          <strong>个人数字分身</strong>
          <small>KNOWLEDGE-BASED DEMO</small>
        </span>
        <span class="brand-status"><i /> DEMO SPACE</span>
      </div>

      <div class="brand-copy">
        <span class="brand-kicker"><UiIcon name="shield" :size="13" /> EVIDENCE-FIRST AI</span>
        <h1 class="brand-title">让经验沉淀下来，<br /><span>让回答有据可查。</span></h1>
        <p class="brand-desc">
          简历、项目文档与技术笔记已整理为演示知识库。注册体验账号即可提问；所有体验账号共用这份知识库，回答会展示对应来源。
        </p>
      </div>

      <div class="product-preview" role="group" aria-label="产品界面示意">
        <div class="preview-topbar">
          <span class="preview-dots"><i /><i /><i /></span>
          <span class="preview-window-title">个人数字分身 <span>·</span> 新对话</span>
          <span class="preview-lock"><UiIcon name="layers" :size="12" /> SHARED DEMO</span>
        </div>
        <div class="preview-question">你做过哪些 AI Agent 项目？</div>
        <div class="preview-answer">
          <span class="preview-avatar"><UiIcon name="zap" :size="14" /></span>
          <div class="preview-answer-body">
            <span class="preview-label">基于知识库的回答</span>
            <p>结合项目经历作答，并附上可核查的原文片段。</p>
          </div>
        </div>
        <div class="preview-source">
          <span><UiIcon name="file" :size="13" /> 项目经历 · 相关知识片段</span>
          <span class="source-status"><UiIcon name="check" :size="12" /> 可追溯</span>
        </div>
      </div>

      <ul class="brand-points">
        <li><UiIcon name="layers" :size="15" /><span>回答关联知识来源</span></li>
        <li><UiIcon name="zap" :size="15" /><span>检索与推理过程可查看</span></li>
        <li><UiIcon name="shield" :size="15" /><span>没有依据时如实说明</span></li>
      </ul>
      <p class="brand-footer">演示知识库 <span>·</span> 证据优先 <span>·</span> 来源可追溯</p>
    </section>

    <!-- 右栏：登录 / 注册 -->
    <section class="auth-panel">
      <div class="auth-card">
        <div class="auth-heading">
          <span class="auth-heading-icon"><UiIcon :name="mode === 'login' ? 'user' : 'plus'" :size="17" /></span>
          <div>
            <p class="auth-eyebrow">{{ mode === 'login' ? 'WELCOME BACK' : 'START YOUR DEMO' }}</p>
            <h2>{{ mode === 'login' ? '欢迎回来' : '创建体验账号' }}</h2>
          </div>
        </div>
        <p class="auth-intro">
          {{ mode === 'login'
            ? '已有账号请登录；首次体验可创建账号，注册后立即进入问答。'
            : '注册后自动进入问答。所有体验账号共用同一份演示知识库。' }}
        </p>
        <UiTabs :model-value="mode" :tabs="TABS" variant="pill" @update:model-value="setMode" />

        <form class="auth-form" @submit.prevent="submit">
          <label class="field">
            <span class="field-label">用户名</span>
            <UiInput
              v-model="username"
              :size="'md'"
              placeholder="请输入用户名"
              autocomplete="username"
            />
            <!-- 只在注册模式下提示长度要求，且输入过才提示 -->
            <span v-if="mode === 'register' && username && !usernameOk" class="field-hint is-bad">
              {{ usernameError }}
            </span>
          </label>

          <label class="field">
            <span class="field-label">密码</span>
            <div class="password-wrap">
              <UiInput
                v-model="password"
                :type="showPassword ? 'text' : 'password'"
                placeholder="请输入密码"
                :autocomplete="mode === 'register' ? 'new-password' : 'current-password'"
                @enter="submit"
              />
              <button
                class="password-toggle"
                type="button"
                :aria-label="showPassword ? '隐藏密码' : '显示密码'"
                @click="showPassword = !showPassword"
              >
                <UiIcon :name="showPassword ? 'eye-off' : 'eye'" :size="15" />
              </button>
            </div>
          </label>

          <!-- 实时校验清单：注册模式才出现，规则来自后端 -->
          <div v-if="mode === 'register'" class="rules">
            <p class="rules-title">密码要求</p>
            <ul class="rules-list">
              <li
                v-for="check in checks"
                :key="check.key"
                class="rule"
                :class="{ 'is-ok': check.ok }"
              >
                <UiIcon :name="check.ok ? 'check' : 'dots'" :size="14" />
                <span>{{ check.label }}</span>
              </li>
            </ul>
            <p v-if="rulesError" class="rules-error">
              未能获取校验规则（{{ rulesError }}），提交时以后端判定为准
            </p>
          </div>

          <!-- 注册未开放：直接说明，不要让人填完才被拒 -->
          <UiAlert v-if="mode === 'register' && !rulesLoading && !allowRegistration" tone="warning">
            本站当前未开放注册，请联系管理员开通账号。
          </UiAlert>

          <UiAlert v-if="auth.error" tone="danger">{{ auth.error }}</UiAlert>

          <UiButton
            type="submit"
            variant="primary"
            block
            :loading="auth.loading"
            :disabled="!canSubmit || (mode === 'register' && !allowRegistration)"
          >
            {{ mode === 'login' ? '登录' : '注册并进入' }}
          </UiButton>

          <p class="switch-hint">
            <template v-if="mode === 'login'">
              还没有账号？
              <button type="button" class="link" @click="setMode('register')">创建体验账号</button>
            </template>
            <template v-else>
              已有账号？
              <button type="button" class="link" @click="setMode('login')">去登录</button>
            </template>
          </p>
        </form>

        <div v-if="rulesLoading" class="rules-loading">
          <UiSpinner :size="14" />
          <span>正在读取校验规则…</span>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.auth-page {
  position: relative;
  isolation: isolate;
  display: grid;
  grid-template-columns: minmax(0, 1.05fr) minmax(430px, 0.95fr);
  min-height: 100%;
  overflow: hidden;
  background: var(--c-bg);
}

/* ── 左栏：品牌与产品示意 ── */
.auth-brand {
  position: relative;
  isolation: isolate;
  display: flex;
  flex-direction: column;
  justify-content: center;
  min-height: 100%;
  padding: clamp(36px, 6vh, 72px) clamp(32px, 6vw, 96px);
  overflow: hidden;
  color: var(--c-white);
  background: linear-gradient(145deg, var(--c-brand-ink) 0%, var(--c-brand-indigo) 56%, var(--c-brand-violet) 100%);
}
.auth-brand::before,
.auth-brand::after {
  position: absolute;
  z-index: -1;
  pointer-events: none;
  content: '';
}
.auth-brand::before {
  top: -190px;
  right: -190px;
  width: 560px;
  height: 560px;
  background: radial-gradient(circle, var(--c-brand-glow), transparent 72%);
  border: 1px solid var(--c-brand-glass-border);
  border-radius: 50%;
}
.auth-brand::after {
  right: 0;
  bottom: 0;
  left: 0;
  height: 48%;
  background-image: radial-gradient(var(--c-brand-grid) 0.7px, transparent 0.7px);
  background-position: 0 0;
  background-size: 22px 22px;
  mask-image: linear-gradient(to top, var(--c-brand-mask), transparent 82%);
}
.auth-brand > * {
  position: relative;
}

.brand-top {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  margin-bottom: clamp(28px, 5vh, 54px);
}
.brand-mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 42px;
  height: 42px;
  color: var(--c-white);
  background: var(--c-accent-gradient);
  border: 1px solid var(--c-brand-glass-border);
  border-radius: 14px;
  box-shadow: var(--sh-brand-mark);
}
.brand-name {
  display: flex;
  flex-direction: column;
  gap: 2px;
  line-height: 1.2;
}
.brand-name strong {
  font-size: var(--fs-md);
  font-weight: var(--fw-semibold);
  letter-spacing: 0.02em;
}
.brand-name small {
  font-size: 9px;
  font-weight: var(--fw-medium);
  letter-spacing: 0.14em;
  color: var(--c-brand-subtle);
}
.brand-status {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  margin-left: auto;
  padding: 6px 10px;
  font-size: 10px;
  font-weight: var(--fw-semibold);
  letter-spacing: 0.12em;
  color: var(--c-brand-status);
  background: var(--c-brand-glass-soft);
  border: 1px solid var(--c-brand-glass-border);
  border-radius: var(--r-full);
}
.brand-status i {
  width: 6px;
  height: 6px;
  background: var(--c-brand-success);
  border-radius: 50%;
  box-shadow: var(--sh-brand-success);
}

.brand-copy {
  max-width: 620px;
}
.brand-kicker {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 6px 10px;
  font-size: 10px;
  font-weight: var(--fw-semibold);
  letter-spacing: 0.14em;
  color: var(--c-brand-highlight);
  background: var(--c-brand-glass-soft);
  border: 1px solid var(--c-brand-glass-border);
  border-radius: var(--r-full);
}
.brand-title {
  margin-top: 20px;
  font-size: clamp(34px, 4vw, 52px);
  font-weight: var(--fw-semibold);
  line-height: 1.24;
  letter-spacing: -0.045em;
  color: var(--c-white);
}
.brand-title span {
  color: var(--c-brand-highlight);
}
.brand-desc {
  max-width: 45ch;
  margin-top: 18px;
  font-size: var(--fs-md);
  line-height: 1.9;
  color: var(--c-brand-muted);
}
.brand-desc strong {
  font-weight: var(--fw-semibold);
  color: var(--c-white);
}

.product-preview {
  width: min(100%, 590px);
  margin-top: clamp(24px, 4vh, 38px);
  padding: 16px 18px 14px;
  background: var(--c-brand-glass);
  border: 1px solid var(--c-brand-glass-border);
  border-radius: 18px;
  box-shadow: var(--sh-brand-preview);
  backdrop-filter: blur(16px);
}
.preview-topbar,
.preview-source {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-3);
}
.preview-topbar {
  padding-bottom: 12px;
  border-bottom: 1px solid var(--c-brand-glass-border);
}
.preview-dots {
  display: flex;
  gap: 4px;
}
.preview-dots i {
  width: 6px;
  height: 6px;
  background: var(--c-brand-grid);
  border-radius: 50%;
}
.preview-dots i:first-child {
  background: var(--c-danger);
}
.preview-dots i:nth-child(2) {
  background: var(--c-warning);
}
.preview-dots i:nth-child(3) {
  background: var(--c-success);
}
.preview-window-title {
  flex: 1;
  font-size: 11px;
  color: var(--c-brand-muted);
}
.preview-window-title span {
  padding: 0 3px;
  color: var(--c-brand-subtle);
}
.preview-lock,
.source-status {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 10px;
  font-weight: var(--fw-medium);
  color: var(--c-brand-highlight);
}
.preview-question {
  width: fit-content;
  max-width: 86%;
  margin: 16px 0 14px auto;
  padding: 9px 12px;
  font-size: var(--fs-sm);
  color: var(--c-white);
  background: var(--c-brand-glass-soft);
  border: 1px solid var(--c-brand-glass-border);
  border-radius: 13px 13px 4px 13px;
}
.preview-answer {
  display: flex;
  align-items: flex-start;
  gap: 10px;
}
.preview-avatar {
  display: inline-flex;
  flex-shrink: 0;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  color: var(--c-brand-highlight);
  background: var(--c-brand-glass-soft);
  border: 1px solid var(--c-brand-glass-border);
  border-radius: 9px;
}
.preview-answer-body {
  padding-top: 1px;
}
.preview-label {
  font-size: 10px;
  font-weight: var(--fw-semibold);
  letter-spacing: 0.03em;
  color: var(--c-brand-subtle);
}
.preview-answer-body p {
  margin-top: 4px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--c-white);
}
.preview-source {
  margin: 12px 0 0 38px;
  padding: 9px 10px;
  font-size: 10px;
  color: var(--c-brand-muted);
  background: var(--c-brand-deep-glass);
  border: 1px solid var(--c-brand-glass-border);
  border-radius: 9px;
}
.preview-source > span:first-child {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.source-status {
  color: var(--c-brand-success);
}

.brand-points {
  display: flex;
  flex-wrap: wrap;
  gap: 10px 20px;
  margin-top: 22px;
}
.brand-points li {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  font-size: 11px;
  color: var(--c-brand-muted);
}
.brand-points svg {
  color: var(--c-brand-highlight);
}
.brand-footer {
  margin-top: clamp(22px, 4vh, 38px);
  font-size: 10px;
  letter-spacing: 0.04em;
  color: var(--c-brand-subtle);
}
.brand-footer span {
  padding: 0 5px;
  color: var(--c-brand-highlight);
}

/* ── 右栏：登录 / 注册 ── */
.auth-panel {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: clamp(24px, 3vw, 52px);
  background:
    radial-gradient(ellipse at 100% 0%, var(--c-auth-glow), transparent 40%),
    var(--c-bg);
}
.auth-card {
  width: 100%;
  max-width: 420px;
  padding: clamp(26px, 3vw, 38px);
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: 24px;
  box-shadow: var(--sh-float);
}
.auth-heading {
  display: flex;
  align-items: center;
  gap: 13px;
}
.auth-heading-icon {
  display: inline-flex;
  flex-shrink: 0;
  align-items: center;
  justify-content: center;
  width: 42px;
  height: 42px;
  color: var(--c-accent-text);
  background: var(--c-accent-soft);
  border: 1px solid var(--c-accent-border);
  border-radius: 13px;
}
.auth-eyebrow {
  margin-bottom: 3px;
  font-size: 9px;
  font-weight: var(--fw-semibold);
  letter-spacing: 0.14em;
  color: var(--c-text-3);
}
.auth-heading h2 {
  font-size: 23px;
  font-weight: var(--fw-semibold);
  letter-spacing: -0.035em;
}
.auth-intro {
  margin: 10px 0 14px;
  font-size: var(--fs-sm);
  line-height: var(--lh-base);
  color: var(--c-text-2);
}
.auth-card > :deep(.ui-tabs) {
  width: fit-content;
  margin-bottom: 14px;
}

.auth-form {
  display: flex;
  flex-direction: column;
  gap: var(--sp-4);
  margin-top: 0;
}

.field {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.field-label {
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--c-text-2);
}
.field-hint {
  font-size: var(--fs-xs);
  color: var(--c-text-3);
}
.field-hint.is-bad {
  color: var(--c-danger-text);
}

.password-wrap {
  position: relative;
  display: flex;
  align-items: center;
}
.password-toggle {
  position: absolute;
  right: var(--sp-2);
  display: inline-flex;
  padding: var(--sp-1);
  color: var(--c-text-3);
  border-radius: var(--r-sm);
}
.password-toggle:hover {
  color: var(--c-text);
  background: var(--c-surface-2);
}

/* ── 实时校验清单 ── */
.rules {
  padding: var(--sp-3);
  background: var(--c-surface-2);
  border-radius: var(--r-md);
}
.rules-title {
  margin-bottom: var(--sp-2);
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  color: var(--c-text-2);
}
.rules-list {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
}
.rule {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  font-size: var(--fs-sm);
  color: var(--c-text-3);
  transition: color var(--dur-fast) var(--ease);
}
/* 满足的条目变绿并打勾 —— 边输入边给正反馈，不用等提交 */
.rule.is-ok {
  color: var(--c-success-text);
}
.rules-error {
  margin-top: var(--sp-2);
  font-size: var(--fs-xs);
  color: var(--c-warning-text);
}

.switch-hint {
  font-size: var(--fs-sm);
  color: var(--c-text-3);
  text-align: center;
}
.link {
  color: var(--c-accent);
  font-size: inherit;
}
.link:hover {
  text-decoration: underline;
}

.rules-loading {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  margin-top: var(--sp-4);
  font-size: var(--fs-xs);
  color: var(--c-text-3);
}

/* ── 窄屏：品牌区收敛，表单成为视觉主体 ── */
@media (max-width: 900px) {
  .auth-page {
    grid-template-columns: 1fr;
  }
  .auth-brand {
    min-height: auto;
    padding: 24px 24px 22px;
  }
  .brand-top {
    margin-bottom: 24px;
  }
  .brand-copy {
    max-width: 700px;
  }
  .brand-title {
    margin-top: 14px;
    font-size: clamp(28px, 5vw, 36px);
  }
  .brand-desc {
    max-width: 58ch;
    margin-top: 10px;
    font-size: var(--fs-sm);
    line-height: 1.7;
  }
  .product-preview,
  .brand-points,
  .brand-footer {
    display: none;
  }
  .auth-panel {
    padding: 24px 20px 36px;
  }
  .auth-card {
    max-width: 460px;
  }
}

@media (max-width: 520px) {
  .auth-brand {
    padding: 20px 20px 18px;
  }
  .brand-top {
    margin-bottom: 20px;
  }
  .brand-status,
  .brand-name small {
    display: none;
  }
  .brand-title {
    font-size: 28px;
  }
  .brand-desc {
    font-size: 13px;
  }
  .auth-panel {
    align-items: flex-start;
    padding: 18px 14px 28px;
  }
  .auth-card {
    padding: 24px 20px;
    border-radius: 20px;
  }
}
</style>
