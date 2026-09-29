<template>
  <div class="kb-view">
    <div class="kb-upload-area" v-if="auth.isAdmin">
      <h3>📤 上传文档</h3>
      <div class="upload-row">
        <input type="file" ref="fileInput" multiple accept=".pdf,.docx,.xlsx,.txt,.md,.py,.json,.zip,.png,.jpg" />
        <button class="btn btn-primary" :disabled="uploading" @click="doUpload">
          {{ uploading ? '提交中...' : '上传并入库' }}
        </button>
      </div>
      <div class="upload-hint">
        支持: PDF、Word、Excel、TXT、Markdown、代码文件、图片(OCR)、ZIP
        <br />相同文件自动跳过，无需担心重复上传
      </div>

      <!-- 上传进度列表 -->
      <div v-if="uploadTasks.length > 0" class="upload-progress-list">
        <div v-for="t in uploadTasks" :key="t.task_id" class="progress-item">
          <div class="progress-header">
            <span class="progress-filename">{{ t.filename }}</span>
            <span :class="['progress-status', t.status]">{{ statusLabel(t.status) }}</span>
          </div>
          <div class="progress-bar-track">
            <div :class="['progress-bar-fill', t.status]" :style="{ width: t.progress + '%' }"></div>
          </div>
          <div v-if="t.error" class="progress-error">{{ t.error }}</div>
        </div>
      </div>
    </div>

    <div class="kb-toolbar">
      <!-- 加加载态与结果提示：原先点击后毫无反馈，
           数据没变时用户不知道是"刷新成功了"还是"按钮没生效" -->
      <button class="btn btn-sm" :disabled="refreshing" @click="refreshFiles">
        {{ refreshing ? '刷新中...' : '🔄 刷新文件列表' }}
      </button>
      <div class="toolbar-right" v-if="auth.isAdmin">
        <button class="btn btn-sm btn-danger" @click="doClear">🗑 清空知识库</button>
        <button class="btn btn-sm" :disabled="rebuilding" @click="doRebuild">
          {{ rebuilding ? '重建中...' : '🔧 重建向量索引' }}
        </button>
      </div>
    </div>

    <div v-if="statusMsg" :class="['status-msg', statusType]">{{ statusMsg }}</div>

    <div class="kb-file-list">
      <h3>📚 知识库文件 <span class="count-badge">{{ files.length }}</span></h3>
      <div v-if="files.length === 0" class="empty-state">📭 知识库为空，上传文档即可开始</div>
      <div v-for="f in files" :key="f.id" class="file-item">
        <div class="file-info">
          <span class="file-icon">📄</span>
          <span class="file-name">#{{ f.id }} {{ f.filename }}</span>
          <span class="file-meta">{{ formatSize(f.file_size) }} · {{ f.chunk_count }} 块 · {{ formatDate(f.created_at) }}</span>
        </div>
        <button v-if="auth.isAdmin" class="btn btn-sm btn-danger" @click="doDelete(f.id)">删除</button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { getKbFiles, uploadKbFiles, deleteKbFile, clearKb, rebuildKb, getUploadStatus } from '../api/kb.js'
import { useAuthStore } from '../stores/auth.js'

const auth = useAuthStore()
const files = ref([])
const fileInput = ref(null)
const uploading = ref(false)
const rebuilding = ref(false)
const statusMsg = ref('')
const statusType = ref('')
const uploadTasks = ref([])

function showMsg(msg, type = 'info') {
  statusMsg.value = msg
  statusType.value = type
  setTimeout(() => { statusMsg.value = '' }, 5000)
}

function statusLabel(s) {
  return { pending: '⏳ 排队中', processing: '🔄 处理中', done: '✅ 完成', failed: '❌ 失败', skipped: '⏭️ 已跳过' }[s] || s
}

async function pollTaskStatus(taskId, filename) {
  let attempts = 0
  const maxAttempts = 120  // 最多轮询 2 分钟
  const task = uploadTasks.value.find(t => t.task_id === taskId)
  if (!task) return

  const poll = async () => {
    if (attempts >= maxAttempts) {
      if (task) task.status = 'failed'; task.error = '处理超时'
      return
    }
    attempts++
    try {
      const res = await getUploadStatus(taskId)
      if (task) {
        task.status = res.status
        task.progress = res.progress
        task.error = res.error
      }
      if (res.status === 'done' || res.status === 'failed' || res.status === 'skipped') {
        await refreshFiles(true)
        // 清理已完成的任务（3 秒后移除）
        setTimeout(() => {
          const idx = uploadTasks.value.findIndex(t => t.task_id === taskId)
          if (idx >= 0) uploadTasks.value.splice(idx, 1)
        }, 3000)
        return
      }
      setTimeout(poll, 1500)  // 1.5 秒轮询一次
    } catch {
      setTimeout(poll, 2000)
    }
  }
  poll()
}

const refreshing = ref(false)

async function refreshFiles(silent = false) {
  if (!auth.isLoggedIn) { showMsg('请先登录', 'error'); return }
  refreshing.value = true
  try {
    const res = await getKbFiles()
    files.value = res.files || []
    if (!silent) showMsg(`已刷新：${files.value.length} 个文件`, 'success')
  } catch (e) {
    showMsg(`加载失败: ${e.message}`, 'error')
  } finally {
    refreshing.value = false
  }
}

async function doUpload() {
  const input = fileInput.value
  if (!input?.files?.length) { showMsg('请先选择文件', 'error'); return }
  if (!auth.isLoggedIn) { showMsg('请先登录', 'error'); return }
  uploading.value = true
  try {
    const res = await uploadKbFiles(input.files)
    if (res.success) {
      // 新异步模式：创建任务并轮询
      if (res.data?.task_ids?.length > 0) {
        for (const tid of res.data.task_ids) {
          uploadTasks.value.push({ task_id: tid, filename: '处理中...', status: 'pending', progress: 0, error: '' })
        }
        // 获取每项的文件名并开始轮询
        for (let i = 0; i < res.data.task_ids.length; i++) {
          const tid = res.data.task_ids[i]
          const fname = input.files[i]?.name || '未知文件'
          const task = uploadTasks.value.find(t => t.task_id === tid)
          if (task) task.filename = fname
          pollTaskStatus(tid, fname)
        }
        showMsg(`📨 ${res.message}`, 'info')
      } else {
        showMsg(res.message || '已提交', 'info')
        await refreshFiles(true)
      }
      input.value = ''
    } else {
      showMsg(`❌ ${res.message || '上传失败'}`, 'error')
    }
  } catch (e) {
    showMsg(`❌ ${e.message}`, 'error')
  } finally {
    uploading.value = false
  }
}

async function doDelete(fileId) {
  if (!confirm(`确定删除文件 #${fileId}？`)) return
  if (!auth.isLoggedIn) { showMsg('请先登录', 'error'); return }
  try {
    const res = await deleteKbFile(fileId)
    showMsg(res.success ? `✅ ${res.message}` : `❌ ${res.error || res.message}`, res.success ? 'success' : 'error')
    await refreshFiles(true)
  } catch (e) {
    showMsg(`❌ ${e.message}`, 'error')
  }
}

async function doClear() {
  if (!confirm('确定清空所有知识库数据？此操作不可恢复！')) return
  if (!auth.isLoggedIn) { showMsg('请先登录', 'error'); return }
  try {
    const res = await clearKb()
    showMsg(res.success ? `✅ ${res.message}` : `❌ ${res.error || res.message}`, res.success ? 'success' : 'error')
    await refreshFiles(true)
  } catch (e) {
    showMsg(`❌ ${e.message}`, 'error')
  }
}

async function doRebuild() {
  if (!auth.isLoggedIn) { showMsg('请先登录', 'error'); return }
  rebuilding.value = true
  try {
    // 后端已把重建改为后台任务（可能耗时数分钟到数小时），
    // 因此这里拿到 task_id 后轮询进度，而不是等一个长请求返回
    const res = await rebuildKb()
    const taskId = res?.data?.task_id
    if (!res.success || !taskId) {
      showMsg(`❌ ${res.error || res.message || '重建任务提交失败'}`, 'error')
      return
    }
    showMsg('🔧 重建任务已提交，正在处理…', 'info')

    const maxAttempts = 1200  // 最多轮询 30 分钟（1.5s 一次）
    for (let i = 0; i < maxAttempts; i++) {
      await new Promise(r => setTimeout(r, 1500))
      let st
      try {
        st = await getUploadStatus(taskId)
      } catch {
        continue
      }
      if (st.status === 'done') {
        showMsg(`✅ 重建完成（${st.chunk_count} 个向量块）`, 'success')
        await refreshFiles(true)
        return
      }
      if (st.status === 'failed') {
        showMsg(`❌ 重建失败: ${st.error || '未知错误'}`, 'error')
        return
      }
    }
    showMsg('⚠️ 重建仍在进行中，可稍后刷新页面查看结果', 'info')
  } catch (e) {
    showMsg(`❌ ${e.message}`, 'error')
  } finally {
    rebuilding.value = false
  }
}

function formatSize(bytes) {
  if (!bytes) return '0KB'
  return (bytes / 1024).toFixed(1) + 'KB'
}

function formatDate(d) {
  if (!d) return ''
  return String(d).slice(0, 10)
}

onMounted(() => {
  if (auth.isLoggedIn) refreshFiles()
})
</script>

<style scoped>
/* 上传进度 */
.upload-progress-list {
  margin-top: 12px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.progress-item {
  padding: 10px 12px;
  background: #f8fafc;
  border: 1px solid var(--border);
  border-radius: 8px;
}
.progress-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 6px;
  font-size: 13px;
}
.progress-filename {
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.progress-status {
  font-size: 12px;
  padding: 1px 8px;
  border-radius: 10px;
  flex-shrink: 0;
}
.progress-status.done { background: #dcfce7; color: #16a34a; }
.progress-status.processing { background: #eff6ff; color: #2563eb; }
.progress-status.pending { background: #fef9c3; color: #a16207; }
.progress-status.failed { background: #fef2f2; color: #dc2626; }
.progress-status.skipped { background: #f1f5f9; color: #64748b; }
.progress-bar-track {
  width: 100%;
  height: 6px;
  background: #e2e8f0;
  border-radius: 3px;
  overflow: hidden;
}
.progress-bar-fill {
  height: 100%;
  border-radius: 3px;
  transition: width 0.4s ease;
  background: #4f46e5;
}
.progress-bar-fill.done { background: #16a34a; }
.progress-bar-fill.failed { background: #dc2626; }
.progress-bar-fill.skipped { background: #94a3b8; }
.progress-error {
  margin-top: 4px;
  font-size: 11px;
  color: #dc2626;
}
</style>
