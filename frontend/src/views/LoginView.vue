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
  { key: 'register', label: '注册', icon: 'plus' },
]

const mode = ref('login')
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

onMounted(loadRules)
</script>

<template>
  <div class="auth-page">
    <!-- 左栏：品牌与定位。窄屏隐藏。 -->
    <section class="auth-brand">
      <div class="brand-top">
        <span class="brand-mark"><UiIcon name="layers" :size="20" /></span>
        <span class="brand-name">私人知识库</span>
      </div>

      <h1 class="brand-title">个人数字分身</h1>
      <p class="brand-desc">
        把自己的简历、项目文档、技术笔记喂进私有知识库，
        让 AI 以本人的身份回答问题 —— 并且<strong>句句有据可查</strong>。
      </p>

      <ul class="brand-points">
        <li>
          <UiIcon name="layers" :size="16" />
          <span>每个回答都标注它依据了知识库里的哪些片段与相关度</span>
        </li>
        <li>
          <UiIcon name="zap" :size="16" />
          <span>可展开查看完整推理过程：检索了哪些内容、模型怎么想的</span>
        </li>
        <li>
          <UiIcon name="volume" :size="16" />
          <span>支持语音朗读，可用复刻的真人音色</span>
        </li>
        <li>
          <UiIcon name="shield" :size="16" />
          <span>只依据已上传的文档作答，未知内容如实说明，不编造</span>
        </li>
      </ul>
    </section>

    <!-- 右栏：登录 / 注册 -->
    <section class="auth-panel">
      <div class="auth-card">
        <UiTabs v-model="mode" :tabs="TABS" variant="pill" />

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
                autocomplete="current-password"
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
              <button type="button" class="link" @click="mode = 'register'">去注册</button>
            </template>
            <template v-else>
              已有账号？
              <button type="button" class="link" @click="mode = 'login'">去登录</button>
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
  display: grid;
  grid-template-columns: 1.1fr 1fr;
  min-height: 100%;
  background: var(--c-bg);
}

/* ── 左栏：品牌 ── */
.auth-brand {
  display: flex;
  flex-direction: column;
  justify-content: center;
  padding: var(--sp-12) var(--sp-10);
  background: var(--c-surface);
  border-right: 1px solid var(--c-border);
}

.brand-top {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  margin-bottom: var(--sp-8);
}
.brand-mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 40px;
  color: var(--c-white);
  background: var(--c-accent);
  border-radius: var(--r-lg);
}
.brand-name {
  font-size: var(--fs-md);
  font-weight: var(--fw-semibold);
}

.brand-title {
  font-size: var(--fs-2xl);
  font-weight: var(--fw-semibold);
  letter-spacing: -0.01em;
}
.brand-desc {
  max-width: 40ch;
  margin-top: var(--sp-3);
  font-size: var(--fs-base);
  line-height: var(--lh-loose);
  color: var(--c-text-2);
}
.brand-desc strong {
  color: var(--c-accent-text);
}

.brand-points {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  margin-top: var(--sp-8);
  padding-top: var(--sp-6);
  border-top: 1px solid var(--c-border);
}
.brand-points li {
  display: flex;
  align-items: flex-start;
  gap: var(--sp-3);
  font-size: var(--fs-sm);
  line-height: var(--lh-base);
  color: var(--c-text-2);
}
.brand-points svg {
  margin-top: 3px;
  color: var(--c-accent);
}

/* ── 右栏：表单 ── */
.auth-panel {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--sp-8) var(--sp-6);
}

.auth-card {
  width: 100%;
  max-width: 380px;
  padding: var(--sp-6);
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--r-xl);
  box-shadow: var(--sh-2);
}

.auth-form {
  display: flex;
  flex-direction: column;
  gap: var(--sp-4);
  margin-top: var(--sp-5);
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

/* ── 窄屏：单栏，品牌区收成一条 ── */
@media (max-width: 900px) {
  .auth-page {
    grid-template-columns: 1fr;
  }
  .auth-brand {
    padding: var(--sp-8) var(--sp-5) var(--sp-6);
    border-right: none;
    border-bottom: 1px solid var(--c-border);
  }
  .brand-points {
    display: none;
  }
  .brand-title {
    font-size: var(--fs-xl);
  }
  .auth-panel {
    padding: var(--sp-6) var(--sp-4) var(--sp-10);
  }
}
</style>
