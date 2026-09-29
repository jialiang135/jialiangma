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
 */
import { computed } from 'vue'
import UiEmpty from './UiEmpty.vue'
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
})

const showEmpty = computed(() => !props.loading && props.rows.length === 0)
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
      <slot name="empty-action" />
    </UiEmpty>

    <table v-else class="ui-table">
      <thead>
        <tr>
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
        <tr v-for="row in rows" :key="row[rowKey]">
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
.ui-table tbody tr:hover td {
  background: var(--c-surface-2);
}

.ui-table .is-mono {
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
}
.ui-table .is-right {
  text-align: right;
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
  .ui-table tr {
    margin-bottom: var(--sp-3);
    background: var(--c-surface);
    border: 1px solid var(--c-border);
    border-radius: var(--r-md);
    overflow: hidden;
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
}
</style>
