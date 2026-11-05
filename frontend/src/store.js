import { reactive, computed } from 'vue'
import { api } from './api'

export const store = reactive({
  periods: [],
  ym: localStorage.getItem('wt_ym') || '',
  dicts: { workers: [], processes: [], config: {} },
  ocr: {},
  // 健康快照（db / worker / queue）。C1 之后识别引擎跑在独立进程里，
  // 光看 ocr.loaded 已经判断不了「现在能不能识别」——要知道那个进程还在不在。
  health: {},
  stats: null,
  ready: false
})

export const currentPeriod = computed(() =>
  store.periods.find((p) => p.ym === store.ym) || store.periods[0] || null
)

export const periodLocked = computed(() => currentPeriod.value?.status === 'locked')

// 工作者名册：按角色 kind 过滤，而不是 is_inspector 布尔。
// 车间里一人多职很常见（马永刚既是工作者又是检验员），只看 is_inspector
// 会把他从工作者下拉里剔掉，明细回显就变成裸 id「13」。
// 接口没返回 kinds（后端还是旧版本）时一律放行：仅凭 is_inspector 分不清
// 「纯检验员」和「兼岗工作者」，多给一个选项远好于把匹配好的人藏起来。
export const workerRoster = computed(() =>
  (store.dicts.workers || []).filter((w) => {
    if (w.kinds?.length) return w.kinds.includes('worker')
    return true
  })
)

export const workerOptions = computed(() =>
  workerRoster.value.map((w) => ({ value: w.id, label: w.name }))
)

// 检验员名册：明细里「检验员」是手写签名，只能从名册里挑（或 allow-create 新名字）。
// 让它在核对页变成下拉，是「签名认不出来」这条链路上成本最低的一环——
// 人眼认签名一秒，比让模型去猜草书靠谱得多。
export const inspectorOptions = computed(() =>
  (store.dicts.workers || [])
    .filter((w) => {
      if (w.kinds?.length) return w.kinds.includes('inspector')
      return !!w.is_inspector
    })
    .map((w) => ({ value: w.name, label: w.name }))
)

// 当前值不在名册里（人员被停用/删除，或历史数据指向已删人员）时，给一个兜底选项，
// 否则 el-select 只会把原始 value 当文本显示出来。
// 兜底名优先取人员表真名：名册是按角色过滤出来的，被过滤掉不等于这人不存在；
// worker_raw 是手写识别的原始文本，常常是「马áA」这类噪声，只在查无此人时用。
export function orphanWorkerOption(item) {
  const id = item?.worker_id
  if (id === null || id === undefined) return null
  if (workerOptions.value.some((o) => o.value === id)) return null
  const known = (store.dicts.workers || []).find((w) => w.id === id)
  return { value: id, label: known?.name || item.worker_raw || `人员 #${id}` }
}

export function setPeriod(ym) {
  store.ym = ym
  localStorage.setItem('wt_ym', ym)
  refreshStats()
}

export async function loadDicts() {
  const [dicts, health] = await Promise.all([api.dicts(), api.health().catch(() => ({}))])
  store.dicts = dicts
  store.ocr = health.ocr || dicts.ocr || {}
  store.health = health
}

/** 只刷健康快照（很轻：4 个 count + 看心跳文件）。
 *
 * 顶栏那盏灯原先只在进入页面时取一次，OCR 进程半路挂了它还亮着绿的 ——
 * 而人正是靠这盏灯判断「要不要去重启」。所以要能定期刷。
 */
export async function refreshHealth() {
  try {
    store.health = await api.health()
    store.ocr = store.health.ocr || store.ocr
  } catch (e) {
    // 后端不可达时不要把灯留成绿色：这是最需要报警的一种情况。
    // app/org 要留住上一份：断线时顶栏的版本号、主机名不该跟着变成"—"，
    // 那不是「状态变了」，那是「没问到」，两件事得分开看。
    const prev = store.health || {}
    store.health = {
      ok: false,
      db: { ok: false, error: '后端无响应' },
      worker: { alive: false },
      queue: {},
      app: prev.app,
      org: prev.org
    }
  }
}

export async function loadPeriods() {
  const list = await api.periods()
  store.periods = list
  if (!list.length) return
  if (!list.some((p) => p.ym === store.ym)) {
    // 默认落到**当前月**：报工是当月发生的事，系统却停在几个月前的核算期，
    // 就会看到「刚报的工时一分都没进」——其实记到本月去了。
    const now = new Date()
    const cur = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`
    store.ym = list.some((p) => p.ym === cur) ? cur : list[0].ym
    localStorage.setItem('wt_ym', store.ym)
  }
}

export async function refreshStats() {
  try {
    store.stats = await api.stats(store.ym)
  } catch (e) {
    store.stats = null
  }
}

export async function bootstrap() {
  if (store.ready) return
  await Promise.all([loadDicts(), loadPeriods()])
  await refreshStats()
  store.ready = true
}
