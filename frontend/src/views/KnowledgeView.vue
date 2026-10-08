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
            :accept="acceptAttr"
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
          支持 {{ formatSummary }}；相同文件会自动跳过。
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
            <div class="row-actions">
              <UiButton size="sm" variant="secondary" @click="openPreview(row)">查看</UiButton>
              <UiButton v-if="auth.isAdmin" size="sm" variant="danger" @click="askDelete(row)">
                删除
              </UiButton>
            </div>
          </template>

          <template #empty-action>
            <UiButton v-if="auth.isAdmin" variant="primary" @click="fileInput?.click()">
              <template #icon><UiIcon name="upload" :size="16" /></template>
              选择文件上传
            </UiButton>
          </template>
        </UiTable>
      </UiCard>

      <FilePreviewDrawer
        :open="!!previewFile"
        :file="previewFile"
        @close="previewFile = null"
      />

      <!-- ── 切块设置：知识库级参数（像 Dify 的数据集分段设置） ── -->
      <UiCard>
        <template #header>
          <div class="list-head">
            <div class="list-head-title">
              <UiIcon name="sliders" :size="18" />
              <h2>切块设置</h2>
            </div>
            <UiBadge tone="neutral">{{ isSemantic ? '结构感知' : '定长切分' }}</UiBadge>
          </div>
        </template>

        <div class="chunking-form">
          <label class="chunking-field">
            <span class="chunking-label">分段方式</span>
            <UiSelect
              v-model="chunkingForm.mode"
              :options="modeOptions"
              :disabled="!auth.isAdmin || chunkingLoading"
            />
          </label>

          <label class="chunking-field">
            <span class="chunking-label">块大小（字符）</span>
            <UiInput
              v-model="chunkingForm.chunk_size"
              type="number"
              :disabled="!auth.isAdmin || chunkingLoading"
              :placeholder="`${chunkingBounds.chunk_size_min} ~ ${chunkingBounds.chunk_size_max}`"
            />
          </label>

          <label class="chunking-field">
            <span class="chunking-label">块间重叠（字符）</span>
            <UiInput
              v-model="chunkingForm.chunk_overlap"
              type="number"
              :disabled="!auth.isAdmin || chunkingLoading"
            />
          </label>

          <label class="chunking-field">
            <span class="chunking-label">自定义分隔符（可选）</span>
            <UiInput
              v-model="chunkingForm.separatorsText"
              :disabled="!auth.isAdmin || chunkingLoading"
              placeholder="逗号分隔，\n 表示换行；留空用内置默认"
            />
          </label>
        </div>

        <!-- 这段说明必须与**实测**一致。
             原先写的是"块大小与重叠仅在定长兜底时生效"，还据此把两个输入框在
             结构感知模式下禁用 —— 实测是错的：同一个文件（08_面试问答准备.md，
             39038 字）块大小 1000→123 块、300→204 块，结构感知下**明显生效**。
             它控制的是"结构块超过 1.5 倍块大小时要不要二次切分"。
             现在没有"禁用"了，而且下面有试切预览 —— 改完立刻看得见效果。 -->
        <UiAlert v-if="isSemantic" tone="info" hide-icon>
          结构感知按文档结构切分（Markdown 标题 → 中文编号章节 → 段落）。
          块大小仍有用：结构块超过它的 1.5 倍时会被二次切分（实测同一文件，
          块大小 1000 → 123 块，300 → 204 块）；块间重叠只在二次切分与定长兜底时生效。
        </UiAlert>

        <p class="chunking-hint">{{ chunkingNote }}</p>

        <!-- ── 试切预览：改配置之前先看看会切成什么样 ──
             这一块是为了让上面的参数**可验证** —— 用户不必重建索引再肉眼比对，
             改完立刻看到"当前 123 块 → 你的配置 204 块"，以及前几块长什么样。
             实测就是靠它发现"自定义分隔符"对结构良好的文档几乎无效（123→122）。 -->
        <div v-if="auth.isAdmin" class="chunking-preview">
          <div class="preview-bar">
            <span class="chunking-label">试切预览</span>
            <UiSelect
              v-model="previewFileId"
              :options="previewFileOptions"
              :disabled="previewLoading || !files.length"
            />
            <UiButton
              variant="secondary"
              :loading="previewLoading"
              :disabled="!previewFileId"
              @click="runChunkingPreview"
            >
              按当前填写的参数试切
            </UiButton>
          </div>

          <div v-if="chunkingPreview" class="preview-result">
            <div class="preview-compare">
              <span>当前保存的配置：<b>{{ chunkingPreview.current.total_chunks }}</b> 块（平均 {{ chunkingPreview.current.avg_chars }} 字）</span>
              <span class="preview-arrow">→</span>
              <span>你填的配置：<b>{{ chunkingPreview.candidate.total_chunks }}</b> 块（平均 {{ chunkingPreview.candidate.avg_chars }} 字，最大 {{ chunkingPreview.candidate.max_chars }}）</span>
            </div>

            <UiAlert v-if="chunkingPreview.identical" tone="warning" hide-icon>
              两套配置切出来的结果**完全一样** —— 说明你改的这项对<b>这个文件</b>没有影响。
              换个文件试试，或改别的参数。
            </UiAlert>

            <div v-if="chunkingPreview.candidate.sample.length" class="preview-sample">
              <div class="preview-sample-title">前 {{ chunkingPreview.candidate.sample.length }} 块长这样：</div>
              <div v-for="c in chunkingPreview.candidate.sample" :key="c.index" class="preview-chunk">
                <span class="preview-chunk-head">
                  #{{ c.index }}<template v-if="c.page"> · 第 {{ c.page }} 页</template> · {{ c.chars }} 字
                </span>
                <pre class="preview-chunk-text">{{ c.content }}</pre>
              </div>
            </div>
          </div>
        </div>

        <div v-if="auth.isAdmin" class="chunking-actions">
          <UiButton variant="primary" :loading="chunkingSaving" @click="saveChunking">
            保存切块设置
          </UiButton>
        </div>
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
import {
  getKbFiles,
  uploadKbFiles,
  deleteKbFile,
  clearKb,
  rebuildKb,
  getUploadStatus,
  getKbChunking,
  updateKbChunking,
  previewKbChunking,
  getKbFormats,
} from '../api/kb.js'
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
import UiSelect from '../components/ui/UiSelect.vue'
import FilePreviewDrawer from '../components/kb/FilePreviewDrawer.vue'
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

/** 上传支持的格式：从后端取，别在前端手抄（曾经抄漏过 6 种，见 /kb/formats 的注释） */
const kbFormats = ref({ extensions: [], groups: [] })
const acceptAttr = computed(() =>
  kbFormats.value.extensions.length ? kbFormats.value.extensions.join(',') : undefined,
)
const formatSummary = computed(() =>
  kbFormats.value.groups.length
    ? kbFormats.value.groups.map((g) => g.label).join('、')
    : 'PDF、Word、Excel、TXT、Markdown、代码、图片（OCR）与 ZIP',
)

const files = computed(() => kbStats.value?.files ?? [])
const totalFiles = computed(() => kbStats.value?.total_files ?? files.value.length)
const totalChunks = computed(
  () => kbStats.value?.total_chunks ?? files.value.reduce((sum, f) => sum + (f.chunk_count || 0), 0),
)

/** 当前在抽屉里预览的文件（null = 关着）。只读操作，所有登录用户都能用。 */
const previewFile = ref(null)

function openPreview(row) {
  previewFile.value = row
}

const columns = computed(() => {
  const cols = [
    { key: 'id', label: 'ID', width: '76px', mono: true },
    { key: 'filename', label: '文件名' },
    { key: 'file_size', label: '大小', width: '92px' },
    { key: 'chunk_count', label: '向量块', width: '92px', align: 'right', mono: true },
    { key: 'created_at', label: '上传时间', width: '128px' },
  ]
  // 操作列对**所有登录用户**开放（「查看」是只读的，与文件列表同权限）；
  // 里面的「删除」按钮才按 isAdmin 显示 —— 权限由后端把关，前端只是不显示。
  cols.push({ key: 'actions', label: '操作', width: auth.isAdmin ? '132px' : '76px', align: 'right' })
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

// ── 切块设置（知识库级） ──
//
// 读口径与文件列表一致（登录即可）；写由后端 require_admin 把关，前端只对
// 非管理员隐藏控件。改动的生效口径（新文件即时、旧文件需重建）来自后端返回的
// `applies_to`，前端不自己写死文案 —— 规则只有一份。
const chunkingLoading = ref(false)
const chunkingSaving = ref(false)
const chunkingForm = ref({ mode: 'semantic', chunk_size: 1000, chunk_overlap: 200, separatorsText: '' })
const chunkingBounds = ref({ chunk_size_min: 100, chunk_size_max: 8000 })
const chunkingNote = ref('')
const isSemantic = computed(() => chunkingForm.value.mode === 'semantic')

/* ── 试切预览：让上面的参数可验证，而不是靠说明文字让人相信 ── */
const previewLoading = ref(false)
const chunkingPreview = ref(null)
/** 默认选**块数最多**的那个文件 —— 它最能体现出参数差异 */
const previewFileId = ref(null)

const previewFileOptions = computed(() =>
  (files.value || [])
    .slice()
    .sort((a, b) => (b.chunk_count || 0) - (a.chunk_count || 0))
    .map((f) => ({
      value: f.id,
      label: `${f.filename}（${formatTokens(f.chunk_count || 0)} 块）`,
    })),
)

async function runChunkingPreview() {
  if (!previewFileId.value) return
  previewLoading.value = true
  chunkingPreview.value = null
  try {
    const res = await previewKbChunking(previewFileId.value, {
      mode: chunkingForm.value.mode,
      chunk_size: Number(chunkingForm.value.chunk_size),
      chunk_overlap: Number(chunkingForm.value.chunk_overlap),
      separators: chunkingForm.value.separatorsText.trim() || null,
    })
    chunkingPreview.value = res?.data || null
  } catch (e) {
    toast.error(e?.message || '试切失败')
  } finally {
    previewLoading.value = false
  }
}
const modeOptions = [
  { value: 'semantic', label: '结构感知（标题 / 章节 / 段落）' },
  { value: 'fixed', label: '定长切分' },
]

/** 分隔符里的换行/制表符在单行输入框里显示成转义形式（发送时后端会还原） */
function escapeSeparator(s) {
  return String(s).replace(/\r/g, '\\r').replace(/\n/g, '\\n').replace(/\t/g, '\\t')
}

function applyChunkingConfig(data) {
  if (!data) return
  chunkingForm.value = {
    mode: data.mode,
    chunk_size: data.chunk_size,
    chunk_overlap: data.chunk_overlap,
    separatorsText: (data.separators || []).map(escapeSeparator).join(','),
  }
  if (data.bounds) chunkingBounds.value = data.bounds
  if (data.applies_to) chunkingNote.value = data.applies_to
}

async function loadFormats() {
  try {
    const res = await getKbFormats()
    kbFormats.value = res?.data || { extensions: [], groups: [] }
  } catch {
    // 拿不到就回落内置文案与不设 accept（浏览器不过滤，用户仍可全选上传）
  }
}

async function loadChunking() {
  if (!auth.isLoggedIn) return
  chunkingLoading.value = true
  try {
    const res = await getKbChunking()
    applyChunkingConfig(res?.data ?? res)
  } catch (e) {
    toast.error(`切块设置加载失败：${e.message}`)
  } finally {
    chunkingLoading.value = false
  }
}

async function saveChunking() {
  if (!auth.isAdmin) return
  chunkingSaving.value = true
  try {
    const res = await updateKbChunking({
      mode: chunkingForm.value.mode,
      chunk_size: Number(chunkingForm.value.chunk_size),
      chunk_overlap: Number(chunkingForm.value.chunk_overlap),
      // 留空 → null：清掉自定义分隔符，回落内置默认
      separators: chunkingForm.value.separatorsText.trim() || null,
    })
    const r = readResult(res, '保存失败')
    r.ok ? toast.success(r.message) : toast.error(r.message)
    if (r.ok) applyChunkingConfig(res.data)
  } catch (e) {
    // 越界值走后端 400，错误信息（如"chunk_size 必须在 100~8000 之间"）直接展示
    toast.error(e.message)
  } finally {
    chunkingSaving.value = false
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
  if (auth.isLoggedIn) {
    loadFiles()
    loadChunking()
    loadFormats()
  }
})

// 登录态从"未登录"变为已登录（如顶部登录栏登录）时自动补一次加载
watch(
  () => auth.isLoggedIn,
  (isIn) => {
    if (isIn) {
      loadFiles()
      loadChunking()
    }
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

/* ── 切块设置卡 ── */
.chunking-form {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: var(--sp-4);
}
.chunking-field {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.chunking-label {
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--c-text-2);
}
.chunking-hint {
  margin-top: var(--sp-3);
  font-size: var(--fs-xs);
  color: var(--c-text-2);
  line-height: var(--lh-base);
}
.chunking-actions {
  display: flex;
  justify-content: flex-end;
  margin-top: var(--sp-4);
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
/* 操作列里的按钮组：两个按钮时右对齐排开，不挤在一起 */
.row-actions {
  display: inline-flex;
  gap: var(--sp-2);
  justify-content: flex-end;
}

/* 试切预览：把"参数到底有没有效果"变成看得见的东西 */
.chunking-preview {
  margin-top: var(--sp-4);
  padding-top: var(--sp-4);
  border-top: 1px dashed var(--c-border);
}
.preview-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-3);
}
.preview-result {
  margin-top: var(--sp-3);
}
.preview-compare {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-2);
  margin-bottom: var(--sp-3);
  font-size: var(--fs-sm);
  color: var(--c-text-2);
}
.preview-arrow {
  color: var(--c-text-3);
}
.preview-compare b {
  color: var(--c-text);
}
.preview-sample-title {
  margin: var(--sp-3) 0 var(--sp-2);
  font-size: var(--fs-xs);
  color: var(--c-text-3);
}
.preview-chunk {
  margin-bottom: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  background: var(--c-surface-2);
  border: 1px solid var(--c-border);
  border-radius: var(--r-md);
}
.preview-chunk-head {
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--c-accent);
}
.preview-chunk-text {
  margin: var(--sp-1) 0 0;
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: var(--fs-xs);
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--c-text-2);
}
</style>
