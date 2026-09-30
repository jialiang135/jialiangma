<script setup>
/**
 * 证据轨
 * ======
 *
 * 这个产品的差异化在这里：**答案句句有据可查**。
 * 改造前这份能力被藏在"推理过程"折叠面板里，还和英文的模型思考流混在一起，
 * 等于把王牌塞进了裤兜。
 *
 * 现在它单独成一条轨，贴在每条回答下面：折叠态是一行"依据 N 个知识库片段"，
 * 展开后每条来源给出**文档名 + 相关度 + 片段正文** ——
 * 提问者能直接看到这个答案是从哪份简历、哪段项目文档里来的。
 *
 * 为什么不做"点某句高亮对应来源"：那需要改提示词让模型输出可解析的引用编号，
 * 会动到回答本身（幻觉抑制等已有行为要重新验证），风险与收益不成比例。
 * 当前方案先如实呈现"依据了哪些片段"，不断言哪句出自哪片。
 */
import { computed, ref } from 'vue'

import { relevanceLabel, relevanceTone } from '../../composables/useEvidence.js'
import { plainTextFromMarkdown } from '../../utils/markdown.js'
import UiBadge from '../ui/UiBadge.vue'
import UiIcon from '../ui/UiIcon.vue'

const props = defineProps({
  /** [{ key, source, score, content, chunkIdx }] */
  items: { type: Array, default: () => [] },
  /** 流式进行中：显示"检索中"而不是"没有依据" */
  streaming: { type: Boolean, default: false },
})

/**
 * 片段是知识库文件的**原文**，直接显示会露出 `##`、`**`、表格竖线。
 * 这里剥成纯文本再展示 —— 证据要传达的是"这段文字讲了什么"，
 * 不是"这份文档的排版长什么样"。
 */
const displayItems = computed(() =>
  props.items.map((item) => ({
    ...item,
    plain: plainTextFromMarkdown(item.content),
  })),
)

const open = ref(false)
/** 单独展开正文的片段 key 集合 */
const expandedKeys = ref(new Set())

const count = computed(() => props.items.length)

function toggleFragment(key) {
  const next = new Set(expandedKeys.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  expandedKeys.value = next
}

const isFragmentOpen = (key) => expandedKeys.value.has(key)

function scoreText(score) {
  return score == null ? '—' : score.toFixed(3)
}
</script>

<template>
  <div v-if="count || streaming" class="evidence">
    <button
      class="evidence-toggle"
      type="button"
      :aria-expanded="open"
      :disabled="!count"
      @click="open = !open"
    >
      <UiIcon name="layers" :size="15" />
      <span v-if="count">依据 {{ count }} 个知识库片段</span>
      <span v-else>正在检索知识库…</span>
      <UiIcon v-if="count" :name="open ? 'chevron-down' : 'chevron-right'" :size="14" class="evidence-chevron" />
    </button>

    <Transition name="evidence-expand">
      <ul v-if="open && count" class="evidence-list">
        <li v-for="item in displayItems" :key="item.key" class="evidence-item">
          <div class="evidence-head">
            <UiIcon name="file" :size="14" class="evidence-file-icon" />
            <span class="evidence-source" :title="item.source">{{ item.source }}</span>
            <UiBadge :tone="relevanceTone(item.score)" :mono="item.score != null">
              {{ relevanceLabel(item.score) }} {{ scoreText(item.score) }}
            </UiBadge>
          </div>

          <p
            class="evidence-content"
            :class="{ 'is-expanded': isFragmentOpen(item.key) }"
            @click="toggleFragment(item.key)"
          >
            {{ item.plain }}
          </p>

          <button
            v-if="item.plain.length > 90"
            class="evidence-more"
            type="button"
            @click="toggleFragment(item.key)"
          >
            {{ isFragmentOpen(item.key) ? '收起' : '展开全文' }}
          </button>
        </li>
      </ul>
    </Transition>
  </div>
</template>

<style scoped>
.evidence {
  margin-top: var(--sp-3);
}

.evidence-toggle {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-1) var(--sp-2);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--c-text-2);
  border-radius: var(--r-sm);
  transition: color var(--dur-fast) var(--ease),
    background var(--dur-fast) var(--ease);
}
.evidence-toggle:hover:not(:disabled) {
  color: var(--c-accent-text);
  background: var(--c-accent-soft);
}
.evidence-toggle:disabled {
  cursor: default;
  color: var(--c-text-3);
}
.evidence-chevron {
  color: var(--c-text-3);
}

.evidence-list {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  margin-top: var(--sp-2);
}

.evidence-item {
  padding: var(--sp-3);
  background: var(--c-surface-2);
  border: 1px solid var(--c-border);
  border-radius: var(--r-md);
}

.evidence-head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  margin-bottom: var(--sp-2);
}
.evidence-file-icon {
  color: var(--c-text-3);
}
.evidence-source {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--c-text);
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 片段正文默认只露两行 —— 证据是"佐证"，不该把答案挤下去。
   点击展开全文。 */
.evidence-content {
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  overflow: hidden;
  font-size: var(--fs-xs);
  line-height: var(--lh-base);
  color: var(--c-text-2);
  cursor: pointer;
  word-break: break-word;
}
.evidence-content.is-expanded {
  display: block;
  -webkit-line-clamp: unset;
}

.evidence-more {
  margin-top: var(--sp-1);
  font-size: var(--fs-xs);
  color: var(--c-accent);
}
.evidence-more:hover {
  text-decoration: underline;
}

.evidence-expand-enter-active,
.evidence-expand-leave-active {
  transition: opacity var(--dur-base) var(--ease);
}
.evidence-expand-enter-from,
.evidence-expand-leave-to {
  opacity: 0;
}
</style>
