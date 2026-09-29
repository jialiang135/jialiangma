/**
 * 轮询等待任务完成
 * ================
 *
 * 改造前有三份各自实现的轮询循环，间隔都是 1500ms，都有终态判断和次数上限：
 * - `KnowledgeView.pollTaskStatus`（上传任务进度，上限 120 次 / 2 分钟）
 * - `KnowledgeView.doRebuild`     （重建向量索引，上限 1200 次 / 30 分钟）
 * - `EvalView.poll`               （评测报告状态，上限 1600 次 / 40 分钟）
 *
 * 三份代码结构相同、参数不同、bug 也不同（有的用 setTimeout 递归、
 * 有的用 for + await sleep；卸载时都没有清理，组件销毁后循环还在跑）。
 *
 * 这里统一成一个带取消能力的实现。
 *
 * 用法：
 *   const { start, stop, active } = usePolling()
 *   await start(() => getUploadStatus(id), {
 *     isDone: (r) => ['done', 'failed', 'skipped'].includes(r.status),
 *     onTick: (r) => { ... },
 *   })
 */
import { ref, onBeforeUnmount } from 'vue'

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

export function usePolling() {
  const active = ref(false)
  // 每次 start 递增，用来让"上一轮"的循环察觉到自己是过期的
  let generation = 0

  /**
   * @param {Function} fetcher 每次轮询调用的异步函数
   * @param {object} opts
   * @param {number} opts.interval 间隔毫秒，默认 1500
   * @param {number} opts.maxAttempts 最大次数
   * @param {(r:any)=>boolean} opts.isDone 判定终态
   * @param {(r:any)=>void} opts.onTick 每次拿到结果回调
   * @returns {Promise<any|null>} 终态结果；超时或被取消返回 null
   */
  async function start(fetcher, opts = {}) {
    const {
      interval = 1500,
      maxAttempts = 120,
      isDone = () => true,
      onTick,
    } = opts

    const myGen = ++generation
    active.value = true

    try {
      for (let i = 0; i < maxAttempts; i++) {
        if (myGen !== generation) return null // 已被新的一轮或 stop() 取代
        const res = await fetcher()
        if (myGen !== generation) return null
        onTick?.(res, i)
        if (isDone(res)) return res
        await sleep(interval)
      }
      return null // 超出上限
    } catch (e) {
      if (myGen === generation) throw e
      return null
    } finally {
      if (myGen === generation) active.value = false
    }
  }

  /** 取消当前轮询（组件卸载时自动调用） */
  function stop() {
    generation++
    active.value = false
  }

  onBeforeUnmount(stop)

  return { start, stop, active }
}
