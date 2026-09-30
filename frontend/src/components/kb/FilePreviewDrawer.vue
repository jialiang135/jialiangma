<script setup>
/**
 * 文件预览抽屉
 * ============
 *
 * 在知识库列表里点开一个文件，看两样东西：
 *
 *   原文件     —— 磁盘上那份文件本身（PDF 交给浏览器内置查看器）
 *   入库切片   —— **模型实际读到的东西**，按 chunk_idx 排列，带页码与字数
 *
 * 为什么"切片"这个视图重要：分块边界在哪、PDF 页码有没有丢、解析有没有把标题
 * 重复一遍（实测 `20_简历_马佳良.pdf` 就有"教育经历 教育经历"这种现象）——
 * 这些以前**只有模型看得见**，出事时无从判断。切片正文本来就完整存在向量库里，
 * 只有 SSE 的证据轨为了控制单帧大小截断到 800 字，所以这个视图不需要重新解析
 * 文件，也不会失真。
 *
 * 原文件为什么要绕一圈 blob：
 * 接口要 `Authorization: Bearer`，而 `<iframe src="/api/...">` **带不上请求头**
 * （JWT 存在 localStorage）。所以必须 fetch 成 blob 再 `createObjectURL`，
 * 用完 revoke —— 不 revoke 的话整个文件会常驻内存。
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'

import { getKbFileBlob, getKbFileChunks } from '../../api/kb.js'
import UiAlert from '../ui/UiAlert.vue'
import UiButton from '../ui/UiButton.vue'
import UiDrawer from '../ui/UiDrawer.vue'
import UiIcon from '../ui/UiIcon.vue'
import UiSpinner from '../ui/UiSpinner.vue'
import UiTabs from '../ui/UiTabs.vue'

const props = defineProps({
  open: { type: Boolean, default: false },
  /** { id, filename, chunk_count } */
  file: { type: Object, default: null },
  /** 从证据轨跳进来时要高亮/滚动到的切片序号 */
  focusChunk: { type: Number, default: null },
})

const emit = defineEmits(['close'])

const TABS = [
  { key: 'raw', label: '原文件', icon: 'file' },
  { key: 'chunks', label: '入库切片', icon: 'layers' },
]

const tab = ref('raw')

const chunks = ref([])
const chunkTotal = ref(0)
const chunksLoading = ref(false)
const chunksError = ref('')

const rawUrl = ref('')
const rawText = ref('')
const rawLoading = ref(false)
const rawError = ref('')
/** 'pdf' | 'image' | 'text' | 'binary' */
const rawKind = ref('binary')

const EXTS = {
  pdf: 'pdf',
  png: 'image',
  jpg: 'image',
  jpeg: 'image',
  gif: 'image',
  webp: 'image',
  bmp: 'image',
  md: 'text',
  markdown: 'text',
  txt: 'text',
  csv: 'text',
  tsv: 'text',
  json: 'text',
  log: 'text',
  yml: 'text',
  yaml: 'text',
  html: 'text',
  css: 'text',
  js: 'text',
  ts: 'text',
  py: 'text',
  java: 'text',
  go: 'text',
  sql: 'text',
  sh: 'text',
}

const ext = computed(() => (props.file?.filename || '').split('.').pop()?.toLowerCase() || '')
const chunksChars = computed(() => chunks.value.reduce((n, c) => n + (c.chars || 0), 0))

function revokeRaw() {
  if (rawUrl.value) {
    URL.revokeObjectURL(rawUrl.value)
    rawUrl.value = ''
  }
}

/** 换文件 / 关闭时把状态清干净，别让上一个文件的内容闪一下 */
function reset() {
  revokeRaw()
  rawText.value = ''
  rawError.value = ''
  rawKind.value = 'binary'
  chunks.value = []
  chunkTotal.value = 0
  chunksError.value = ''
  tab.value = 'raw'
}

async function loadRaw() {
  if (!props.file || rawLoading.value || rawUrl.value || rawText.value) return
  rawLoading.value = true
  rawError.value = ''
  try {
    const blob = await getKbFileBlob(props.file.id)
    const kind = EXTS[ext.value] || 'binary'
    rawKind.value = kind
    if (kind === 'text') {
      rawText.value = await blob.text()
    } else {
      rawUrl.value = URL.createObjectURL(blob)
    }
  } catch (e) {
    rawError.value = e?.message || '加载原文件失败'
  } finally {
    rawLoading.value = false
  }
}

async function loadChunks() {
  if (!props.file || chunksLoading.value || chunks.value.length) return
  chunksLoading.value = true
  chunksError.value = ''
  try {
    const res = await getKbFileChunks(props.file.id)
    const data = res?.data || {}
    chunks.value = data.chunks || []
    chunkTotal.value = data.total || 0
    scrollToFocus()
  } catch (e) {
    chunksError.value = e?.message || '加载切片失败'
  } finally {
    chunksLoading.value = false
  }
}

/** 从证据轨跳进来时，把目标切片滚到视野中间并高亮 */
async function scrollToFocus() {
  if (props.focusChunk === null || props.focusChunk === undefined) return
  await new Promise((r) => requestAnimationFrame(r))
  const el = document.querySelector(`[data-chunk-idx="${props.focusChunk}"]`)
  el?.scrollIntoView({ block: 'center', behavior: 'smooth' })
}

watch(
  () => [props.open, props.file?.id],
  ([open]) => {
    if (!open) {
      reset()
      return
    }
    reset()
    // 默认标签是"原文件"，切片等切过去再拉（省一次请求）
    loadRaw()
    if (props.focusChunk !== null && props.focusChunk !== undefined) {
      tab.value = 'chunks'
      loadChunks()
    }
  },
  { immediate: true },
)

watch(tab, (now) => {
  if (now === 'raw') loadRaw()
  else loadChunks()
})

onBeforeUnmount(revokeRaw)
</script>

<template>
  <UiDrawer :open="open" size="lg" @close="emit('close')">
    <template #header>
      <div class="head">
        <UiIcon name="file" :size="15" class="head-icon" />
        <div class="head-text">
          <div class="head-name">{{ file?.filename || '文件预览' }}</div>
          <div class="head-meta">
            <template v-if="chunkTotal">
              {{ chunkTotal }} 个切片 · 共 {{ chunksChars.toLocaleString() }} 字
            </template>
            <template v-else>按切片查看模型实际读到的内容</template>
          </div>
        </div>
      </div>
    </template>

    <div class="tabs-bar">
      <UiTabs v-model="tab" :tabs="TABS" variant="underline" />
    </div>

    <!-- ── 原文件 ── -->
    <div v-show="tab === 'raw'" class="pane">
      <div v-if="rawLoading" class="center">
        <UiSpinner :size="20" label="加载中" />
      </div>
      <UiAlert v-else-if="rawError" tone="danger">
        {{ rawError }}
        <template #action>
          <UiButton size="sm" variant="secondary" @click="loadRaw">重试</UiButton>
        </template>
      </UiAlert>

      <iframe
        v-else-if="rawKind === 'pdf'"
        class="raw-frame"
        :src="rawUrl"
        :title="`${file?.filename} 预览`"
      />
      <div v-else-if="rawKind === 'image'" class="center">
        <img class="raw-image" :src="rawUrl" :alt="file?.filename" />
      </div>
      <pre v-else-if="rawKind === 'text'" class="raw-text">{{ rawText }}</pre>

      <div v-else class="center binary">
        <UiIcon name="file" :size="28" />
        <p class="binary-title">这个格式浏览器无法直接预览</p>
        <p class="binary-desc">
          它已经入库了，切到「入库切片」能看到解析出来的全部内容。
        </p>
        <a class="download" :href="rawUrl" :download="file?.filename">
          下载原文件
        </a>
      </div>
    </div>

    <!-- ── 入库切片 ── -->
    <div v-show="tab === 'chunks'" class="pane">
      <div v-if="chunksLoading" class="center">
        <UiSpinner :size="20" label="加载中" />
      </div>
      <UiAlert v-else-if="chunksError" tone="danger">
        {{ chunksError }}
        <template #action>
          <UiButton size="sm" variant="secondary" @click="loadChunks">重试</UiButton>
        </template>
      </UiAlert>

      <template v-else>
        <p class="chunks-hint">
          这就是**模型实际读到的内容**，按块序号排列。证据轨里的每个片段都能在这里找到出处。
        </p>
        <ol class="chunks">
          <li
            v-for="c in chunks"
            :key="c.chunk_idx"
            :data-chunk-idx="c.chunk_idx"
            class="chunk"
            :class="{ 'is-focus': c.chunk_idx === focusChunk }"
          >
            <div class="chunk-head">
              <span class="chunk-idx">#{{ c.chunk_idx }}</span>
              <span v-if="c.page" class="chunk-tag">第 {{ c.page }} 页</span>
              <span class="chunk-chars">{{ c.chars }} 字</span>
            </div>
            <pre class="chunk-text">{{ c.content }}</pre>
          </li>
        </ol>
        <p v-if="!chunks.length" class="chunks-empty">
          这个文件在向量库里没有切片。
        </p>
      </template>
    </div>
  </UiDrawer>
</template>

<style scoped>
.head {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: var(--sp-3);
}
.head-icon {
  flex-shrink: 0;
  color: var(--c-accent);
}
.head-text {
  min-width: 0;
}
.head-name {
  overflow: hidden;
  font-size: var(--fs-base);
  font-weight: var(--fw-semibold);
  text-overflow: ellipsis;
  white-space: nowrap;
}
.head-meta {
  font-size: var(--fs-xs);
  font-weight: var(--fw-normal);
  color: var(--c-text-3);
}

/* 标签条粘在头部下方：切片列表很长时不用滚回顶部才能切标签 */
.tabs-bar {
  position: sticky;
  top: 0;
  z-index: var(--z-base);
  padding: 0 var(--sp-5);
  background: var(--c-surface);
  border-bottom: 1px solid var(--c-border);
}

.pane {
  padding: var(--sp-4) var(--sp-5) var(--sp-6);
}

.center {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--sp-3);
  padding: var(--sp-8) var(--sp-4);
  color: var(--c-text-3);
  text-align: center;
}

.raw-frame {
  width: 100%;
  height: 70vh;
  border: 1px solid var(--c-border);
  border-radius: var(--r-md);
}
.raw-image {
  max-width: 100%;
  border-radius: var(--r-md);
}
.raw-text,
.chunk-text {
  margin: 0;
  font-family: var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  font-size: var(--fs-xs);
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
}
.raw-text {
  padding: var(--sp-4);
  background: var(--c-surface-2);
  border: 1px solid var(--c-border);
  border-radius: var(--r-md);
}

.binary-title {
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--c-text);
}
.binary-desc {
  max-width: 32em;
  font-size: var(--fs-xs);
}
.download {
  font-size: var(--fs-sm);
  color: var(--c-accent);
  text-decoration: underline;
}

.chunks-hint {
  margin-bottom: var(--sp-4);
  font-size: var(--fs-xs);
  line-height: var(--lh-base);
  color: var(--c-text-3);
}
.chunks {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  margin: 0;
  padding: 0;
  list-style: none;
}
.chunk {
  padding: var(--sp-3) var(--sp-4);
  background: var(--c-surface-2);
  border: 1px solid var(--c-border);
  border-radius: var(--r-md);
}
.chunk.is-focus {
  border-color: var(--c-accent);
  box-shadow: 0 0 0 3px var(--c-accent-soft);
}
.chunk-head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  margin-bottom: var(--sp-2);
  font-size: var(--fs-xs);
}
.chunk-idx {
  font-weight: var(--fw-semibold);
  color: var(--c-accent);
  font-family: var(--font-mono, ui-monospace, monospace);
}
.chunk-tag {
  padding: 0 var(--sp-2);
  color: var(--c-text-2);
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--r-sm);
}
.chunk-chars {
  margin-left: auto;
  color: var(--c-text-3);
}
.chunks-empty {
  padding: var(--sp-6) 0;
  font-size: var(--fs-sm);
  color: var(--c-text-3);
  text-align: center;
}
</style>
