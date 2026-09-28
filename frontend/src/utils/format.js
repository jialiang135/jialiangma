/**
 * 数字格式化工具
 *
 * 抽出来的原因：Token 统计面板与管理面板原先各写一份费用格式化，
 * 结果同一个数字在两处显示成 $0.00000 和 $0.0000 —— 不一致且都不可读。
 */

/** 千分位整数，例如 3427 → "3,427" */
export function formatTokens(n) {
  return (Number(n) || 0).toLocaleString()
}

/**
 * 费用格式化：成本通常极小，固定小数位会显示成一串零。
 * 按量级选择精度，既读得出来又不占宽度。
 */
export function formatCost(v) {
  const n = Number(v) || 0
  if (n === 0) return '0'
  if (n < 0.01) return n.toFixed(4)
  if (n < 1) return n.toFixed(3)
  return n.toFixed(2)
}
