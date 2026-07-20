<template>
  <div class="kb-view">
    <div class="kb-upload-area" v-if="auth.isAdmin">
      <h3>📤 上传文档</h3>
      <div class="upload-row">
        <input type="file" ref="fileInput" multiple accept=".pdf,.docx,.xlsx,.txt,.md,.py,.json,.zip,.png,.jpg" />
        <button class="btn btn-primary" :disabled="uploading" @click="doUpload">
          {{ uploading ? '上传中...' : '上传并入库' }}
        </button>
      </div>
      <div class="upload-hint">
        支持: PDF、Word、Excel、TXT、Markdown、代码文件、图片(OCR)、ZIP
      </div>
    </div>

    <div class="kb-toolbar">
      <button class="btn btn-sm" @click="refreshFiles">🔄 刷新文件列表</button>
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
import { getKbFiles, uploadKbFiles, deleteKbFile, clearKb, rebuildKb } from '../api/kb.js'
import { useAuthStore } from '../stores/auth.js'

const auth = useAuthStore()
const files = ref([])
const fileInput = ref(null)
const uploading = ref(false)
const rebuilding = ref(false)
const statusMsg = ref('')
const statusType = ref('')

function showMsg(msg, type = 'info') {
  statusMsg.value = msg
  statusType.value = type
  setTimeout(() => { statusMsg.value = '' }, 5000)
}

async function refreshFiles() {
  if (!auth.isLoggedIn) { showMsg('请先登录', 'error'); return }
  try {
    const res = await getKbFiles()
    files.value = res.files || []
    showMsg(`📁 共 ${files.value.length} 个文件 · 🧩 共 ${res.total_chunks || 0} 个向量块`, 'info')
  } catch (e) {
    showMsg(`加载失败: ${e.message}`, 'error')
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
      showMsg(`✅ ${res.message}`, 'success')
      input.value = ''
      await refreshFiles()
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
    await refreshFiles()
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
    await refreshFiles()
  } catch (e) {
    showMsg(`❌ ${e.message}`, 'error')
  }
}

async function doRebuild() {
  if (!auth.isLoggedIn) { showMsg('请先登录', 'error'); return }
  rebuilding.value = true
  try {
    const res = await rebuildKb()
    showMsg(res.success ? `✅ ${res.message}` : `❌ ${res.error || res.message}`, res.success ? 'success' : 'error')
    await refreshFiles()
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

// 进入页面时自动加载文件列表
onMounted(() => {
  if (auth.isLoggedIn) refreshFiles()
})
</script>
