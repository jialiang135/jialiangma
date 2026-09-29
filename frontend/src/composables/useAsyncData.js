/**
 * 异步数据加载
 * ============
 *
 * 改造前"设 loading → try 请求 → catch 报错 → finally 收尾"这套模板
 * 在四个视图里手写了约 10 遍，而且错误处理有四种互不相同的约定：
 * 有的存进 `dashError`、有的塞进 `userMsg`、有的静默吞掉（管理页的
 * chat-logs / files / audit / system 四个 tab 请求失败时**用户什么都看不到**）。
 *
 * 这里把模板收成一个组合式函数，顺便强制"错误必须落到 error 上"，
 * 让视图自己决定怎么展示（alert / toast / 静默但至少能查）。
 *
 * 用法：
 *   const { data, loading, error, run } = useAsyncData(() => getKbFiles())
 *   onMounted(run)
 *   // 模板里： v-if="loading" / v-else-if="error" / v-else
 */
import { ref, shallowRef } from 'vue'

export function useAsyncData(fetcher, { immediate = false, initial = null } = {}) {
  // shallowRef：这些数据是整块替换的（列表/对象），深层响应式只会白付性能
  const data = shallowRef(initial ?? null)
  const loading = ref(false)
  const error = ref('')

  /** 并发保护：后发的请求赢，先发的回来时丢弃（避免慢请求覆盖新结果） */
  let seq = 0

  async function run(...args) {
    const mySeq = ++seq
    loading.value = true
    error.value = ''
    try {
      const res = await fetcher(...args)
      if (mySeq === seq) data.value = res
      return res
    } catch (e) {
      if (mySeq === seq) error.value = e?.message || '请求失败'
      return null
    } finally {
      if (mySeq === seq) loading.value = false
    }
  }

  if (immediate) run()

  return { data, loading, error, run }
}

/**
 * 一次性动作（提交、删除、重建…）
 *
 * 与 useAsyncData 的区别：不保存结果，只关心"在跑 / 出错"，
 * 适合绑到按钮上（`<UiButton :loading="pending">`）。
 */
export function useAsyncAction(action) {
  const pending = ref(false)
  const error = ref('')

  async function run(...args) {
    pending.value = true
    error.value = ''
    try {
      return await action(...args)
    } catch (e) {
      error.value = e?.message || '操作失败'
      return null
    } finally {
      pending.value = false
    }
  }

  return { pending, error, run }
}
