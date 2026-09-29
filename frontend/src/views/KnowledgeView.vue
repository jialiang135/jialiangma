<template>
  <div class="page">
    <!-- ── 页头：一眼看清"这是什么、有多少" ── -->
    <header class="page-head">
      <div class="head-text">
        <h1 class="page-title">知识库</h1>
        <p class="page-desc">上传文档入库，智能问答即可引用其中的内容。</p>
      </div>
      <div v-if="auth.isLoggedIn" class="head-meta">
        <span class="meta-item">
          <UiIcon name="file" :size="15" />
          {{ formatTokens(totalFiles) }} 个文件
        </span>
        <span class="meta-item">
          <UiIcon name="layers" :size="15" />
          {{ formatTokens(totalChunks) }} 个向量块
        </span>
      </div>
    </header>

    <!-- ── 未登录 ── -->
    <UiAlert v-if="!auth.isLoggedIn" tone="warning">
      请先登录后再上传和管理知识库文件。
    </UiAlert>

    <template v-else>
      <!-- ── 主操作：上传区（用强调底色与列表卡片拉开层级） ── -->
      <section v-if="auth.isAdmin" class="upload-panel">
        <div class="upload-head">
          <UiIcon name="upload" :size="18" />
          <h2 class="upload-title">上传文档</h2>
        </div>

        <div class="upload-controls">
          <input
            ref="fileInput"
            class="visually-hidden"
            type="file"
            multiple
            accept=".pdf,.docx,.xlsx,.txt,.md,.py,.json,.zip,.png,.jpg"
            @change="onPickFiles"
          />
          <UiButton variant="secondary" :disabled="uploading" @click="fileInput?.click()">
            <template #icon><UiIcon name="file" :size="16" /></template>
            选择文件
          </UiButton>
          <span class="picked-label" :class="{ 'is-empty': !pickedFiles.length }">
            {{ pickedLabel }}
          </span>
          <UiButton
            variant="primary"
            :loading="uploading"
            :disabled="!pickedFiles.length"
            @click="doUpload"
          >
            上传并入库
          </UiButton>
        </div>

        <p class="upload-hint">
          支持 PDF、Word、Excel、TXT、Markdown、代码文件、图片（OCR）与 ZIP；相同文件会自动跳过。
        </p>

        <!-- 逐文件处理进度 -->
        <ul v-if="uploadTasks.length" class="progress-list" aria-live="polite">
          <li v-for="t in uploadTasks" :key="t.task_id" class="progress-item">
            <div class="progress-head">
              <span class="progress-name" :title="t.filename">{{ t.filename }}</span>
              <UiBadge :tone="statusMeta(t.status).tone" dot>{{ statusMeta(t.status).label }}</UiBadge>
            </div>
            <UiProgress
              :value="t.progress || 0"
              :tone="progressTone(t.status)"
              :active="isRunning(t.status)"
              :label="`${t.filename} 处理进度`"
            />
            <p v-if="t.error" class="progress-error">{{ t.error }}</p>
          </li>
        </ul>
      </section>

      <!-- ── 主体内容：文件列表 ── -->
      <UiCard>
        <template #header>
          <div class="list-head">
            <div class="list-head-title">
              <UiIcon name="book" :size="18" />
              <h2>知识库文件</h2>
              <UiBadge tone="neutral" mono>{{ files.length }}</UiBadge>
            </div>
            <UiButton
              size="sm"
              variant="secondary"
              :loading="filesLoading"
              @click="refreshFiles()"
            >
              <template #icon><UiIcon name="refresh" :size="15" /></template>
              刷新
            </UiButton>
          </div>
        </template>

        <UiTable
          :columns="columns"
          :rows="files"
          row-key="id"
          :loading="filesLoading"
          empty-icon="book"
          empty-title="知识库为空"
          empty-description="上传文档后，这里会列出已入库的文件。"
        >
          <template #cell-id="{ value }">
            <span class="cell-id">#{{ value }}</span>
          </template>

          <template #cell-filename="{ value }">
            <span class="cell-file">
              <UiIcon name="file" :size="15" />
              <span class="cell-file-name break-anywhere">{{ value }}</span>
            </span>
          </template>

          <template #cell-file_size="{ value }">
            {{ formatSize(value) }}
          </template>

          <template #cell-chunk_count="{ value }">
            {{ formatTokens(value) }}
          </template>

          <template #cell-created_at="{ value }">
            <span :title="formatDateTime(value)">{{ formatRelativeTime(value) }}</span>
          </template>

          <template #cell-actions="{ row }">
            <UiButton size="sm" variant="danger" @click="askDelete(row)">删除</UiButton>
          </template>

          <template #empty-action>
            <UiButton v-if="auth.isAdmin" variant="primary" @click="fileInput?.click()">
              <template #icon><UiIcon name="upload" :size="16" /></template>
              选择文件上传
            </UiButton>
          </template>
        </UiTable>
      </UiCard>

      <!-- ── 维护与破坏性操作：用独立底色与普通区域拉开差距 ── -->
      <section v-if="auth.isAdmin" class="maintenance">
        <div class="maintenance-text">
          <h2 class="maintenance-title">维护操作</h2>
          <p class="maintenance-desc">
            重建索引会重新解析全部文档并重建向量（可能耗时数分钟以上）；清空知识库会删除全部数据，不可恢复。
          </p>
        </div>

        <UiAlert v-if="rebuilding" tone="info" hide-icon>
          <span class="rebuild-line">
            <UiSpinner :size="14" label="重建中" />
            正在重建向量索引… {{ rebuildProgress }}%
          </span>
          <!-- 重建最长可能跑 30 分钟，光有百分比不够，给条进度条 -->
          <UiProgress :value="rebuildProgress" active label="重建进度" class="rebuild-bar" />
        </UiAlert>

        <div class="maintenance-actions">
          <UiButton variant="secondary" :loading="rebuilding" @click="doRebuild">
            <template #icon><UiIcon name="refresh" :size="16" /></template>
            重建向量索引
          </UiButton>
          <UiButton variant="danger" @click="askClear">
            <template #icon><UiIcon name="trash" :size="16" /></template>
            清空知识库
          </UiButton>
        </div>
      </section>
    </template>

    <!-- ── 二次确认（破坏性操作统一走这里，不用原生 confirm） ── -->
    <UiModal
      :open="!!confirm"
      :title="confirm?.title"
      size="sm"
      :close-on-overlay="false"
      @close="closeConfirm"
    >
      <p class="confirm-text">{{ confirm?.body }}</p>
      <template #footer>
        <UiButton variant="ghost" :disabled="confirmPending" @click="closeConfirm">取消</UiButton>
        <UiButton variant="danger" :loading="confirmPending" @click="runConfirm">
          {{ confirm?.confirmLabel }}
        </UiButton>
      </template>
    </UiModal>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { getKbFiles, uploadKbFiles, deleteKbFile, clearKb, rebuildKb, getUploadStatus } from '../api/kb.js'
import { useAuthStore } from '../stores/auth.js'
import { useToast } from '../composables/useToast.js'
import { usePolling } from '../composables/usePolling.js'
import { useAsyncData } from '../composables/useAsyncData.js'
import {
  formatSize,
  formatTokens,
  formatDateTime,
  formatRelativeTime,
} from '../utils/format.js'
import UiButton from '../components/ui/UiButton.vue'
import UiCard from '../components/ui/UiCard.vue'
import UiBadge from '../components/ui/UiBadge.vue'
import UiAlert from '../components/ui/UiAlert.vue'
import UiTable from '../components/ui/UiTable.vue'
import UiModal from '../components/ui/UiModal.vue'
import UiProgress from '../components/ui/UiProgress.vue'
import UiIcon from '../components/ui/UiIcon.vue'
import UiSpinner from '../components/ui/UiSpinner.vue'

const auth = useAuthStore()

/**
 * 处理状态 → 进度条色调。
 * 抽出来放一处，模板里就不用写四层嵌套三元表达式。
 */
function progressTone(status) {
  if (status === 'done') return 'success'
  if (status === 'failed') return 'danger'
  if (status === 'skipped') return 'neutral'
  if (status === 'partial') return 'warning'
  return 'accent'
}

const isRunning = (status) => status === 'pending' || status === 'processing'
const toast = useToast()

// ── 文件列表：走 useAsyncData（并发保护 + 错误必落到 error，不再静默吞掉） ──
const { data: kbStats, loading: filesLoading, error: filesError, run: loadFiles } =
  useAsyncData(() => getKbFiles())

const files = computed(() => kbStats.value?.files ?? [])
const totalFiles = computed(() => kbStats.value?.total_files ?? files.value.length)
const totalChunks = computed(
  () => kbStats.value?.total_chunks ?? files.value.reduce((sum, f) => sum + (f.chunk_count || 0), 0),
)

const columns = computed(() => {
  const cols = [
    { key: 'id', label: 'ID', width: '76px', mono: true },
    { key: 'filename', label: '文件名' },
    { key: 'file_size', label: '大小', width: '92px' },
    { key: 'chunk_count', label: '向量块', width: '92px', align: 'right', mono: true },
    { key: 'created_at', label: '上传时间', width: '128px' },
  ]
  if (auth.isAdmin) cols.push({ key: 'actions', label: '操作', width: '88px', align: 'right' })
  return cols
})

// ── 响应体归一化：2xx 是 {success,message,data}，非 2xx 是 {detail} ──
function readResult(res, fallback) {
  if (!res || typeof res !== 'object') return { ok: false, message: fallback }
  if (res.success) return { ok: true, message: res.message || '操作成功' }
  const d = res.detail
  const message =
    (typeof d === 'string' && d) ||
    (d && typeof d === 'object' && d.message) ||
    res.error ||
    fallback
  return { ok: false, message }
}

function ensureLoggedIn() {
  if (!auth.isLoggedIn) {
    toast.error('请先登录')
    return false
  }
  return true
}

// ── 刷新文件列表 ──
async function refreshFiles({ silent = false } = {}) {
  if (!ensureLoggedIn()) return
  const res = await loadFiles()
  if (res && !silent) toast.success(`已刷新：${(res.files || []).length} 个文件`)
  else if (!res) toast.error(`加载失败：${filesError.value || '未知错误'}`)
}

// ── 状态到徽章的映射（纯文本标签，无 emoji） ──
const STATUS_META = {
  pending: { label: '排队中', tone: 'warning' },
  processing: { label: '处理中', tone: 'info' },
  done: { label: '完成', tone: 'success' },
  // 后端新增的"部分完成"：有文件成功、也有文件失败/为空，知识库可用但不完整。
  // ⚠️ 前端必须认识这个状态 —— 否则 usePolling 永远等不到终态，
  //    会一直轮询到 30 分钟上限，然后显示"重建仍在进行中"。
  partial: { label: '部分完成', tone: 'warning' },
  failed: { label: '失败', tone: 'danger' },
  skipped: { label: '已跳过', tone: 'neutral' },
}
const statusMeta = (s) => STATUS_META[s] || { label: s || '未知', tone: 'neutral' }
const isTerminal = (s) =>
  s === 'done' || s === 'failed' || s === 'skipped' || s === 'partial'

// ── 上传 ──
const fileInput = ref(null)
const pickedFiles = ref([])
const uploading = ref(false)
const uploadTasks = ref([])

const pickedLabel = computed(() => {
  const n = pickedFiles.value.length
  if (n === 0) return '未选择文件'
  if (n === 1) return pickedFiles.value[0].name
  return `已选择 ${n} 个文件`
})

function onPickFiles(e) {
  pickedFiles.value = Array.from(e.target.files || [])
}

function clearPick() {
  pickedFiles.value = []
  if (fileInput.value) fileInput.value.value = ''
}

// 一组上传任务共用**一个**轮询循环：每个 tick 把所有未终态任务一起查一遍。
// 新任务只要推进 uploadTasks，下一 tick 就会被自动带上，无需另开循环。
const uploadPoll = usePolling()
const MAX_TASK_ERRORS = 5

function applyUploadTick(results) {
  let needRefresh = false
  for (const r of results) {
    const t = r.task
    if (r.error) {
      if (t.errors < MAX_TASK_ERRORS) continue // 偶发抖动：保持原状态，下一 tick 重试
      t.status = 'failed'
      t.error = r.error?.message || '状态查询失败'
    } else {
      const res = r.res
      if (res.filename) t.filename = res.filename // 用服务端回传的真实文件名（异步任务无法靠下标对应）
      t.status = res.status
      if (typeof res.progress === 'number') t.progress = res.progress
      t.error = res.error || ''
    }
    if (isTerminal(t.status) && !t.settled) {
      t.settled = true
      needRefresh = true
      // 终态后短暂保留，让用户看到结果，随后回收
      setTimeout(() => {
        const i = uploadTasks.value.findIndex((x) => x.task_id === t.task_id)
        if (i >= 0) uploadTasks.value.splice(i, 1)
      }, 3000)
    }
  }
  if (needRefresh) refreshFiles({ silent: true })
}

async function pollUploadTasks() {
  if (uploadPoll.active.value) return // 已在跑，新任务会被下一 tick 带上
  const settled = await uploadPoll.start(
    async () => {
      const pending = uploadTasks.value.filter((t) => !isTerminal(t.status))
      if (pending.length === 0) return []
      return Promise.all(
        pending.map(async (t) => {
          try {
            const res = await getUploadStatus(t.task_id)
            t.errors = 0
            return { task: t, res }
          } catch (e) {
            t.errors = (t.errors || 0) + 1
            return { task: t, error: e }
          }
        }),
      )
    },
    {
      interval: 1500,
      maxAttempts: 240, // 约 6 分钟
      // 只以任务自身的终态为准：偶发查询失败不算结束，否则单个抖动就会让循环提前退出
      isDone: (rs) => rs.length === 0 || rs.every((r) => isTerminal(r.task.status)),
      onTick: applyUploadTick,
    },
  )
  if (settled === null) {
    // 超时：把仍未结束的任务收敛掉，避免一直停在"处理中"
    for (const t of uploadTasks.value) {
      if (!isTerminal(t.status)) {
        t.status = 'failed'
        t.error = '处理超时'
      }
    }
  }
}

async function doUpload() {
  if (!ensureLoggedIn()) return
  const picked = Array.from(fileInput.value?.files || [])
  if (!picked.length) {
    toast.error('请先选择文件')
    return
  }
  uploading.value = true
  try {
    const res = await uploadKbFiles(picked)
    if (!res?.success) {
      toast.error(readResult(res, '上传失败').message)
      return
    }
    const taskIds = res.data?.task_ids || []
    if (taskIds.length) {
      for (const taskId of taskIds) {
        uploadTasks.value.push({
          task_id: taskId,
          filename: '等待处理…',
          status: 'pending',
          progress: 0,
          error: '',
          errors: 0,
          settled: false,
        })
      }
      toast.info(res.message || `已提交 ${taskIds.length} 个文件`)
      pollUploadTasks()
    } else {
      // 全部为重复文件：没有任务可跟踪，直接提示并刷新
      toast.info(res.message || '已提交')
      await refreshFiles({ silent: true })
    }
    clearPick()
  } catch (e) {
    toast.error(e.message)
  } finally {
    uploading.value = false
  }
}

// ── 重建向量索引：长轮询（上限约 30 分钟），进度可视化 ──
const rebuilding = ref(false)
const rebuildProgress = ref(0)
const rebuildPoll = usePolling()

async function doRebuild() {
  if (!ensureLoggedIn() || rebuilding.value) return
  rebuilding.value = true
  rebuildProgress.value = 0
  let consecutiveErrors = 0
  try {
    const res = await rebuildKb()
    const taskId = res?.data?.task_id
    if (!res?.success || !taskId) {
      toast.error(readResult(res, '重建任务提交失败').message)
      return
    }
    toast.info('重建任务已提交，正在处理')

    const final = await rebuildPoll.start(
      () =>
        getUploadStatus(taskId).catch((e) => {
          // 短暂抖动继续重试；连续失败多次说明任务已不可用，抛出终止轮询
          if (++consecutiveErrors >= MAX_TASK_ERRORS) throw e
          return { status: 'processing' }
        }),
      {
        interval: 1500,
        maxAttempts: 1200, // 约 30 分钟
        isDone: (st) =>
          st?.status === 'done' || st?.status === 'failed' || st?.status === 'partial',
        onTick: (st) => {
          if (typeof st?.progress === 'number') rebuildProgress.value = st.progress
        },
      },
    )

    if (final === null) {
      toast.warning('重建仍在进行中，可稍后刷新页面查看结果')
    } else if (final.status === 'done') {
      toast.success(`重建完成（${final.chunk_count ?? 0} 个向量块）`)
      await refreshFiles({ silent: true })
    } else if (final.status === 'partial') {
      // 部分成功：知识库能用但不完整。必须让用户看到是**哪些文件**出了问题，
      // 否则他会以为重建干净地成功了（这正是"静默失效"要消灭的那种情况）。
      toast.warning(
        `重建部分完成（${final.chunk_count ?? 0} 个向量块）：${final.error || '部分文件未能处理'}`,
        8000,
      )
      await refreshFiles({ silent: true })
    } else {
      toast.error(`重建失败：${final.error || '未知错误'}`)
    }
  } catch (e) {
    toast.error(e.message)
  } finally {
    rebuilding.value = false
  }
}

// ── 删除文件 / 清空知识库（均走二次确认） ──
async function doDelete(row) {
  if (!ensureLoggedIn()) return
  try {
    const res = await deleteKbFile(row.id)
    const r = readResult(res, '删除失败')
    r.ok ? toast.success(r.message) : toast.error(r.message)
    await refreshFiles({ silent: true })
  } catch (e) {
    toast.error(e.message)
  }
}

async function doClear() {
  if (!ensureLoggedIn()) return
  try {
    const res = await clearKb()
    const r = readResult(res, '清空失败')
    r.ok ? toast.success(r.message) : toast.error(r.message)
    // 知识库已清空：上传任务的跟踪对象不复存在，手动停掉轮询并清掉占位
    uploadPoll.stop()
    uploadTasks.value = []
    await refreshFiles({ silent: true })
  } catch (e) {
    toast.error(e.message)
  }
}

// ── 确认框（一个通用宿主，删除/清空复用） ──
const confirm = ref(null)
const confirmPending = ref(false)

function askDelete(row) {
  confirm.value = {
    title: '删除文件',
    body: `确定删除「${row.filename}」？该文件的向量数据会一并移除，操作不可恢复。`,
    confirmLabel: '删除',
    onConfirm: () => doDelete(row),
  }
}

function askClear() {
  confirm.value = {
    title: '清空知识库',
    body: '将删除全部文件与向量数据，此操作不可恢复。确定继续？',
    confirmLabel: '清空',
    onConfirm: doClear,
  }
}

function closeConfirm() {
  if (!confirmPending.value) confirm.value = null
}

async function runConfirm() {
  const c = confirm.value
  if (!c) return
  confirmPending.value = true
  try {
    await c.onConfirm()
  } finally {
    confirmPending.value = false
    confirm.value = null
  }
}

onMounted(() => {
  if (auth.isLoggedIn) loadFiles()
})

// 登录态从"未登录"变为已登录（如顶部登录栏登录）时自动补一次加载
watch(
  () => auth.isLoggedIn,
  (isIn) => {
    if (isIn) loadFiles()
  },
)
</script>

<style scoped>
/* ── 页面骨架 ── */
.page {
  display: flex;
  flex-direction: column;
  gap: var(--sp-6);
  width: 100%;
  max-width: var(--content-max);
  margin-inline: auto;
}

/* ── 页头 ── */
.page-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: var(--sp-4);
  flex-wrap: wrap;
}
.page-title {
  font-size: var(--fs-xl);
  font-weight: var(--fw-semibold);
  color: var(--c-text);
}
.page-desc {
  margin-top: var(--sp-1);
  font-size: var(--fs-sm);
  color: var(--c-text-2);
}
.head-meta {
  display: flex;
  align-items: center;
  gap: var(--sp-4);
  font-size: var(--fs-sm);
  color: var(--c-text-2);
}
.meta-item {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
}

/* ── 上传区：主操作，用强调底与列表卡片区分权重 ── */
.upload-panel {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  padding: var(--sp-5);
  background: var(--c-accent-soft);
  border: 1px solid var(--c-accent-border);
  border-radius: var(--r-lg);
}
.upload-head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  color: var(--c-accent-text);
}
.upload-title {
  font-size: var(--fs-md);
  font-weight: var(--fw-semibold);
}
.upload-controls {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  flex-wrap: wrap;
}
.picked-label {
  flex: 1;
  min-width: 0;
  font-size: var(--fs-sm);
  color: var(--c-text-2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.picked-label.is-empty {
  color: var(--c-text-3);
}
.upload-hint {
  font-size: var(--fs-xs);
  color: var(--c-text-2);
  line-height: var(--lh-base);
}

/* 隐藏原生文件输入（用于承接"选择文件"按钮的点击） */
.visually-hidden {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}

/* ── 上传进度 ── */
.progress-list {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  margin-top: var(--sp-1);
}
.progress-item {
  padding: var(--sp-3);
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--r-md);
}
.progress-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-3);
  margin-bottom: var(--sp-2);
}
.progress-name {
  min-width: 0;
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--c-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
/* 进度条已抽成 UiProgress 组件（原先这个页面和评测页各手写一套） */

.rebuild-bar {
  margin-top: var(--sp-2);
}
.progress-error {
  margin-top: var(--sp-2);
  font-size: var(--fs-xs);
  color: var(--c-danger-text);
}

/* ── 文件列表卡片 ── */
.list-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-3);
  width: 100%;
}
.list-head-title {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  font-size: var(--fs-md);
  font-weight: var(--fw-semibold);
  color: var(--c-text);
}
.cell-id {
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  color: var(--c-text-2);
}
.cell-file {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-2);
  color: var(--c-text-2);
}
.cell-file-name {
  color: var(--c-text);
}

/* ── 维护 / 破坏性操作区：独立底色，与普通区域拉开差距 ── */
.maintenance {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  padding: var(--sp-5);
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-left: 3px solid var(--c-danger-border);
  border-radius: var(--r-lg);
}
.maintenance-title {
  font-size: var(--fs-md);
  font-weight: var(--fw-semibold);
  color: var(--c-text);
}
.maintenance-desc {
  margin-top: var(--sp-1);
  font-size: var(--fs-xs);
  color: var(--c-text-2);
  line-height: var(--lh-base);
}
.rebuild-line {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-2);
}
.maintenance-actions {
  display: flex;
  gap: var(--sp-2);
  flex-wrap: wrap;
}

.confirm-text {
  font-size: var(--fs-sm);
  color: var(--c-text-2);
  line-height: var(--lh-base);
}

/* ── 窄屏收拢 ── */
@media (max-width: 768px) {
  .page-head {
    align-items: flex-start;
  }
  .head-meta {
    gap: var(--sp-3);
  }
  .upload-controls {
    align-items: stretch;
  }
  .picked-label {
    flex-basis: 100%;
  }
  .maintenance-actions {
    flex-direction: column;
  }
}
</style>
