<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h2 class="page-title">管理面板</h2>
        <p class="page-sub">用户、对话、审计与系统运行状态</p>
      </div>
    </header>

    <UiTabs v-model="activeTab" :tabs="tabs" />

    <!-- ═══ 仪表盘 ═══ -->
    <section v-if="activeTab === 'dashboard'" class="page-body">
      <div v-if="dashLoading" class="center">
        <UiSpinner :size="24" label="加载中" />
      </div>
      <UiAlert v-else-if="dashError" tone="danger">
        <div class="alert-inner">
          <span class="alert-text">仪表盘加载失败：{{ dashError }}</span>
          <UiButton size="sm" @click="loadDashboard">重试</UiButton>
        </div>
      </UiAlert>
      <template v-else>
        <div class="kpi-hero">
          <UiCard v-for="s in heroStats" :key="s.key" class="kpi">
            <span class="kpi-icon is-accent"><UiIcon :name="s.icon" :size="18" /></span>
            <span class="kpi-value kpi-value-lg">{{ s.value }}</span>
            <span class="kpi-label">{{ s.label }}</span>
          </UiCard>
        </div>
        <h3 class="group-title">资源与用量</h3>
        <div class="kpi-grid">
          <UiCard v-for="s in subStats" :key="s.key" class="kpi">
            <span class="kpi-icon is-neutral"><UiIcon :name="s.icon" :size="16" /></span>
            <span class="kpi-value">{{ s.value }}</span>
            <span class="kpi-label">{{ s.label }}</span>
          </UiCard>
        </div>
      </template>
    </section>

    <!-- ═══ 用户管理 ═══ -->
    <section v-if="activeTab === 'users'" class="page-body">
      <UiAlert v-if="usersError" tone="danger">
        <div class="alert-inner">
          <span class="alert-text">用户列表加载失败：{{ usersError }}</span>
          <UiButton size="sm" @click="loadUsers">重试</UiButton>
        </div>
      </UiAlert>
      <UiTable
        v-else
        :columns="userColumns"
        :rows="users"
        :loading="usersLoading"
        empty-title="暂无用户数据"
        empty-icon="user"
      >
        <template #cell-role="{ row }">
          <UiBadge :tone="row.role === 'admin' ? 'accent' : 'neutral'">
            {{ row.role === 'admin' ? '管理员' : '用户' }}
          </UiBadge>
        </template>
        <template #cell-created_at="{ value }">{{ formatDate(value) }}</template>
        <template #cell-actions="{ row }">
          <div class="row-actions">
            <UiButton size="sm" :disabled="row.id === auth.ownerId" @click="toggleRole(row)">
              {{ row.role === 'admin' ? '降为用户' : '升管理员' }}
            </UiButton>
            <UiButton
              size="sm"
              variant="danger"
              :disabled="row.id === auth.ownerId"
              @click="delUser(row)"
            >
              <template #icon><UiIcon name="trash" :size="14" /></template>
              删除
            </UiButton>
          </div>
        </template>
      </UiTable>
    </section>

    <!-- ═══ 对话日志 ═══ -->
    <section v-if="activeTab === 'chatLogs'" class="page-body">
      <div class="filter-row">
        <div class="filter-field">
          <UiInput v-model="chatFilterUser" size="sm" placeholder="按用户名筛选" @enter="loadChatLogs" />
        </div>
        <UiButton size="sm" :loading="chatLoading" @click="loadChatLogs">
          <template #icon><UiIcon name="search" :size="14" /></template>
          查询
        </UiButton>
      </div>

      <UiAlert v-if="chatError" tone="danger">
        <div class="alert-inner">
          <span class="alert-text">对话日志加载失败：{{ chatError }}</span>
          <UiButton size="sm" @click="loadChatLogs">重试</UiButton>
        </div>
      </UiAlert>
      <div v-else-if="chatLoading && !chatLogs.length" class="center">
        <UiSpinner :size="22" label="加载中" />
      </div>
      <UiEmpty
        v-else-if="!chatLogs.length"
        icon="chat"
        title="暂无对话记录"
        :description="chatFilterUser ? `没有匹配「${chatFilterUser}」的记录` : '系统里还没有任何对话记录'"
        compact
      >
        <UiButton v-if="chatFilterUser" size="sm" @click="clearChatFilter">清除筛选</UiButton>
      </UiEmpty>
      <div v-else class="log-list">
        <UiCard v-for="log in chatLogs" :key="log.id" :padded="false" class="log-card">
          <button
            type="button"
            class="log-head"
            :aria-expanded="expandedLogId === log.id"
            @click="toggleLog(log)"
          >
            <UiIcon name="user" :size="14" class="log-head-icon" />
            <span class="log-user">{{ log.username || '匿名' }}</span>
            <UiBadge v-if="log.agent_mode" tone="info">{{ log.agent_mode }}</UiBadge>
            <span class="log-time" :title="formatDateTime(log.created_at)">
              {{ formatRelativeTime(log.created_at) }}
            </span>
            <UiIcon
              :name="expandedLogId === log.id ? 'chevron-down' : 'chevron-right'"
              :size="16"
              class="log-caret"
            />
          </button>

          <div class="log-body">
            <div class="log-line">
              <span class="qa-tag">Q</span>
              <p class="log-text">
                {{ expandedLogId === log.id ? log.question : truncate(log.question, 150) }}
              </p>
            </div>
            <div class="log-line">
              <span class="qa-tag">A</span>
              <p class="log-text">
                {{ expandedLogId === log.id ? log.answer : truncate(log.answer, 200) }}
              </p>
            </div>

            <!-- 展开时显示完整推理过程。reasoning 落库有两种历史格式，
                 统一交给 parseStoredReasoning 解析，模板只消费解析后的结构。 -->
            <div v-if="expandedLogId === log.id && log.reasoning" class="reason-panel">
              <div class="reason-head">
                <UiIcon name="zap" :size="14" />
                <span>推理过程</span>
                <UiBadge v-if="expandedReasoning.steps.length" tone="accent" mono>
                  {{ expandedReasoning.steps.length }} 步
                </UiBadge>
                <UiBadge v-if="expandedReasoning.legacy" tone="neutral">旧格式</UiBadge>
              </div>

              <div v-if="expandedReasoning.thinking" class="reason-thinking">
                <div class="reason-thinking-label">
                  <UiIcon name="message" :size="13" /> 模型思考
                </div>
                <p class="reason-thinking-text">{{ expandedReasoning.thinking }}</p>
              </div>

              <div v-if="expandedReasoning.steps.length" class="reason-steps">
                <div v-for="(st, si) in expandedReasoning.steps" :key="si" class="reason-step">
                  <UiIcon :name="st.iconName" :size="14" class="reason-step-icon" />
                  <span class="reason-step-text">{{ st.text }}</span>
                </div>
              </div>

              <!-- 证据轨只在非旧格式（真正带 evidence 的 JSON）下出现 -->
              <div
                v-if="!expandedReasoning.legacy && expandedReasoning.evidence.length"
                class="reason-evidence"
              >
                <div class="reason-evidence-label">
                  <UiIcon name="layers" :size="13" /> 检索证据（{{ expandedReasoning.evidence.length }}）
                </div>
                <div v-for="ev in expandedReasoning.evidence" :key="ev.key" class="evidence-item">
                  <div class="evidence-head">
                    <UiIcon name="file" :size="13" />
                    <span class="evidence-source">{{ ev.source }}</span>
                    <UiBadge v-if="ev.score !== null" tone="neutral" mono>{{ fmtScore(ev.score) }}</UiBadge>
                  </div>
                  <p class="evidence-text">{{ truncate(ev.content, 160) }}</p>
                </div>
              </div>

              <!-- 两种格式都解析不出内容时兜底显示原文，避免"展开是空的" -->
              <pre v-if="!expandedReasoning.steps.length && !expandedReasoning.thinking" class="reason-raw">{{ log.reasoning }}</pre>
            </div>
          </div>
        </UiCard>
      </div>
    </section>

    <!-- ═══ 文件管理 ═══ -->
    <section v-if="activeTab === 'files'" class="page-body">
      <div class="filter-row">
        <div class="filter-field">
          <UiInput v-model="fileFilterUser" size="sm" placeholder="按用户名筛选" @enter="loadFiles" />
        </div>
        <UiButton size="sm" :loading="fileLoading" @click="loadFiles">
          <template #icon><UiIcon name="search" :size="14" /></template>
          查询
        </UiButton>
      </div>

      <UiAlert v-if="fileError" tone="danger">
        <div class="alert-inner">
          <span class="alert-text">文件列表加载失败：{{ fileError }}</span>
          <UiButton size="sm" @click="loadFiles">重试</UiButton>
        </div>
      </UiAlert>
      <UiTable
        v-else
        :columns="fileColumns"
        :rows="allFiles"
        :loading="fileLoading"
        empty-title="暂无文件"
        empty-description="当前筛选条件下没有文件记录"
        empty-icon="file"
      >
        <template #cell-filename="{ row }">
          <span class="file-cell">
            <UiIcon name="file" :size="14" class="file-cell-icon" />
            <span class="file-cell-name">{{ row.filename }}</span>
          </span>
        </template>
        <template #cell-file_size="{ value }">{{ formatSize(value) }}</template>
        <template #cell-created_at="{ value }">{{ formatDateTime(value) }}</template>
      </UiTable>
    </section>

    <!-- ═══ 审计日志 ═══ -->
    <section v-if="activeTab === 'audit'" class="page-body">
      <div class="filter-row">
        <div class="filter-field">
          <UiInput v-model="auditFilterUser" size="sm" placeholder="用户名" @enter="loadAuditLogs" />
        </div>
        <div class="filter-field">
          <UiInput
            v-model="auditFilterAction"
            size="sm"
            placeholder="操作（如 login）"
            @enter="loadAuditLogs"
          />
        </div>
        <UiButton size="sm" :loading="auditLoading" @click="loadAuditLogs">
          <template #icon><UiIcon name="search" :size="14" /></template>
          查询
        </UiButton>
      </div>

      <UiAlert v-if="auditError" tone="danger">
        <div class="alert-inner">
          <span class="alert-text">审计日志加载失败：{{ auditError }}</span>
          <UiButton size="sm" @click="loadAuditLogs">重试</UiButton>
        </div>
      </UiAlert>
      <UiTable
        v-else
        :columns="auditColumns"
        :rows="auditLogs"
        :loading="auditLoading"
        empty-title="暂无审计日志"
        empty-description="当前筛选条件下没有审计记录"
        empty-icon="list"
      >
        <template #cell-created_at="{ value }">{{ formatDateTime(value) }}</template>
        <template #cell-username="{ value }">{{ value || '-' }}</template>
        <template #cell-action="{ value }">{{ truncate(value, 40) }}</template>
        <template #cell-ip_address="{ value }">{{ value || '-' }}</template>
        <template #cell-duration_ms="{ value }">{{ value ? `${value} ms` : '-' }}</template>
        <template #cell-status="{ value }">
          <UiBadge :tone="value === 'success' ? 'success' : 'danger'" dot>{{ value }}</UiBadge>
        </template>
      </UiTable>
    </section>

    <!-- ═══ 系统状态 ═══ -->
    <section v-if="activeTab === 'system'" class="page-body">
      <div class="sys-grid">
        <UiCard>
          <template #header>
            <span class="card-title"><UiIcon name="zap" :size="16" /> 熔断器</span>
            <UiButton size="sm" :loading="circuitLoading" @click="loadCircuitStatus">
              <template #icon><UiIcon name="refresh" :size="14" /></template>
              刷新
            </UiButton>
          </template>

          <UiAlert v-if="circuitError" tone="danger">
            <div class="alert-inner">
              <span class="alert-text">熔断器状态加载失败：{{ circuitError }}</span>
              <UiButton size="sm" @click="loadCircuitStatus">重试</UiButton>
            </div>
          </UiAlert>
          <div v-else-if="circuitLoading && !circuits.length" class="center">
            <UiSpinner :size="20" label="加载中" />
          </div>
          <UiEmpty v-else-if="!circuits.length" icon="zap" title="暂无熔断器数据" compact />
          <div v-else class="circ-list">
            <div v-for="c in circuits" :key="c.name" class="circ-row">
              <span class="circ-name">{{ c.name }}</span>
              <UiBadge :tone="circuitTone(c.state)" dot>{{ c.state }}</UiBadge>
              <span class="circ-fail">失败 {{ formatTokens(c.failure_count) }}</span>
            </div>
          </div>
        </UiCard>

        <UiCard>
          <template #header>
            <span class="card-title"><UiIcon name="database" :size="16" /> 异步队列</span>
            <UiButton size="sm" :loading="queueLoading" @click="loadQueueStatus">
              <template #icon><UiIcon name="refresh" :size="14" /></template>
              刷新
            </UiButton>
          </template>

          <UiAlert v-if="queueError" tone="danger">
            <div class="alert-inner">
              <span class="alert-text">队列状态加载失败：{{ queueError }}</span>
              <UiButton size="sm" @click="loadQueueStatus">重试</UiButton>
            </div>
          </UiAlert>
          <div v-else-if="queueLoading && !queueStatus" class="center">
            <UiSpinner :size="20" label="加载中" />
          </div>
          <UiEmpty v-else-if="!queueStatus" icon="database" title="暂无队列数据" compact />
          <div v-else class="queue-rows">
            <div class="queue-row">
              <span class="queue-k">队列积压</span>
              <span class="queue-v is-mono">{{ formatTokens(queueStatus.queue_size) }}</span>
            </div>
            <div class="queue-row">
              <span class="queue-k">后端</span>
              <span class="queue-v">{{ backendLabel(queueStatus.backend) }}</span>
            </div>
            <div class="queue-row">
              <span class="queue-k">健康状态</span>
              <UiBadge :tone="queueStatus.healthy ? 'success' : 'danger'" dot>
                {{ queueStatus.healthy ? '正常' : '异常' }}
              </UiBadge>
            </div>
          </div>
        </UiCard>
      </div>
    </section>

    <!-- 破坏性操作二次确认：不再调用浏览器原生确认框 -->
    <UiModal
      :open="confirmState.open"
      :title="confirmState.title"
      size="sm"
      :close-on-overlay="false"
      @close="closeConfirm"
    >
      <p class="confirm-lead">{{ confirmState.message }}</p>
      <ul v-if="confirmState.details.length" class="confirm-list">
        <li v-for="d in confirmState.details" :key="d">{{ d }}</li>
      </ul>
      <p v-if="confirmState.note" class="confirm-note">{{ confirmState.note }}</p>
      <template #footer>
        <UiButton variant="secondary" :disabled="confirmState.busy" @click="closeConfirm">
          取消
        </UiButton>
        <UiButton
          :variant="confirmState.danger ? 'danger' : 'primary'"
          :loading="confirmState.busy"
          @click="runConfirm"
        >
          {{ confirmState.confirmText }}
        </UiButton>
      </template>
    </UiModal>
  </div>
</template>

<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { useAuthStore } from '../stores/auth.js'
import { useToast } from '../composables/useToast.js'
import { useAsyncData } from '../composables/useAsyncData.js'
import {
  formatTokens,
  formatCost,
  formatSize,
  formatDate,
  formatDateTime,
  formatRelativeTime,
  truncate,
} from '../utils/format.js'
import { parseStoredReasoning } from '../utils/reasoning.js'
import {
  getDashboard, getUsers, updateUserRole, deleteUser,
  getChatLogs, getFiles, getAuditLogs,
  getCircuitStatus, getQueueStatus,
} from '../api/admin.js'
import UiTabs from '../components/ui/UiTabs.vue'
import UiTable from '../components/ui/UiTable.vue'
import UiBadge from '../components/ui/UiBadge.vue'
import UiButton from '../components/ui/UiButton.vue'
import UiInput from '../components/ui/UiInput.vue'
import UiCard from '../components/ui/UiCard.vue'
import UiAlert from '../components/ui/UiAlert.vue'
import UiEmpty from '../components/ui/UiEmpty.vue'
import UiSpinner from '../components/ui/UiSpinner.vue'
import UiIcon from '../components/ui/UiIcon.vue'
import UiModal from '../components/ui/UiModal.vue'

const auth = useAuthStore()
const toast = useToast()

const tabs = [
  { key: 'dashboard', label: '仪表盘', icon: 'chart' },
  { key: 'users', label: '用户', icon: 'user' },
  { key: 'chatLogs', label: '对话', icon: 'chat' },
  { key: 'files', label: '文件', icon: 'file' },
  { key: 'audit', label: '审计', icon: 'list' },
  { key: 'system', label: '系统', icon: 'sliders' },
]
const activeTab = ref('dashboard')

/**
 * 错误可见性兜底
 * ------------
 * 改造前 chat-logs / files / audit / system 四个 tab 的 try/catch 是空的，
 * 请求失败被彻底吞掉，用户看到的是一个空列表，分不清"没有数据"还是"加载失败"。
 * 现在每个请求的 error 都会被订阅：既弹 toast（即时提醒），
 * 也在对应 tab 里渲染 UiAlert + 重试按钮（持续可见、可操作）。
 */
function surfaceError(errorRef) {
  watch(errorRef, (message) => {
    if (message) toast.error(message)
  })
}

// —— 仪表盘 ——
const { data: dashData, loading: dashLoading, error: dashError, run: loadDashboard } =
  useAsyncData(() => getDashboard(), { immediate: true })
surfaceError(dashError)

const heroStats = computed(() => {
  const d = dashData.value || {}
  return [
    { key: 'user_count', label: '用户总数', value: formatTokens(d.user_count), icon: 'user' },
    { key: 'total_chats', label: '总对话数', value: formatTokens(d.total_chats), icon: 'chat' },
    { key: 'today_chats', label: '今日对话', value: formatTokens(d.today_chats), icon: 'activity' },
  ]
})
const subStats = computed(() => {
  const d = dashData.value || {}
  return [
    { key: 'total_tokens', label: '总 Token', value: formatTokens(d.total_tokens), icon: 'zap' },
    { key: 'total_cost', label: '总费用', value: `$${formatCost(d.total_cost)}`, icon: 'chart' },
    { key: 'file_count', label: '文件总数', value: formatTokens(d.file_count), icon: 'file' },
    { key: 'disk', label: '上传文件', value: formatSize((d.disk_used_mb || 0) * 1024 * 1024), icon: 'upload' },
    { key: 'chroma', label: '向量库', value: formatSize((d.chroma_db_mb || 0) * 1024 * 1024), icon: 'database' },
  ]
})

// —— 用户 ——
const { data: usersData, loading: usersLoading, error: usersError, run: loadUsers } =
  useAsyncData(() => getUsers())
surfaceError(usersError)
const users = computed(() => usersData.value?.users || [])

const userColumns = [
  { key: 'id', label: 'ID', width: '64px', mono: true },
  { key: 'username', label: '用户名' },
  { key: 'role', label: '角色', width: '96px' },
  { key: 'file_count', label: '文件数', width: '80px', align: 'right' },
  { key: 'chat_count', label: '对话数', width: '80px', align: 'right' },
  { key: 'created_at', label: '注册时间', width: '120px' },
  { key: 'actions', label: '操作', width: '180px' },
]

// —— 对话日志 ——
const chatFilterUser = ref('')
const {
  data: chatData, loading: chatLoading, error: chatError, run: loadChatLogs,
} = useAsyncData(() => getChatLogs(50, 0, chatFilterUser.value.trim()))
surfaceError(chatError)
const chatLogs = computed(() => chatData.value?.logs || [])

const expandedLogId = ref(null)
// 展开时解析一次即可：模板里对每张卡片反复解析是浪费，而且列表可能有几十条
const expandedReasoning = ref({ steps: [], thinking: '', evidence: [], legacy: false })

function toggleLog(log) {
  if (expandedLogId.value === log.id) {
    expandedLogId.value = null
    expandedReasoning.value = { steps: [], thinking: '', evidence: [], legacy: false }
    return
  }
  expandedLogId.value = log.id
  expandedReasoning.value = parseStoredReasoning(log.reasoning)
}

function clearChatFilter() {
  chatFilterUser.value = ''
  loadChatLogs()
}

// —— 文件 ——
const fileFilterUser = ref('')
const { data: filesData, loading: fileLoading, error: fileError, run: loadFiles } =
  useAsyncData(() => getFiles(50, 0, fileFilterUser.value.trim()))
surfaceError(fileError)
const allFiles = computed(() => filesData.value?.files || [])

const fileColumns = [
  { key: 'id', label: 'ID', width: '64px', mono: true },
  { key: 'username', label: '用户' },
  { key: 'filename', label: '文件名' },
  { key: 'file_size', label: '大小', width: '96px', align: 'right' },
  { key: 'chunk_count', label: '分块', width: '80px', align: 'right' },
  { key: 'created_at', label: '上传时间', width: '140px' },
]

// —— 审计日志 ——
const auditFilterUser = ref('')
const auditFilterAction = ref('')
const { data: auditData, loading: auditLoading, error: auditError, run: loadAuditLogs } =
  useAsyncData(() => getAuditLogs(50, auditFilterAction.value.trim(), auditFilterUser.value.trim()))
surfaceError(auditError)
const auditLogs = computed(() => auditData.value?.logs || [])

const auditColumns = [
  { key: 'created_at', label: '时间', width: '150px', mono: true },
  { key: 'username', label: '用户', width: '120px' },
  { key: 'action', label: '操作', mono: true },
  { key: 'ip_address', label: 'IP', width: '130px', mono: true },
  { key: 'duration_ms', label: '耗时', width: '90px', align: 'right' },
  { key: 'status', label: '状态', width: '96px' },
]

// —— 系统状态 ——
const { data: circuitData, loading: circuitLoading, error: circuitError, run: loadCircuitStatus } =
  useAsyncData(() => getCircuitStatus())
const { data: queueData, loading: queueLoading, error: queueError, run: loadQueueStatus } =
  useAsyncData(() => getQueueStatus())
surfaceError(circuitError)
surfaceError(queueError)
const circuits = computed(() => circuitData.value?.circuits || [])
const queueStatus = computed(() => queueData.value)

/** 熔断器状态映射为徽章语义色 */
function circuitTone(state) {
  return { closed: 'success', open: 'danger', half_open: 'warning' }[state] || 'neutral'
}

/** 队列后端名称：进程内线程池 / Redis(RQ) */
function backendLabel(backend) {
  return {
    thread_pool: '进程内线程池',
    rq: 'Redis (RQ)',
  }[backend] || backend || '未知'
}

/** 相关度分数：证据轨用两位小数，便于竖向对齐比较 */
function fmtScore(v) {
  const n = Number(v)
  return Number.isFinite(n) ? n.toFixed(2) : ''
}

// —— 二次确认弹窗（角色变更 / 删除用户）——
const confirmState = reactive({
  open: false,
  title: '',
  message: '',
  details: [],
  note: '',
  confirmText: '确认',
  danger: true,
  busy: false,
  action: null,
})

function openConfirm(opts) {
  Object.assign(confirmState, {
    open: true,
    details: [],
    note: '',
    danger: true,
    busy: false,
    action: null,
    ...opts,
  })
}

function closeConfirm() {
  confirmState.open = false
  confirmState.action = null
}

async function runConfirm() {
  if (!confirmState.action || confirmState.busy) return
  confirmState.busy = true
  try {
    await confirmState.action()
  } catch (e) {
    toast.error(e?.message || '操作失败')
  } finally {
    confirmState.busy = false
    closeConfirm()
  }
}

function toggleRole(u) {
  const toAdmin = u.role !== 'admin'
  openConfirm({
    title: toAdmin ? '提升为管理员' : '降为普通用户',
    message: `确定将「${u.username}」的角色改为「${toAdmin ? '管理员' : '普通用户'}」？`,
    details: toAdmin
      ? ['管理员可查看全站用户、对话日志与审计记录']
      : ['该用户将失去管理入口与全部管理权限'],
    confirmText: toAdmin ? '提升' : '降级',
    danger: false,
    action: async () => {
      const res = await updateUserRole(u.id, toAdmin ? 'admin' : 'user')
      if (!res.success) throw new Error(res.message || res.detail || '修改失败')
      toast.success(res.message || '角色已更新')
      await loadUsers()
    },
  })
}

function delUser(u) {
  openConfirm({
    title: '删除用户',
    message: `确定删除用户「${u.username}」？以下数据将被一并清除：`,
    details: ['该用户的全部对话记录', '已上传的文件与向量数据', 'Token 统计', '相关的审计日志'],
    note: '此操作不可恢复。',
    confirmText: '删除用户',
    danger: true,
    action: async () => {
      const res = await deleteUser(u.id)
      if (!res.success) throw new Error(res.message || res.detail || '删除失败')
      toast.success(res.message || '用户已删除')
      await loadUsers()
    },
  })
}

// —— 切换标签页时懒加载对应数据 ——
watch(activeTab, (tab) => {
  switch (tab) {
    case 'dashboard': loadDashboard(); break
    case 'users': loadUsers(); break
    case 'chatLogs': loadChatLogs(); break
    case 'files': loadFiles(); break
    case 'audit': loadAuditLogs(); break
    case 'system': loadCircuitStatus(); loadQueueStatus(); break
  }
})
</script>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: var(--sp-5);
  width: 100%;
}

.page-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--sp-4);
}
.page-title {
  font-size: var(--fs-xl);
  font-weight: var(--fw-semibold);
  color: var(--c-text);
}
.page-sub {
  margin-top: var(--sp-1);
  font-size: var(--fs-sm);
  color: var(--c-text-2);
}

.page-body {
  display: flex;
  flex-direction: column;
  gap: var(--sp-4);
}

.center {
  display: flex;
  justify-content: center;
  padding: var(--sp-10);
}

/* —— 错误提示条内的"文案 + 重试"布局 —— */
.alert-inner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-3);
  flex-wrap: wrap;
}
.alert-text {
  flex: 1;
  min-width: 0;
}

/* ══ 仪表盘：主次分明的两组指标 ══ */
.group-title {
  font-size: var(--fs-md);
  font-weight: var(--fw-semibold);
  color: var(--c-text-2);
}
.kpi-hero,
.kpi-grid {
  display: grid;
  gap: var(--sp-4);
}
.kpi-hero {
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
}
.kpi-grid {
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
}

.kpi {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
}
.kpi-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  margin-bottom: var(--sp-2);
  border-radius: var(--r-md);
}
.kpi-icon.is-accent {
  color: var(--c-accent-text);
  background: var(--c-accent-soft);
}
.kpi-icon.is-neutral {
  color: var(--c-text-2);
  background: var(--c-surface-2);
}
.kpi-value {
  font-size: var(--fs-xl);
  font-weight: var(--fw-semibold);
  line-height: var(--lh-tight);
  color: var(--c-text);
  font-variant-numeric: tabular-nums;
}
.kpi-value-lg {
  font-size: var(--fs-2xl);
}
.kpi-label {
  font-size: var(--fs-sm);
  color: var(--c-text-2);
}

/* ══ 筛选行 ══ */
.filter-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-2);
}
.filter-field {
  width: 200px;
  max-width: 100%;
}

/* ══ 表格单元格 ══ */
.row-actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
}
.file-cell {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-2);
  min-width: 0;
}
.file-cell-icon {
  color: var(--c-text-3);
}
.file-cell-name {
  overflow-wrap: anywhere;
}

/* ══ 对话日志卡片 ══ */
.log-list {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
}
.log-card {
  overflow: hidden;
}
.log-head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  width: 100%;
  padding: var(--sp-3) var(--sp-4);
  font-size: var(--fs-sm);
  color: var(--c-text-2);
  text-align: left;
  background: var(--c-surface-2);
  border-bottom: 1px solid var(--c-border);
  transition: background var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease);
}
.log-head:hover {
  color: var(--c-text);
  background: var(--c-surface-3);
}
.log-head-icon {
  color: var(--c-text-3);
}
.log-user {
  font-weight: var(--fw-semibold);
  color: var(--c-text);
}
.log-time {
  margin-left: auto;
  font-size: var(--fs-xs);
  font-family: var(--font-mono);
  color: var(--c-text-3);
}
.log-caret {
  color: var(--c-text-3);
}

.log-body {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  padding: var(--sp-4);
}
.log-line {
  display: flex;
  align-items: flex-start;
  gap: var(--sp-2);
}
.qa-tag {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  margin-top: 1px;
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  color: var(--c-text-2);
  background: var(--c-surface-2);
  border: 1px solid var(--c-border);
  border-radius: var(--r-sm);
}
.log-text {
  flex: 1;
  min-width: 0;
  font-size: var(--fs-sm);
  line-height: var(--lh-base);
  color: var(--c-text);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

/* —— 推理过程面板 —— */
.reason-panel {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  margin-top: var(--sp-2);
  padding: var(--sp-3) var(--sp-4);
  background: var(--c-reasoning-soft);
  border: 1px solid var(--c-reasoning-border);
  border-radius: var(--r-md);
}
.reason-head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
  color: var(--c-reasoning);
}
.reason-thinking {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
}
.reason-thinking-label {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
  font-size: var(--fs-xs);
  color: var(--c-text-2);
}
.reason-thinking-text {
  font-size: var(--fs-sm);
  line-height: var(--lh-base);
  color: var(--c-text-2);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.reason-steps {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.reason-step {
  display: flex;
  align-items: flex-start;
  gap: var(--sp-2);
  font-size: var(--fs-sm);
  line-height: var(--lh-base);
  color: var(--c-text);
}
.reason-step-icon {
  margin-top: 2px;
  color: var(--c-reasoning);
}
.reason-step-text {
  flex: 1;
  min-width: 0;
  overflow-wrap: anywhere;
}
.reason-evidence {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  padding-top: var(--sp-3);
  border-top: 1px dashed var(--c-reasoning-border);
}
.reason-evidence-label {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
  font-size: var(--fs-xs);
  color: var(--c-text-2);
}
.evidence-item {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
}
.evidence-head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  font-size: var(--fs-xs);
  color: var(--c-text-2);
}
.evidence-source {
  font-family: var(--font-mono);
  overflow-wrap: anywhere;
}
.evidence-text {
  font-size: var(--fs-xs);
  line-height: var(--lh-base);
  color: var(--c-text-3);
}
.reason-raw {
  margin: 0;
  padding: var(--sp-3);
  font-size: var(--fs-xs);
  line-height: var(--lh-base);
  color: var(--c-text-2);
  background: var(--c-surface);
  border-radius: var(--r-sm);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

/* ══ 系统状态 ══ */
.sys-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: var(--sp-4);
}
.card-title {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-2);
  font-size: var(--fs-md);
  font-weight: var(--fw-semibold);
  color: var(--c-text);
}
.circ-list,
.queue-rows {
  display: flex;
  flex-direction: column;
}
.circ-row,
.queue-row {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  padding: var(--sp-3) 0;
  font-size: var(--fs-sm);
  border-bottom: 1px solid var(--c-border);
}
.circ-row:last-child,
.queue-row:last-child {
  border-bottom: none;
}
.circ-name {
  flex: 1;
  min-width: 0;
  font-weight: var(--fw-medium);
  color: var(--c-text);
  overflow-wrap: anywhere;
}
.circ-fail {
  color: var(--c-text-2);
}
.queue-row {
  justify-content: space-between;
}
.queue-k {
  color: var(--c-text-2);
}
.queue-v {
  font-weight: var(--fw-medium);
  color: var(--c-text);
}
.queue-v.is-mono {
  font-family: var(--font-mono);
}

/* ══ 二次确认弹窗 ══ */
.confirm-lead {
  font-size: var(--fs-base);
  line-height: var(--lh-base);
  color: var(--c-text);
}
.confirm-list {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  margin-top: var(--sp-3);
  padding-left: var(--sp-5);
  font-size: var(--fs-sm);
  color: var(--c-text-2);
  list-style: disc;
}
.confirm-note {
  margin-top: var(--sp-3);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--c-danger-text);
}
</style>
