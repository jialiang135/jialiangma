<script setup>
/**
 * 表格
 * =====
 *
 * 改造前有三套表格（全局裸 `table/th/td`、管理页 `.data-table`、
 * 评测页 `.eval-table`），外加 TokenStats 的 `.ts-model-table`。
 * 而且"窄屏表格变卡片"这套响应式技巧**只给知识库写了**
 * （用 `td::before { content: attr(data-label) }`），
 * 管理页和评测页在手机上就是一张挤爆的表格。
 *
 * 这里把它变成组件能力：所有表格窄屏自动卡片化，不用各页重写。
 *
 * 展开行
 * ------
 * 管理页的「对话日志」需要"点开某条看完整推理过程"，原先是**卡片列表**，
 * 因为表格没有展开能力。现在组件自带展开：加 `expandable`，
 * 在每行前面出现一个箭头，展开后渲染 `#expanded` 插槽。
 */
import { computed, ref } from 'vue'

import { useHasSlot } from '../../composables/useSlots.js'
import UiEmpty from './UiEmpty.vue'
import UiIcon from './UiIcon.vue'
import UiSpinner from './UiSpinner.vue'

const props = defineProps({
  /** [{ key, label, width?, mono?, align? }] */
  columns: { type: Array, required: true },
  rows: { type: Array, default: () => [] },
  /** 行的唯一键字段名 */
  rowKey: { type: String, default: 'id' },
  loading: { type: Boolean, default: false },
  /** 空数据时的文案 */
  emptyTitle: { type: String, default: '暂无数据' },
  emptyDescription: { type: String, default: '' },
  emptyIcon: { type: String, default: 'list' },
  /** 是否可展开行（需要配合 #expanded 插槽） */
  expandable: { type: Boolean, default: false },
  /** 手风琴模式：同时只展开一行（日志浏览常用） */
  accordion: { type: Boolean, default: false },
  /**
   * 点整行也能展开。默认关 —— 行内如果有按钮（删除之类），
   * 整行可点会造成误触。纯展示型的行可以打开。
   */
  expandOnRowClick: { type: Boolean, default: false },
})

const emit = defineEmits(['expand'])

const expandedKeys = ref(new Set())

const isExpanded = (row) => expandedKeys.value.has(row[props.rowKey])

function toggle(row, { silent = false } = {}) {
  const key = row[props.rowKey]
  const next = props.accordion ? new Set() : new Set(expandedKeys.value)

  if (expandedKeys.value.has(key)) {
    next.delete(key)
  } else {
    next.add(key)
    if (!silent) emit('expand', row)
  }
  expandedKeys.value = next
}

function onRowClick(row) {
  if (!props.expandable || !props.expandOnRowClick) return
  toggle(row)
}

const showEmpty = computed(() => !props.loading && props.rows.length === 0)
/** 展开行的 colspan（多出来的那列是箭头列） */
const colSpan = computed(() => props.columns.length + (props.expandable ? 1 : 0))

const slotHasEmptyAction = useHasSlot('empty-action')
const hasEmptyAction = computed(() => slotHasEmptyAction())
</script>

<template>
  <div class="ui-table-wrap">
    <div v-if="loading" class="ui-table-loading">
      <UiSpinner :size="20" label="加载中" />
    </div>

    <UiEmpty
      v-else-if="showEmpty"
      :icon="emptyIcon"
      :title="emptyTitle"
      :description="emptyDescription"
      compact
    >
      <template v-if="hasEmptyAction" #default>
        <slot name="empty-action" />
      </template>
    </UiEmpty>

    <table v-else class="ui-table">
      <thead>
        <tr>
          <th v-if="expandable" class="ui-table-expand-col" aria-hidden="true" />
          <th
            v-for="col in columns"
            :key="col.key"
            :style="col.width ? { width: col.width } : null"
            :class="{ 'is-right': col.align === 'right' }"
          >
            {{ col.label }}
          </th>
        </tr>
      </thead>
      <tbody>
        <template v-for="row in rows" :key="row[rowKey]">
          <tr
            :class="{ 'is-expanded': expandable && isExpanded(row) }"
            @click="onRowClick(row)"
          >
            <td v-if="expandable" class="ui-table-expand-col">
              <button
                class="ui-table-expand-btn"
                type="button"
                :aria-expanded="isExpanded(row)"
                :aria-label="isExpanded(row) ? '收起' : '展开'"
                @click.stop="toggle(row)"
              >
                <UiIcon
                  :name="isExpanded(row) ? 'chevron-down' : 'chevron-right'"
                  :size="15"
                />
              </button>
            </td>
            <td
              v-for="col in columns"
              :key="col.key"
              :data-label="col.label"
              :class="{ 'is-mono': col.mono, 'is-right': col.align === 'right' }"
            >
              <slot :name="`cell-${col.key}`" :row="row" :value="row[col.key]">
                {{ row[col.key] }}
              </slot>
            </td>
          </tr>

          <tr v-if="expandable && isExpanded(row)" class="ui-table-expanded">
            <td :colspan="colSpan">
              <slot name="expanded" :row="row" />
            </td>
          </tr>
        </template>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.ui-table-wrap {
  width: 100%;
  overflow-x: auto;
}
.ui-table-loading {
  display: flex;
  justify-content: center;
  padding: var(--sp-10);
}

.ui-table {
  width: 100%;
  font-size: var(--fs-sm);
  border-collapse: collapse;
}

.ui-table th {
  padding: var(--sp-3) var(--sp-4);
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  color: var(--c-text-2);
  text-align: left;
  white-space: nowrap;
  background: var(--c-surface-2);
  border-bottom: 1px solid var(--c-border);
}
.ui-table th:first-child {
  border-top-left-radius: var(--r-md);
}
.ui-table th:last-child {
  border-top-right-radius: var(--r-md);
}

.ui-table td {
  padding: var(--sp-3) var(--sp-4);
  color: var(--c-text);
  border-bottom: 1px solid var(--c-border);
  vertical-align: middle;
}
.ui-table tbody tr:last-child td {
  border-bottom: none;
}
.ui-table tbody tr:hover > td {
  background: var(--c-surface-2);
}

.ui-table .is-mono {
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
}
.ui-table .is-right {
  text-align: right;
}

/* —— 展开列 —— */
.ui-table-expand-col {
  width: 40px;
  padding-right: 0 !important;
}
.ui-table-expand-btn {
  display: inline-flex;
  padding: var(--sp-1);
  color: var(--c-text-3);
  border-radius: var(--r-sm);
  transition: color var(--dur-fast) var(--ease),
    background var(--dur-fast) var(--ease);
}
.ui-table-expand-btn:hover {
  color: var(--c-text);
  background: var(--c-surface-3);
}

.ui-table tr.is-expanded > td {
  background: var(--c-surface-2);
  border-bottom-color: transparent;
}

/* 展开内容：去掉上边框，和它所属的行连成一块 */
.ui-table-expanded > td {
  padding: 0 var(--sp-4) var(--sp-4) !important;
  background: var(--c-surface-2);
}
.ui-table-expanded:hover > td {
  background: var(--c-surface-2) !important;
}

/* ── 窄屏卡片化 ──
   表头隐藏，每个单元格前面用伪元素补上字段名，
   这样一行数据在手机上是一张自解释的小卡片，而不是横着挤爆的表格。 */
@media (max-width: 768px) {
  .ui-table thead {
    display: none;
  }
  .ui-table,
  .ui-table tbody,
  .ui-table tr,
  .ui-table td {
    display: block;
    width: 100%;
  }
  .ui-table tbody tr:not(.ui-table-expanded) {
    margin-bottom: var(--sp-3);
    background: var(--c-surface);
    border: 1px solid var(--c-border);
    border-radius: var(--r-md);
    overflow: hidden;
  }
  .ui-table tr.is-expanded:not(.ui-table-expanded) {
    margin-bottom: 0;
    border-bottom: none;
    border-bottom-left-radius: 0;
    border-bottom-right-radius: 0;
  }
  .ui-table td {
    display: flex;
    align-items: baseline;
    gap: var(--sp-3);
    padding: var(--sp-2) var(--sp-3);
    border-bottom: 1px solid var(--c-border);
  }
  .ui-table td:last-child {
    border-bottom: none;
  }
  .ui-table td::before {
    flex-shrink: 0;
    width: 5.5em;
    font-size: var(--fs-xs);
    color: var(--c-text-3);
    content: attr(data-label);
  }
  .ui-table .is-right {
    text-align: left;
  }

  /* 展开列在卡片模式下单独一行放箭头，不占字段名的位置 */
  .ui-table-expand-col {
    width: auto;
    padding: var(--sp-2) var(--sp-3) !important;
    border-bottom: 1px solid var(--c-border);
  }
  .ui-table-expand-col::before {
    content: none;
  }

  .ui-table-expanded {
    margin-bottom: var(--sp-3);
    background: var(--c-surface-2);
    border: 1px solid var(--c-border);
    border-top: none;
    border-radius: 0 0 var(--r-md) var(--r-md);
  }
  .ui-table-expanded > td {
    padding: var(--sp-3) !important;
    background: transparent;
  }
  .ui-table-expanded > td::before {
    content: none;
  }
}
</style>
