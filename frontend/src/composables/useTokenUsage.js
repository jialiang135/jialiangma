/**
 * Token 用量刷新信号
 * ==================
 *
 * 改造前用 `window.dispatchEvent(new CustomEvent('token-usage-updated'))`
 * 通知用量统计页刷新。那个做法能用，但有三个问题：
 * 1. 字符串事件名散在发送方和接收方两处，写错一个字符就静默失效
 * 2. 挂在 window 上，测试时不好隔离、也不好追踪谁在发
 * 3. 类型/来源完全不可见，读代码时看不出这条事件的契约
 *
 * 换成一个模块级的 ref 计数器：谁改了数据就 `bump()`，
 * 订阅方 `watch(signal, ...)`。仍然是解耦的，但契约是显式的。
 */
import { ref } from 'vue'

const signal = ref(0)

/** 通知订阅方"token 数据变了，去重新拉一次" */
export function bumpTokenUsage() {
  signal.value += 1
}

export function useTokenUsageSignal() {
  return signal
}
