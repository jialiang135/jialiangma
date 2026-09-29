/**
 * 插槽内容判断
 * ============
 *
 * 用途：判断一个插槽**是否真的渲染出了东西**。
 *
 * 为什么不能直接 `v-if="$slots.xxx"`：插槽只要被**声明**了，
 * `$slots.xxx` 就是真值。调用方写
 * `<template #action><UiButton v-if="isAdmin">重试</UiButton></template>`
 * 而 `isAdmin` 为假时，渲染结果是一个**注释节点**，但外层包裹元素照样渲染，
 * 页面上就多出一块莫名其妙的空白。
 *
 * 这个坑在 UiEmpty 上真实发生过（管理页/知识库页都用到了
 * `#empty-action` 条件渲染）。所以抽成共享判断，避免每个组件各踩一次。
 */
import { Comment, Text, useSlots } from 'vue'

/** 单个节点算不算"有内容" */
function isRealNode(node) {
  if (!node || typeof node !== 'object') return false
  // 注释节点：v-if 为假留下的占位就是它
  if (node.type === Comment) return false
  // 空白文本节点不算内容（模板里的换行缩进会产生）
  if (node.type === Text) return String(node.children ?? '').trim().length > 0
  // Fragment（v-if / v-for 常见包裹）往里看一层
  if (Array.isArray(node.children)) return node.children.some(isRealNode)
  return true
}

/**
 * 用法：
 *   const hasAction = useHasSlot('action')
 *   <div v-if="hasAction" class="alert-action"><slot name="action" /></div>
 *
 * @param {string} [name] 具名插槽名；不传则看默认插槽
 * @returns {() => boolean} 调用后返回布尔值（放在 computed 里用）
 */
export function useHasSlot(name) {
  const slots = useSlots()
  return () => {
    const render = name ? slots[name] : slots.default
    if (!render) return false
    return render().some(isRealNode)
  }
}
