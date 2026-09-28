<template>
  <div class="admin-view">
    <div class="admin-header">
      <h2>🔴 管理员面板</h2>
      <div class="admin-tabs">
        <button v-for="t in tabs" :key="t.key"
                :class="['tab-btn', { active: activeTab === t.key }]"
                @click="activeTab = t.key">
          {{ t.label }}
        </button>
      </div>
    </div>

    <!-- ═══ 仪表盘 ═══ -->
    <div v-if="activeTab === 'dashboard'" class="tab-content">
      <div v-if="dashLoading" class="loading">加载中...</div>
      <div v-else-if="dashError" class="error">{{ dashError }}</div>
      <div v-else class="dashboard-grid">
        <div class="stat-card"><span class="stat-num">{{ dash.user_count }}</span><span class="stat-label">用户总数</span></div>
        <div class="stat-card"><span class="stat-num">{{ dash.file_count }}</span><span class="stat-label">文件总数</span></div>
        <div class="stat-card"><span class="stat-num">{{ dash.today_chats }}</span><span class="stat-label">今日对话</span></div>
        <div class="stat-card"><span class="stat-num">{{ dash.total_chats }}</span><span class="stat-label">总对话数</span></div>
        <div class="stat-card"><span class="stat-num">{{ (dash.total_tokens || 0).toLocaleString() }}</span><span class="stat-label">总 Token</span></div>
        <div class="stat-card"><span class="stat-num">${{ (dash.total_cost || 0).toFixed(4) }}</span><span class="stat-label">总费用</span></div>
        <div class="stat-card"><span class="stat-num">{{ dash.disk_used_mb }} MB</span><span class="stat-label">上传文件</span></div>
        <div class="stat-card"><span class="stat-num">{{ dash.chroma_db_mb }} MB</span><span class="stat-label">向量库</span></div>
      </div>
    </div>

    <!-- ═══ 用户管理 ═══ -->
    <div v-if="activeTab === 'users'" class="tab-content">
      <div v-if="userMsg" :class="['msg', userMsgType]">{{ userMsg }}</div>
      <table class="data-table" v-if="users.length">
        <thead>
          <tr><th>ID</th><th>用户名</th><th>角色</th><th>文件数</th><th>对话数</th><th>注册时间</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="u in users" :key="u.id">
            <td>{{ u.id }}</td>
            <td><strong>{{ u.username }}</strong></td>
            <td>
              <span :class="['role-badge', u.role === 'admin' ? 'role-admin' : 'role-user']">
                {{ u.role === 'admin' ? '管理员' : '用户' }}
              </span>
            </td>
            <td>{{ u.file_count }}</td>
            <td>{{ u.chat_count }}</td>
            <td>{{ fmtDate(u.created_at) }}</td>
            <td class="actions">
              <button class="btn btn-xs" @click="toggleRole(u)"
                      :disabled="u.id === auth.ownerId">
                {{ u.role === 'admin' ? '降为用户' : '升管理员' }}
              </button>
              <button class="btn btn-xs btn-danger" @click="delUser(u)"
                      :disabled="u.id === auth.ownerId">
                删除
              </button>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-else class="empty">暂无用户数据</div>
    </div>

    <!-- ═══ 对话日志 ═══ -->
    <div v-if="activeTab === 'chatLogs'" class="tab-content">
      <div class="filter-row">
        <input v-model="chatFilterUser" class="input-xs" placeholder="按用户名筛选" @keydown.enter="loadChatLogs" />
        <button class="btn btn-sm" @click="loadChatLogs">查询</button>
      </div>
      <div v-for="log in chatLogs" :key="log.id"
           :class="['log-card', { expanded: expandedLogId === log.id }]"
           @click="toggleLog(log.id)">
        <div class="log-meta">
          <strong>{{ log.username }}</strong> · {{ log.agent_mode }} · {{ fmtDate(log.created_at) }}
          <span class="expand-hint">{{ expandedLogId === log.id ? '收起 ▲' : '展开详情 ▼' }}</span>
        </div>
        <div class="log-q"><strong>Q:</strong> {{ expandedLogId === log.id ? log.question : truncate(log.question, 150) }}</div>
        <div v-if="expandedLogId === log.id" class="log-a"><strong>A:</strong> {{ log.answer }}</div>
        <div v-else class="log-a"><strong>A:</strong> {{ truncate(log.answer, 200) }}</div>
        <!-- 展开后显示推理过程 -->
        <div v-if="expandedLogId === log.id && log.reasoning" class="log-reasoning">
          <details open>
            <summary>🧠 推理过程</summary>
            <pre>{{ log.reasoning }}</pre>
          </details>
        </div>
      </div>
      <div v-if="!chatLogs.length && !chatLoading" class="empty">暂无对话记录</div>
    </div>

    <!-- ═══ 文件管理 ═══ -->
    <div v-if="activeTab === 'files'" class="tab-content">
      <div class="filter-row">
        <input v-model="fileFilterUser" class="input-xs" placeholder="按用户名筛选" @keydown.enter="loadFiles" />
        <button class="btn btn-sm" @click="loadFiles">查询</button>
      </div>
      <table class="data-table" v-if="allFiles.length">
        <thead>
          <tr><th>ID</th><th>用户</th><th>文件名</th><th>大小</th><th>分块</th><th>上传时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="f in allFiles" :key="f.id">
            <td>{{ f.id }}</td>
            <td>{{ f.username }}</td>
            <td>📄 {{ f.filename }}</td>
            <td>{{ (f.file_size / 1024).toFixed(1) }}KB</td>
            <td>{{ f.chunk_count }}</td>
            <td>{{ fmtDate(f.created_at) }}</td>
          </tr>
        </tbody>
      </table>
      <div v-if="!allFiles.length && !fileLoading" class="empty">暂无文件</div>
    </div>

    <!-- ═══ 审计日志 ═══ -->
    <div v-if="activeTab === 'audit'" class="tab-content">
      <div class="filter-row">
        <input v-model="auditFilterUser" class="input-xs" placeholder="用户名" />
        <input v-model="auditFilterAction" class="input-xs" placeholder="操作（如 login）" />
        <button class="btn btn-sm" @click="loadAuditLogs">查询</button>
      </div>
      <table class="data-table" v-if="auditLogs.length">
        <thead>
          <tr><th>时间</th><th>用户</th><th>操作</th><th>IP</th><th>耗时</th><th>状态</th></tr>
        </thead>
        <tbody>
          <tr v-for="a in auditLogs" :key="a.id">
            <td class="time-cell">{{ fmtDate(a.created_at) }}</td>
            <td>{{ a.username || '-' }}</td>
            <td class="action-cell">{{ a.action }}</td>
            <td>{{ a.ip_address || '-' }}</td>
            <td>{{ a.duration_ms ? a.duration_ms + 'ms' : '-' }}</td>
            <td><span :class="a.status === 'success' ? 'text-green' : 'text-red'">{{ a.status }}</span></td>
          </tr>
        </tbody>
      </table>
      <div v-if="!auditLogs.length && !auditLoading" class="empty">暂无审计日志</div>
    </div>

    <!-- ═══ 系统状态 ═══ -->
    <div v-if="activeTab === 'system'" class="tab-content">
      <div class="section">
        <h3>🔌 熔断器</h3>
        <button class="btn btn-sm" @click="loadCircuitStatus">刷新</button>
        <div v-if="circuits.length" class="mt-8">
          <div v-for="c in circuits" :key="c.name" class="status-row">
            <span class="status-name">{{ c.name }}</span>
            <span :class="['status-badge', c.state]">{{ c.state }}</span>
            <span>失败: {{ c.failure_count }}</span>
          </div>
        </div>
      </div>
      <div class="section">
        <h3>📨 异步队列</h3>
        <button class="btn btn-sm" @click="loadQueueStatus">刷新</button>
        <div v-if="queueStatus" class="mt-8">
          <div>队列积压: <strong>{{ queueStatus.queue_size }}</strong></div>
          <div>后端: <strong>{{ backendLabel(queueStatus.backend) }}</strong></div>
          <div>健康: {{ queueStatus.healthy ? '✅ 正常' : '❌ 异常' }}</div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, watch } from 'vue'
import { useAuthStore } from '../stores/auth.js'
import {
  getDashboard, getUsers, updateUserRole, deleteUser,
  getChatLogs, getFiles, getAuditLogs,
  getCircuitStatus, getQueueStatus,
} from '../api/admin.js'

const auth = useAuthStore()

const tabs = [
  { key: 'dashboard', label: '📊 仪表盘' },
  { key: 'users', label: '👥 用户' },
  { key: 'chatLogs', label: '💬 对话' },
  { key: 'files', label: '📁 文件' },
  { key: 'audit', label: '📋 审计' },
  { key: 'system', label: '⚙️ 系统' },
]
const activeTab = ref('dashboard')

// —— 仪表盘 ——
const dash = ref({})
const dashLoading = ref(false)
const dashError = ref('')

async function loadDashboard() {
  dashLoading.value = true; dashError.value = ''
  try {
    dash.value = await getDashboard()
  } catch (e) { dashError.value = e.message }
  finally { dashLoading.value = false }
}

// —— 用户 ——
const users = ref([])
const userMsg = ref(''); const userMsgType = ref('info')

async function loadUsers() {
  try {
    const res = await getUsers()
    users.value = res.users || []
  } catch (e) { userMsg.value = e.message; userMsgType.value = 'error' }
}

async function toggleRole(u) {
  const newRole = u.role === 'admin' ? 'user' : 'admin'
  if (!confirm(`确定将 ${u.username} 的角色改为 ${newRole}？`)) return
  try {
    const res = await updateUserRole(u.id, newRole)
    if (res.success) { userMsg.value = res.message; userMsgType.value = 'success'; await loadUsers() }
    else { userMsg.value = res.message || res.detail; userMsgType.value = 'error' }
  } catch (e) { userMsg.value = e.message; userMsgType.value = 'error' }
}

async function delUser(u) {
  if (!confirm(`确定删除用户 ${u.username}？\n\n会同时删除其：\n- 所有对话记录\n- 已上传的文件和向量数据\n- Token 统计\n- 审计日志\n\n此操作不可恢复！`)) return
  try {
    const res = await deleteUser(u.id)
    if (res.success) { userMsg.value = res.message; userMsgType.value = 'success'; await loadUsers() }
    else { userMsg.value = res.message || res.detail; userMsgType.value = 'error' }
  } catch (e) { userMsg.value = e.message; userMsgType.value = 'error' }
}

// —— 对话日志 ——
const chatLogs = ref([])
const chatLoading = ref(false)
const chatFilterUser = ref('')
const expandedLogId = ref(null)  // 当前展开的对话 ID

function toggleLog(id) {
  expandedLogId.value = expandedLogId.value === id ? null : id
}

function truncate(text, max) {
  if (!text) return ''
  return text.length > max ? text.slice(0, max) + '...' : text
}

async function loadChatLogs() {
  chatLoading.value = true
  try {
    const res = await getChatLogs(50, 0, chatFilterUser.value)
    chatLogs.value = res.logs || []
  } catch (e) { /* ignore */ }
  finally { chatLoading.value = false }
}

// —— 文件 ——
const allFiles = ref([])
const fileLoading = ref(false)
const fileFilterUser = ref('')

async function loadFiles() {
  fileLoading.value = true
  try {
    const res = await getFiles(50, 0, fileFilterUser.value)
    allFiles.value = res.files || []
  } catch (e) { /* ignore */ }
  finally { fileLoading.value = false }
}

// —— 审计日志 ——
const auditLogs = ref([])
const auditLoading = ref(false)
const auditFilterUser = ref('')
const auditFilterAction = ref('')

async function loadAuditLogs() {
  auditLoading.value = true
  try {
    const res = await getAuditLogs(50, auditFilterAction.value, auditFilterUser.value)
    auditLogs.value = res.logs || []
  } catch (e) { /* ignore */ }
  finally { auditLoading.value = false }
}

// —— 系统状态 ——
const circuits = ref([])
const queueStatus = ref(null)

async function loadCircuitStatus() {
  try {
    const res = await getCircuitStatus()
    circuits.value = res.circuits || []
  } catch (e) { /* ignore */ }
}

async function loadQueueStatus() {
  try {
    queueStatus.value = await getQueueStatus()
  } catch (e) { /* ignore */ }
}

/** 队列后端名称：进程内线程池 / Redis(RQ) */
function backendLabel(backend) {
  return {
    thread_pool: '进程内线程池',
    rq: 'Redis (RQ)',
  }[backend] || backend || '未知'
}

function fmtDate(d) {
  if (!d) return ''
  return String(d).slice(0, 19)
}

// 切换标签页时自动加载对应数据
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

onMounted(() => {
  loadDashboard()
})
</script>

<style scoped>
.admin-view { max-width: 1100px; margin: 0 auto; padding: 16px; }
.admin-header { margin-bottom: 16px; }
.admin-header h2 { margin: 0 0 12px; }
.admin-tabs { display: flex; gap: 6px; flex-wrap: wrap; }
.tab-btn {
  padding: 6px 14px; border: 1px solid var(--border); border-radius: 6px;
  background: #fff; cursor: pointer; font-size: 13px; transition: all 0.2s;
}
.tab-btn:hover { background: #f0f4ff; }
.tab-btn.active { background: #4f46e5; color: #fff; border-color: #4f46e5; }
.tab-content { min-height: 300px; }

/* 仪表盘 */
.dashboard-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 12px; }
.stat-card {
  background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 10px;
  padding: 18px 16px; text-align: center;
}
.stat-num { display: block; font-size: 24px; font-weight: 700; color: #4f46e5; margin-bottom: 4px; }
.stat-label { font-size: 13px; color: #64748b; }

/* 表格 */
.data-table { width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 12px; }
.data-table th { text-align: left; padding: 8px 10px; border-bottom: 2px solid #e0e0e0; color: #555; font-weight: 600; white-space: nowrap; }
.data-table td { padding: 8px 10px; border-bottom: 1px solid #f0f0f0; }
.data-table tbody tr:hover { background: #f8f9fa; }
.actions { white-space: nowrap; display: flex; gap: 4px; }

/* 角色标签 */
.role-badge { font-size: 11px; padding: 2px 8px; border-radius: 10px; }
.role-admin { background: #fef2f2; color: #dc2626; }
.role-user { background: #f0fdf4; color: #16a34a; }

/* 日志卡片 */
.log-card {
  border: 1px solid #e2e8f0; border-radius: 8px; padding: 12px; margin-bottom: 8px; background: #fff;
  cursor: pointer; transition: border-color 0.2s, box-shadow 0.2s;
}
.log-card:hover { border-color: #4f46e5; box-shadow: 0 2px 8px rgba(79,70,229,0.1); }
.log-card.expanded { border-color: #4f46e5; background: #fafbff; }
.log-meta { font-size: 12px; color: #888; margin-bottom: 6px; display: flex; justify-content: space-between; align-items: center; }
.expand-hint { font-size: 11px; color: #4f46e5; }
.log-q, .log-a { font-size: 13px; margin-bottom: 4px; line-height: 1.6; white-space: pre-wrap; }
.log-a { color: #555; }
.log-reasoning { margin-top: 10px; padding: 10px; background: #f8fafc; border-radius: 6px; border: 1px dashed #e0e0e0; }
.log-reasoning summary { font-size: 13px; font-weight: 600; cursor: pointer; color: #4f46e5; }
.log-reasoning pre { margin: 8px 0 0; font-size: 12px; color: #555; white-space: pre-wrap; line-height: 1.6; font-family: inherit; }

/* 筛选 */
.filter-row { display: flex; gap: 8px; margin-bottom: 12px; align-items: center; }
.input-xs { padding: 4px 10px; border: 1px solid #d0d0d0; border-radius: 6px; font-size: 13px; width: 160px; }

/* 系统状态 */
.section { margin-bottom: 20px; }
.section h3 { margin: 0 0 8px; font-size: 15px; }
.status-row { display: flex; gap: 16px; align-items: center; padding: 8px 0; font-size: 14px; }
.status-name { font-weight: 600; min-width: 160px; }
.status-badge { font-size: 12px; padding: 2px 10px; border-radius: 10px; }
.status-badge.closed { background: #dcfce7; color: #16a34a; }
.status-badge.open { background: #fef2f2; color: #dc2626; }
.status-badge.half_open { background: #fef9c3; color: #a16207; }

/* 通用 */
.loading, .empty { text-align: center; padding: 40px; color: #888; }
.error { text-align: center; padding: 40px; color: #dc2626; }
.msg { padding: 8px 14px; border-radius: 8px; margin-bottom: 12px; font-size: 13px; }
.msg.success { background: #dcfce7; color: #16a34a; }
.msg.error { background: #fef2f2; color: #dc2626; }
.msg.info { background: #eff6ff; color: #2563eb; }
.btn-xs { padding: 3px 10px; font-size: 12px; }
.text-green { color: #16a34a; }
.text-red { color: #dc2626; }
.mt-8 { margin-top: 8px; }
.time-cell { font-size: 12px; color: #888; white-space: nowrap; }
.action-cell { font-size: 12px; word-break: break-all; }
</style>
