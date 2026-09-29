<script setup>
/**
 * 对话历史侧栏
 * ============
 *
 * 改造前的问题：每条记录上都有一个常驻的 `×` 删除按钮，一整列看过去全是叉，
 * 视觉噪音很大；标题一律是「介绍一下你自己」这类重复文本，很难区分。
 *
 * 改动：
 * - 删除按钮**hover 才出现**（触屏上一直是显示的，因为没有 hover）
 * - 时间改用相对时间（"3 分钟前"），比 "09-29 05:52" 一眼能看出新旧
 * - 空状态和加载态用统一组件，不再是裸文字
 */
import { formatRelativeTime, truncate } from '../../utils/format.js'
import UiEmpty from '../ui/UiEmpty.vue'
import UiIcon from '../ui/UiIcon.vue'
import UiSpinner from '../ui/UiSpinner.vue'

defineProps({
  items: { type: Array, default: () => [] },
  loading: { type: Boolean, default: false },
  activeId: { type: String, default: null },
})

const emit = defineEmits(['select', 'delete', 'refresh', 'new'])
</script>

<template>
  <aside class="sidebar">
    <div class="sidebar-head">
      <span class="sidebar-title">历史对话</span>
      <button class="icon-btn" type="button" title="刷新" @click="emit('refresh')">
        <UiIcon name="refresh" :size="15" />
      </button>
    </div>

    <button class="new-chat" type="button" @click="emit('new')">
      <UiIcon name="plus" :size="15" />
      <span>新对话</span>
    </button>

    <div class="sidebar-list">
      <div v-if="loading" class="sidebar-loading">
        <UiSpinner :size="18" />
      </div>

      <UiEmpty
        v-else-if="!items.length"
        icon="message"
        title="还没有对话"
        description="问第一个问题，这里就会出现记录"
        compact
      />

      <template v-else>
        <button
          v-for="item in items"
          :key="item.group_id"
          class="conv"
          :class="{ 'is-active': item.group_id === activeId }"
          type="button"
          @click="emit('select', item)"
        >
          <span class="conv-title">{{ truncate(item.first_question || '新对话', 30) }}</span>
          <span class="conv-meta">
            <span>{{ item.turn_count }} 轮</span>
            <span class="conv-dot">·</span>
            <span>{{ formatRelativeTime(item.last_at) }}</span>
          </span>
          <span
            class="conv-delete"
            role="button"
            tabindex="0"
            title="删除此对话"
            @click.stop="emit('delete', item)"
            @keydown.enter.stop="emit('delete', item)"
          >
            <UiIcon name="trash" :size="14" />
          </span>
        </button>
      </template>
    </div>
  </aside>
</template>

<style scoped>
.sidebar {
  display: flex;
  flex-direction: column;
  width: var(--sidebar-w);
  flex-shrink: 0;
  background: var(--c-surface);
  border-right: 1px solid var(--c-border);
}

.sidebar-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--sp-3) var(--sp-4);
}
.sidebar-title {
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
  color: var(--c-text-2);
}

.icon-btn {
  display: inline-flex;
  padding: var(--sp-1);
  color: var(--c-text-3);
  border-radius: var(--r-sm);
  transition: color var(--dur-fast) var(--ease),
    background var(--dur-fast) var(--ease);
}
.icon-btn:hover {
  color: var(--c-text);
  background: var(--c-surface-2);
}

.new-chat {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--sp-2);
  margin: 0 var(--sp-3) var(--sp-3);
  padding: var(--sp-2);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--c-accent-text);
  background: var(--c-accent-soft);
  border: 1px solid var(--c-accent-border);
  border-radius: var(--r-md);
  transition: background var(--dur-fast) var(--ease);
}
.new-chat:hover {
  background: var(--c-accent);
  border-color: var(--c-accent);
  color: var(--c-white);
}

.sidebar-list {
  flex: 1;
  overflow-y: auto;
  padding: 0 var(--sp-2) var(--sp-3);
}
.sidebar-loading {
  display: flex;
  justify-content: center;
  padding: var(--sp-6);
}

.conv {
  position: relative;
  display: block;
  width: 100%;
  padding: var(--sp-2) var(--sp-3);
  text-align: left;
  border-radius: var(--r-md);
  transition: background var(--dur-fast) var(--ease);
}
.conv:hover {
  background: var(--c-surface-2);
}
.conv.is-active {
  background: var(--c-accent-soft);
}

.conv-title {
  display: block;
  overflow: hidden;
  font-size: var(--fs-sm);
  color: var(--c-text);
  text-overflow: ellipsis;
  white-space: nowrap;
}
.conv.is-active .conv-title {
  color: var(--c-accent-text);
  font-weight: var(--fw-medium);
}

.conv-meta {
  display: flex;
  align-items: center;
  gap: var(--sp-1);
  margin-top: 2px;
  font-size: var(--fs-xs);
  color: var(--c-text-3);
}
.conv-dot {
  opacity: 0.6;
}

/* 删除按钮：hover 才出现，避免整列都是叉。
   触屏设备没有 hover，所以用 @media (hover: none) 让它常显。 */
.conv-delete {
  position: absolute;
  top: 50%;
  right: var(--sp-2);
  display: flex;
  padding: var(--sp-1);
  color: var(--c-text-3);
  background: var(--c-surface);
  border-radius: var(--r-sm);
  opacity: 0;
  transform: translateY(-50%);
  transition: opacity var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease);
}
.conv:hover .conv-delete,
.conv-delete:focus-visible {
  opacity: 1;
}
.conv-delete:hover {
  color: var(--c-danger);
}

@media (hover: none) {
  .conv-delete {
    opacity: 1;
  }
}

@media (max-width: 768px) {
  .sidebar {
    display: none;
  }
}
</style>
