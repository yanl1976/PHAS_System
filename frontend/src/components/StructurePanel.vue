<script setup>
/**
 * 产品结构（装配关系）：总装 → 部件 → 子件。
 *
 * 为什么单独一个组件：**产品结构和工艺编排是两件事**。
 * 结构回答「这台机器由哪些件组成、谁装在谁下面、每台几件、先后顺序」，
 * 是设计的装配事实；工艺回答「这个件怎么做、几道工序、多少工时」。
 * 一个件哪怕一道工序都没编，它在结构里照样存在、照样有件号。
 *
 * 之前把结构编辑混在「工时组合 → 按产品树」这一整页里，页面同时干两件事，
 * 看起来像是在改工艺；现在它只在这一个弹窗里出现，工时组合与工艺编排都
 * 只是它的消费者。
 *
 * 交互：点中一件，`←` 退一级、`→` 挂到上面那件里、`↑↓` 调同层顺序；
 * 按下即入库（没有保存按钮），`Ctrl+Z` 撤回。每一步都留审计。
 */
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api'
import PartRouteDialog from './PartRouteDialog.vue'

const props = defineProps({
  // 打开时聚焦哪个产品（空 = 全部）
  product: { type: String, default: '' },
  // 打开时定位到哪一件
  focusPart: { type: String, default: '' }
})
const emit = defineEmits(['changed'])

const products = ref([])
const allTrees = ref([])
const treeData = computed(() =>
  picked.value
    ? allTrees.value.filter((t) => (t.raw.product || '') === picked.value)
    : allTrees.value
)
const treeLoading = ref(false)
const expanded = ref([])
const treeKey = ref(0)
const treeRef = ref(null)
const picked = ref('') // 产品下拉：'' = 全部
const kw = ref('')

const focusKey = ref('')
const focusRow = ref(null)
const focusPath = ref([])
const moving = ref(false)

// 方向键按下即入库，界面上必须留一个回执：什么时候存的、最后改了哪一步。
const lastSaved = ref(null)
const undoStack = ref([])
const UNDO_MAX = 20

const autoVisible = ref(false)
const autoLoading = ref(false)
const autoApplying = ref(false)
const autoPre = ref({ count: 0, products: 0, items: [], skipped: [] })
const autoPick = ref([])

// none = 结构里有这一件，但台账里还没登记组合（也就是还没编工艺）。
// 这里必须说「无工艺」而不是「无路线」：卡片没出来是因为缺工艺身份，
// 不是路线没生效，工艺员据此知道该去台账补一条组合。
function stateText(s) {
  return { released: '已发布', draft: '草稿', archived: '已归档', none: '无工艺' }[s] || s || '—'
}
// 状态标签直接给 CSS 类，不走 el-tag：
// el-tree 的节点插槽是「每节点一棵子树」，541 个节点 × 2 个 el-tag = 1082 个组件。
// 展开一个 100 件的部件，就是当场建 200 个组件 —— 手感上是「点一下顿一秒」。
function stateCls(s) {
  return {
    released: 's-released',
    draft: 's-draft',
    archived: 's-archived',
    none: 's-danger'
  }[s] || 's-none'
}
function fmtQty(v) {
  const n = Number(v)
  if (!Number.isFinite(n)) return '1'
  return Number.isInteger(n) ? String(n) : String(n)
}
function hhmmss() {
  const d = new Date()
  return [d.getHours(), d.getMinutes(), d.getSeconds()]
    .map((n) => String(n).padStart(2, '0'))
    .join(':')
}

function toNode(n, prefix) {
  const k = n.id ? `c:${n.id}` : `${prefix}#${n.part}`
  return {
    k,
    label: n.part,
    type: 'combo',
    raw: n,
    children: (n.children || []).map((c) => toNode(c, k))
  }
}

function collectKeys(nodes, out = []) {
  for (const n of nodes) {
    out.push(n.k)
    if (n.children && n.children.length) collectKeys(n.children, out)
  }
  return out
}

const shownProducts = computed(() => treeData.value)
// 顶部「N 件」：只数真件，不数产品行
const shownCount = computed(() => {
  let n = 0
  const walk = (nodes) => {
    for (const x of nodes) {
      if (x.type === 'combo' && x.raw && x.raw.id) n += 1
      if (x.children) walk(x.children)
    }
  }
  walk(treeData.value)
  return n
})

async function loadTree() {
  treeLoading.value = true
  try {
    // 结构只认 parent_id，必须用 structure=1；默认的按件名聚类是展示层假分组，
    // 在假分组上按键没有可写的目标。
    const d = await api.comboTrees({ structure: 1 })
    const list = d.products || []
    products.value = list
    allTrees.value = list.map((p) => {
      const k = `p:${p.product || '(空)'}`
      const roots = p.roots || []
      const top = p.virtual_root && roots.length === 1 ? roots[0].children || [] : roots
      return {
        k,
        label: p.product || '(未指定产品)',
        type: 'product',
        raw: {
          ...p,
          subtotal_hours: p.virtual_root && roots.length === 1 ? roots[0].subtotal_hours : null
        },
        children: top.map((n) => toNode(n, k))
      }
    })
    if (!expanded.value.length) {
      expanded.value = allTrees.value.map((n) => n.k)
    }
  } finally {
    treeLoading.value = false
  }
}

// 「全部展开 / 全部折叠」以前是 `treeKey += 1`，等于把整棵 el-tree 连同
// 541 个 el-tree-node 一起销毁重建 —— 展开本身只是改个布尔值，没理由付这个代价。
// `default-expanded-keys` 只在 store 构造时读一次（没有 watch），所以改
// `expanded` 并不会生效，必须直接驱动节点自己的 expand() / collapse()。
//
// 但光去掉重建还不够：541 个节点一次性渲染是 660ms 的单个长任务，期间页面
// 完全不响应，手感就是「点一下卡一秒」。所以再按帧切片：每帧只做一小批，
// 让浏览器有机会绘制和响应输入。总时长没变短，但再也没有卡死的那一下。
// 每批展开几棵子树。结构基本是两层（产品 → 件），一个产品节点下面挂 50 多个件，
// 所以这个数的单位是「子树」不是「行」。
// 调参实测（541 个节点）：一次全做 = 660ms 长任务，页面钉死；
// 1 棵/批 = 不卡但总时长 2.4s（每批都要重绘）；3 棵/批 = 总时长约 0.9s，
// 单批约 150ms，是「不卡死」和「别太慢」之间较合适的点。
const BULK_STEP = 3
let bulkToken = 0

// 让出主线程。**不能用 nextTick**：它是微任务，整条 await 链会在同一个事件处理
// 区间里跑完，浏览器照样一次卡到底。也尽量不用 setTimeout(0)：嵌套调用会被
// 钳到 4ms，50 批就是 200ms 的无谓等待。MessageChannel 是宏任务但不受节流，
// 能让出又不拖慢总时长。
const _chan = typeof MessageChannel !== 'undefined' ? new MessageChannel() : null
const _waiting = []
if (_chan) {
  _chan.port1.onmessage = () => {
    const resolve = _waiting.shift()
    if (resolve) resolve()
  }
}
function yieldTask() {
  if (!_chan) return new Promise((resolve) => setTimeout(resolve, 0))
  return new Promise((resolve) => {
    _waiting.push(resolve)
    _chan.port2.postMessage(0)
  })
}

// 树要重建时先把当前展开状态记下来。以前是直接 `treeKey += 1`，重建后
// 回到「只展开产品」，用户刚点开的分支会莫名收回去。
function rebuildTree() {
  const map = treeRef.value && treeRef.value.store && treeRef.value.store.nodesMap
  if (map) {
    const keys = Object.keys(map).filter((k) => {
      const n = map[k]
      return n && n.expanded && n.childNodes && n.childNodes.length
    })
    if (keys.length) expanded.value = keys
  }
  treeKey.value += 1
}

async function applyExpandAll(val) {
  const store = treeRef.value && treeRef.value.store
  const map = store && store.nodesMap
  if (!map) {
    // 树还没渲染出来（或 el-tree 内部结构变了）：退回重建，慢但不会错
    expanded.value = val ? collectKeys(treeData.value) : []
    treeKey.value += 1
    return
  }

  const nodes = Object.keys(map)
    .map((k) => map[k])
    .filter((n) => n && n.childNodes && n.childNodes.length)
  if (!nodes.length) return

  // 展开折叠都「由外向内」：外层先动，内层被收起后不再参与布局。
  nodes.sort((a, b) => a.level - b.level)

  // 折叠不分批：它不创建任何节点，只是一次性改 visibility，浏览器把所有改动
  // 合并成一次重排。切成小批反而每批都逼浏览器重排一遍 541 行 —— 实测从
  // 0.4 秒恶化到 1 秒，方向是反的。
  if (!val) {
    for (const n of nodes) n.collapse()
    return
  }

  const token = ++bulkToken
  for (let i = 0; i < nodes.length; i += BULK_STEP) {
    if (token !== bulkToken) return // 中途又点了一次：这一轮作废
    for (const n of nodes.slice(i, i + BULK_STEP)) n.expand()
    await yieldTask()
  }
  // 注意：这里刻意不改 expanded。el-tree 的节点在**新建时**会读
  // `default-expanded-keys`，一旦提前把 541 个 key 写进去，展开第一个产品就会
  // 顺着它把整棵子树递归展开 —— 分批等于没分。展开状态由 rebuildTree() 采集。
}

function expandAll() {
  applyExpandAll(true)
}

function collapseAll() {
  applyExpandAll(false)
}

function nodePath(nd) {
  const out = []
  let cur = nd
  while (cur) {
    if (cur.data && cur.data.type === 'combo') out.unshift(cur.data.label)
    cur = cur.parent
  }
  return out
}

function findKeyPath(nodes, key, trail = []) {
  for (const n of nodes) {
    if (n.k === key) return [...trail, n.k]
    const hit = findKeyPath(n.children || [], key, [...trail, n.k])
    if (hit) return hit
  }
  return null
}

function scrollToKey(key) {
  const el = document.querySelector(`.sp-tree [data-k="${key}"]`)
  if (el && el.scrollIntoView) el.scrollIntoView({ block: 'center' })
}

function onNodeClick(data) {
  if (data.type !== 'combo' || data.raw.virtual || !data.raw.id) {
    focusKey.value = data.k
    focusRow.value = null
    focusPath.value = []
    return
  }
  focusKey.value = data.k
  focusRow.value = data.raw
  const nd = treeRef.value?.getNode(data.k)
  focusPath.value = nd ? nodePath(nd) : [data.label]
}

// 改完把树刷一遍，再展开到原来那一行 —— 不然刚挪完的件跑到折叠区域里，
// 人会以为它消失了。
async function refreshStructure(keepKey = focusKey.value) {
  await loadTree()
  if (!keepKey) return
  const path = findKeyPath(treeData.value, keepKey)
  if (!path) {
    focusRow.value = null
    focusPath.value = []
    focusKey.value = ''
    return
  }
  const set = new Set(expanded.value)
  path.forEach((k) => set.add(k))
  expanded.value = [...set]
  treeKey.value += 1
  await nextTick()
  scrollToKey(keepKey)
  const nd = treeRef.value?.getNode(keepKey)
  if (nd) {
    focusKey.value = keepKey
    focusRow.value = nd.data.type === 'combo' ? nd.data.raw : null
    focusPath.value = nodePath(nd)
  }
}

function moveTip(r, action) {
  if (action === 'indent') return `「${r.part}」已挂到「${r.parent_part}」下`
  if (action === 'outdent') {
    return r.parent_part ? `「${r.part}」退到「${r.parent_part}」下` : `「${r.part}」已回到最上层`
  }
  return action === 'up' ? `「${r.part}」上移` : `「${r.part}」下移`
}

async function move(row, action) {
  if (!row || !row.part) return
  if (!row.product) {
    ElMessage.warning('产品名为空，结构没有归属可谈；先改名补上产品')
    return
  }
  if (moving.value) return
  moving.value = true
  try {
    const r = await api.comboMove({ product: row.product, part: row.part, action })
    const tip = moveTip(r, action)
    if (r.undo && r.undo.length) {
      undoStack.value.push({ product: row.product, rows: r.undo, selfId: r.id, tip })
      if (undoStack.value.length > UNDO_MAX) undoStack.value.shift()
    }
    lastSaved.value = { at: hhmmss(), tip }
    ElMessage({ message: tip, type: 'success', duration: 1000 })
    focusKey.value = `c:${r.id ?? row.id}`
    await refreshStructure(focusKey.value)
    emit('changed')
  } catch (e) {
    ElMessage.warning(e.detailText || e.message || '这一步挪不动')
  } finally {
    moving.value = false
  }
}

async function undoMove() {
  const step = undoStack.value[undoStack.value.length - 1]
  if (!step || moving.value) return
  moving.value = true
  try {
    const r = await api.comboUndoMove({ product: step.product, rows: step.rows })
    undoStack.value.pop()
    const name = (r.parts && r.parts[0]) || ''
    lastSaved.value = { at: hhmmss(), tip: `已撤回上一步${name ? `（${name}）` : ''}` }
    ElMessage({ message: '已撤回上一步，数据已恢复', type: 'success', duration: 1400 })
    if (step.selfId) {
      focusKey.value = `c:${step.selfId}`
      await refreshStructure(focusKey.value)
    } else {
      await refreshStructure('')
    }
    emit('changed')
  } catch (e) {
    ElMessage.error(e.detailText || e.message || '撤回失败，这一步可能已经被别处改过')
  } finally {
    moving.value = false
  }
}

async function editQty(row) {
  let value = ''
  try {
    const res = await ElMessageBox.prompt('装一台上级用几件', `「${row.part}」每台用量`, {
      inputValue: String(row.qty_per_unit ?? 1),
      inputPattern: /^\d+(\.\d+)?$/,
      inputErrorMessage: '填正数'
    })
    value = res.value
  } catch (e) {
    return
  }
  try {
    await api.comboSetLevel({
      product: row.product,
      part: row.part,
      qty_per_unit: Number(value)
    })
    ElMessage.success(`每台用量改为 ${value}`)
    await refreshStructure(focusKey.value)
    emit('changed')
  } catch (e) {
    ElMessage.error(e.detailText || e.message || '修改失败')
  }
}

// ------------------------------------------------------------------ 计划生产数量
// 和「每台用量」是两件事：用量是设计定额（装一台用几件），计划量是这一批做几件。
// 改任何一个，整棵树都会重新联动，所以改完必须重刷树 —— 否则人看到的还是旧数。
function planOff(row) {
  if (!row || row.plan_qty == null || row.plan_expect == null) return false
  if (row.plan_src !== 'manual') return false
  return Math.abs(Number(row.plan_qty) - Number(row.plan_expect)) > 1e-9
}

async function editPlan(row) {
  let value = ''
  try {
    const res = await ElMessageBox.prompt(
      '这一批做几件。\n改上级 → 下级按「每台用量」自动重算；\n改下级 → 上级按用量反推（除不尽向上取整）。',
      `「${row.part}」计划生产数量`,
      {
        inputValue: String(row.plan_qty ?? 1),
        inputPattern: /^\d+(\.\d+)?$/,
        inputErrorMessage: '填正数'
      }
    )
    value = res.value
  } catch (e) {
    return
  }
  try {
    const r = await api.comboSetPlan({
      product: row.product,
      part: row.part,
      plan_qty: Number(value)
    })
    if ((r.warnings || []).length) {
      ElMessageBox.alert(r.warnings.join('\n'), '数量已联动，请确认', {
        type: 'warning',
        confirmButtonText: '知道了'
      }).catch(() => {})
    } else {
      ElMessage.success(`「${row.part}」计划 ${value} 件${r.updated > 1 ? `，连带调整 ${r.updated - 1} 件` : ''}`)
    }
    await refreshStructure(focusKey.value)
    emit('changed')
  } catch (e) {
    ElMessage.error(e.detailText || e.message || '修改失败')
  }
}

// ------------------------------------------------------------------ 新增结构件
const nodeVisible = ref(false)
const nodeBusy = ref(false)
const nodeForm = ref({
  product: '',
  part: '',
  parent_part: '',
  qty_per_unit: 1,
  planManual: false,
  plan_qty: 1
})

// 上级候选：只列当前产品里的件，按层级缩进，选的时候能看出挂在哪一支
const nodeParentOptions = computed(() => {
  const t = allTrees.value.find((x) => (x.raw.product || '') === nodeForm.value.product)
  const out = []
  const walk = (ns, depth) => {
    for (const n of ns || []) {
      out.push({ value: n.label, label: `${'　'.repeat(depth)}${n.label}` })
      walk(n.children, depth + 1)
    }
  }
  walk(t ? t.children : [], 0)
  return out
})

function openCreateNode(parentPart = '') {
  const product = picked.value || props.product || focusRow.value?.product || ''
  nodeForm.value = {
    product,
    part: '',
    parent_part: parentPart || '',
    qty_per_unit: 1,
    planManual: false,
    plan_qty: 1
  }
  nodeVisible.value = true
}

async function submitNode() {
  const f = nodeForm.value
  if (!f.product) return ElMessage.warning('先选产品：结构必须归属某个产品')
  const part = (f.part || '').trim()
  if (!part) return ElMessage.warning('件名要填')
  nodeBusy.value = true
  try {
    const payload = {
      product: f.product,
      part,
      parent_part: f.parent_part || '',
      qty_per_unit: Number(f.qty_per_unit) || 1
    }
    // 不勾「手填」就别传：让后端按「上级计划量 × 每台用量」派生，
    // 传一个数字过去等于把这件标成人工设定，之后上级变它不跟着变。
    if (f.planManual) payload.plan_qty = Number(f.plan_qty) || 0
    const r = await api.comboCreateNode(payload)
    ElMessage.success(`已加进结构：${f.product} / ${part}`)
    nodeVisible.value = false
    rebuildTree()
    await refreshStructure(`c:${r.node.id}`)
    emit('changed')
  } catch (e) {
    ElMessage.error(e.detailText || e.message || '新增失败')
  } finally {
    nodeBusy.value = false
  }
}

async function removeNode(row) {
  try {
    await ElMessageBox.confirm(
      `从结构里删掉「${row.part}」？\n有下级、有工艺路线、有订单引用或有报工明细的件会被拦住 —— 删掉它们会让工时和工资指向一个不存在的位置。`,
      '删除结构件',
      { type: 'warning', confirmButtonText: '删除' }
    )
  } catch (e) {
    return
  }
  try {
    await api.comboDeleteNode(row.product, row.part)
    ElMessage.success(`已从结构里删掉「${row.part}」`)
    await refreshStructure('')
    emit('changed')
  } catch (e) {
    ElMessage.error(e.detailText || e.message || '删除失败')
  }
}

// ------------------------------------------------------------------ 编制工序
// 结构里编的是「全厂标准工艺」（产品 × 件名），不是某个订单的工艺；
// 与「生产任务 → 编辑工序」共用同一张表（StepTable），但存取各走各的接口。
const routeVisible = ref(false)
const routeTarget = ref({ product: '', part: '', routeId: null })

function openRoute(row) {
  if (!row || !row.part) return
  // 虚拟节点是树的合成容器，不是库里的件，没有工艺可编
  if (row.virtual) {
    ElMessage.info('这是合成行，不对应库里的件，不能编工艺')
    return
  }
  routeTarget.value = {
    product: row.product || '',
    part: row.part,
    routeId: row.route_id || null
  }
  routeVisible.value = true
}

// 编完工序要重刷结构树：状态（无工艺/草稿/已发布）、道数、单件工时都在节点上
async function onRouteSaved() {
  await refreshStructure(focusKey.value)
  emit('changed')
}

async function doRelayout() {
  try {
    await ElMessageBox.confirm(
      '按件名规则重铺：件名像「xx组件 / xx总成 / xx总包」的当部件挂到总装下，' +
        '其余零件按名字前缀归到对应的部件里；没认出来的留在总装下。\n\n' +
        '只会动「自动」的行，人工设过的不会被冲掉。',
      '按件名重铺结构',
      { type: 'warning', confirmButtonText: '开始重铺' }
    )
  } catch (e) {
    return
  }
  const r = await api.comboRelayout(picked.value || '')
  const comps = (r.detail || []).reduce((s, x) => s + (x.components || 0), 0)
  const leaves = (r.detail || []).reduce((s, x) => s + (x.leaves || 0), 0)
  ElMessage.success(`已重铺 ${r.products} 个产品：部件 ${comps} 个、零件 ${leaves} 个`)
  await refreshStructure('')
  emit('changed')
}

async function openAutoNest() {
  autoVisible.value = true
  autoLoading.value = true
  autoPick.value = []
  try {
    const d = await api.comboAutoNest({ product: picked.value || '', dry_run: true })
    autoPre.value = d
    autoPick.value = (d.items || []).map((_, i) => i)
  } catch (e) {
    ElMessage.error(e.detailText || e.message || '预演失败')
  } finally {
    autoLoading.value = false
  }
}

async function applyAutoNest() {
  const parts = autoPick.value.map((i) => autoPre.value.items[i]?.part).filter(Boolean)
  if (!parts.length) {
    ElMessage.warning('一件都没勾')
    return
  }
  autoApplying.value = true
  try {
    const r = await api.comboAutoNest({
      product: picked.value || '',
      dry_run: false,
      parts
    })
    autoVisible.value = false
    ElMessage.success(`已归位 ${r.count} 件`)
    await refreshStructure('')
    emit('changed')
  } catch (e) {
    ElMessage.error(e.detailText || e.message || '归位失败')
  } finally {
    autoApplying.value = false
  }
}

const allPicked = computed({
  get: () => !!autoPre.value.items.length && autoPick.value.length === autoPre.value.items.length,
  set: (v) => {
    autoPick.value = v ? autoPre.value.items.map((_, i) => i) : []
  }
})

const applyCount = computed(() => autoPick.value.length)

function onKeydown(e) {
  if (moving.value) return
  const t = e.target
  if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return

  if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && String(e.key).toLowerCase() === 'z') {
    if (!undoStack.value.length) return
    e.preventDefault()
    undoMove()
    return
  }

  if (!focusRow.value) return
  if (e.ctrlKey || e.metaKey || e.altKey || e.shiftKey) return
  const act = { ArrowLeft: 'outdent', ArrowRight: 'indent', ArrowUp: 'up', ArrowDown: 'down' }[e.key]
  if (!act) return
  e.preventDefault()
  move(focusRow.value, act)
}

onMounted(async () => {
  picked.value = props.product || ''
  await loadTree()
  window.addEventListener('keydown', onKeydown)
  // 从清单点「结构」进来时，直接定位到那一件：人不用在整棵树里再找一遍
  if (props.focusPart) {
    const hit = findNodeByPart(treeData.value, props.focusPart)
    if (hit) {
      picked.value = hit.product
      await nextTick()
      await refreshStructure(`c:${hit.id}`)
    }
  }
})

onUnmounted(() => window.removeEventListener('keydown', onKeydown))

function findNodeByPart(nodes, part) {
  for (const n of nodes) {
    if (n.type === 'combo' && n.raw && n.raw.part === part && n.raw.id) {
      return { id: n.raw.id, product: n.raw.product }
    }
    const hit = findNodeByPart(n.children || [], part)
    if (hit) return hit
  }
  return null
}
</script>

<template>
  <div class="sp">
    <div class="row-flex sp-tools">
      <el-select
        v-model="picked"
        size="small"
        style="width: 136px"
        placeholder="全部产品"
        filterable
        clearable
      >
        <el-option value="" label="全部产品" />
        <el-option
          v-for="p in products"
          :key="p.product"
          :value="p.product"
          :label="p.product || '(未指定产品)'"
        />
      </el-select>
      <span class="muted sp-count">{{ shownCount }} 件 · {{ shownProducts.length }} 个产品</span>
      <div class="spacer"></div>
      <el-button size="small" text @click="expandAll">全部展开</el-button>
      <el-button size="small" text @click="collapseAll">全部折叠</el-button>
      <el-button size="small" text @click="doRelayout">按件名重铺</el-button>
      <el-button size="small" text type="primary" @click="openCreateNode()">＋ 新增件</el-button>
      <el-button
        size="small"
        type="primary"
        plain
        title="按件名规则把零件挂到对应的部件下：先预演，逐条勾选后再应用"
        @click="openAutoNest"
      >
        一键归位
      </el-button>
    </div>
    <div class="sp-hint muted">
      点中一件后：<b class="kbd">←</b> 退一级 · <b class="kbd">→</b> 挂到上面那件里 ·
      <b class="kbd">↑</b> <b class="kbd">↓</b> 调同层顺序。
      <b class="tip-key">按下就已经存进库了，没有「保存」按钮</b>；按错了
      <b class="kbd">Ctrl</b>+<b class="kbd">Z</b> 撤回。
    </div>

    <div class="sp-focus">
      <template v-if="focusRow">
        <span class="muted">当前</span>
        <b>{{ (focusPath || []).join(' › ') }}</b>
        <span class="muted">· 第 {{ focusRow.level }} 层 · 每台用量 {{ fmtQty(focusRow.qty_per_unit) }}</span>
      </template>
      <span v-else class="muted">还没点中哪一件 —— 点一行再用方向键</span>
      <div class="spacer"></div>
      <span v-if="moving" class="sv-chip saving">保存中…</span>
      <span v-else-if="lastSaved" class="sv-chip done">
        <b>✓ 已保存 {{ lastSaved.at }}</b>
        <span class="sv-tip">{{ lastSaved.tip }}</span>
        <el-button v-if="undoStack.length" size="small" text type="primary" class="sv-undo" @click="undoMove">
          撤销{{ undoStack.length > 1 ? `（${undoStack.length}）` : '' }}
        </el-button>
      </span>
    </div>

    <div class="sp-wrap" v-loading="treeLoading">
      <el-tree
        :key="treeKey"
        ref="treeRef"
        :data="treeData"
        :props="{ label: 'label', children: 'children' }"
        node-key="k"
        :default-expanded-keys="expanded"
        :expand-on-click-node="false"
        class="sp-tree"
        @node-click="onNodeClick"
      >
        <template #default="{ data }">
          <!-- 一行 = 一个定宽栅格：名称(自适应) / 状态 / 工时 / 数量 / 计划 / 操作键。
               el-tree 的缩进加在 .tn 外面的 content 上，.tn 的右边界所有层级都相同，
               所以后四列在任何层级都落在同一条竖线上 —— 缩进只挤名称列，
               件名再长也只变省略号，不会把「3 道·1.2h」「×2」「计划 12」顶得东倒西歪。
               空格子必须占位（tn-blank）：少一个格子后面的列就会整体左移。 -->
          <div
            class="tn"
            :class="{ 'tn-product': data.type === 'product', 'tn-focus': focusKey === data.k }"
            :data-k="data.k"
          >
            <span class="tn-name" :title="data.label">{{ data.label }}</span>

            <span
              v-if="data.type === 'product'"
              class="tstate s-none tn-state"
              :title="`共 ${data.raw.total} 项结构件，其中 ${data.raw.released} 项已有已发布工艺`"
            >
              {{ data.raw.total }} 项 / 已发 {{ data.raw.released }}
            </span>
            <span v-else class="tstate tn-state" :class="stateCls(data.raw.status)">
              {{ stateText(data.raw.status) }}
            </span>

            <!-- 工时列就是工艺的结果：点它直接开会编工序。没编过的件在这里
                 显示「＋ 编工艺」，比只留一片空白更好找 —— 空白说不出该干什么。 -->
            <button
              v-if="data.type === 'combo' && data.raw.id"
              class="tn-hrs tn-hrs-btn mono"
              :class="{ 'no-proc': !data.raw.step_count }"
              :title="data.raw.step_count
                ? `这一版有 ${data.raw.step_count} 道工序，单件 ${(data.raw.unit_sum || 0).toFixed(2)}h；点开查看/编制`
                : '这个件还没编工艺，点开编制'"
              @click.stop="openRoute(data.raw)"
            >
              <template v-if="data.raw.step_count">
                {{ data.raw.step_count }} 道 · {{ (data.raw.unit_sum || 0).toFixed(2) }}h
              </template>
              <template v-else>＋ 编工艺</template>
            </button>
            <span v-else class="tn-hrs mono">
              {{ data.raw.subtotal_hours ? `含子件 ${data.raw.subtotal_hours.toFixed(2)}h` : '' }}
            </span>

            <span
              v-if="data.type === 'combo'"
              class="tn-qty mono"
              title="每台用量，点击修改"
              @click.stop="editQty(data.raw)"
            >×{{ fmtQty(data.raw.qty_per_unit) }}</span>
            <span v-else class="tn-blank"></span>

            <span v-if="data.type === 'combo' && data.raw.plan_qty != null" class="tn-plan-slot">
              <span
                class="tn-plan mono"
                :class="{ off: planOff(data.raw) }"
                :title="planOff(data.raw)
                  ? `人工设定的计划量；按当前上级与每台用量应为 ${fmtQty(data.raw.plan_expect)} 件`
                  : '计划生产数量（这一批做几件），点击修改'"
                @click.stop="editPlan(data.raw)"
              >
                计划 {{ fmtQty(data.raw.plan_qty) }}
              </span>
            </span>
            <span v-else class="tn-blank"></span>

            <span v-if="data.type === 'combo' && data.raw.id" class="tn-keys">
              <!-- 原生 button，不用 el-button：这里是树节点插槽，每个节点都有一份，
                   7 个 el-button × 541 个节点 = 3787 个组件实例（还只是常驻的）。
                   opacity:0 不省任何东西，组件照样 instantiate。 -->
              <button class="tn-btn tn-proc" title="编制/查看工序（这条件的工艺路线）" @click.stop="openRoute(data.raw)">✎</button>
              <button class="tn-btn" title="在这件下面加一个子件" @click.stop="openCreateNode(data.raw.part)">＋</button>
              <button class="tn-btn" title="退一级（←）" @click.stop="move(data.raw, 'outdent')">←</button>
              <button class="tn-btn" title="挂到上面那件里（→）" @click.stop="move(data.raw, 'indent')">→</button>
              <button class="tn-btn" title="上移（↑）" @click.stop="move(data.raw, 'up')">↑</button>
              <button class="tn-btn" title="下移（↓）" @click.stop="move(data.raw, 'down')">↓</button>
              <button class="tn-btn tn-del" title="从结构里删掉这件" @click.stop="removeNode(data.raw)">✕</button>
            </span>
            <span v-else class="tn-blank"></span>
          </div>
        </template>
      </el-tree>
      <el-empty v-if="!treeLoading && !shownProducts.length" :image-size="70" description="没有结构数据" />
    </div>

    <!-- 新增结构件：结构原来只能靠迁移和按件名重铺生成，
         工艺员想补一个漏登记的件，除了改库没有别的方法 -->
    <el-dialog v-model="nodeVisible" title="新增结构件" width="min(540px, 96vw)" append-to-body>
      <el-form label-width="88px" size="small">
        <el-form-item label="产品" required>
          <el-select
            v-model="nodeForm.product"
            filterable
            placeholder="选产品"
            style="width: 100%"
            @change="nodeForm.parent_part = ''"
          >
            <el-option
              v-for="p in products"
              :key="p.product"
              :value="p.product"
              :label="p.product || '(未指定产品)'"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="件名" required>
          <el-input v-model="nodeForm.part" placeholder="照图纸写，前后不要空格" maxlength="128" />
        </el-form-item>
        <el-form-item label="挂到">
          <el-select
            v-model="nodeForm.parent_part"
            filterable
            clearable
            placeholder="不选 = 最上层"
            style="width: 100%"
          >
            <el-option v-for="p in nodeParentOptions" :key="p.value" :value="p.value" :label="p.label" />
          </el-select>
        </el-form-item>
        <el-form-item label="每台用量">
          <el-input-number
            v-model="nodeForm.qty_per_unit"
            :min="0.01"
            :step="1"
            :precision="2"
            :controls="false"
            style="width: 110px"
          />
          <span class="muted" style="margin-left: 8px">装一台上级用几件</span>
        </el-form-item>
        <el-form-item label="计划数量">
          <el-checkbox v-model="nodeForm.planManual">手填</el-checkbox>
          <el-input-number
            v-if="nodeForm.planManual"
            v-model="nodeForm.plan_qty"
            :min="0"
            :step="1"
            :controls="false"
            style="width: 110px; margin-left: 8px"
          />
          <div class="muted node-hint">
            不勾「手填」= 按「上级计划量 × 每台用量」自动算，上级变了它跟着变；
            勾了就是人工设定，上级再变这一件不动。
          </div>
        </el-form-item>
      </el-form>
      <div class="muted node-tip">
        这里只建结构。要不要给它编工艺是下一步的事 —— 结构里有、工艺里没有是正常状态，
        台账会显示「无工艺」。
      </div>
      <template #footer>
        <el-button size="small" @click="nodeVisible = false">取消</el-button>
        <el-button size="small" type="primary" :loading="nodeBusy" @click="submitNode">加进结构</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="autoVisible" title="按件名一键归位（预演）" width="min(720px, 96vw)" append-to-body>
      <div v-loading="autoLoading" class="an-body">
        <div class="an-rule muted">
          规则只有一条：件名以另一个件名开头（至少两个字）就认为装在那件里，取最长前缀。
          <b>认不出来的不猜，留在原处</b>；下面逐条勾选，不勾的那条不动。
        </div>
        <div v-if="autoPre.count" class="an-sum">
          将调整 <b>{{ autoPre.count }}</b> 件，涉及 {{ autoPre.products }} 个产品
          <span v-if="autoPre.skipped && autoPre.skipped.length" class="muted">
            · {{ autoPre.skipped.length }} 件因成环等原因跳过
          </span>
        </div>
        <div v-if="autoPre.count" class="an-list">
          <div class="an-row an-head">
            <span><el-checkbox v-model="allPicked" /></span>
            <span>产品</span><span>件名</span><span>挂到</span><span>原来</span>
          </div>
          <div v-for="(it, i) in autoPre.items" :key="i" class="an-row">
            <span><el-checkbox v-model="autoPick" :value="i" /></span>
            <span class="muted ell">{{ it.product }}</span>
            <span class="ell">{{ it.part }}</span>
            <span class="ok ell">{{ it.parent }}</span>
            <span class="muted ell">{{ it.from || '最上层' }}</span>
          </div>
        </div>
        <el-empty v-else :image-size="60" description="没有可按件名归位的件（都已在正确位置）" />
      </div>
      <template #footer>
        <el-button size="small" @click="autoVisible = false">取消</el-button>
        <el-button size="small" @click="openAutoNest">重新预演</el-button>
        <el-button size="small" type="primary" :disabled="!applyCount" :loading="autoApplying" @click="applyAutoNest">
          应用（{{ applyCount }} 件）
        </el-button>
      </template>
    </el-dialog>

    <!-- 编制工序：在这件上就地编工艺，不用跳去编排页再找一遍 -->
    <PartRouteDialog
      v-model="routeVisible"
      :product="routeTarget.product"
      :part="routeTarget.part"
      :route-id="routeTarget.routeId"
      @saved="onRouteSaved"
    />
  </div>
</template>

<style scoped>
.sp { display: flex; flex-direction: column; font-size: 13px; }
/* 工具条一行排完：窗口缩到 730px 后，220px 的下拉 + 5 个按钮会折成两行，
   白白吃掉 34px 高度。窄一点的下拉 + 取消按钮之间的双倍间距（gap 已给 6px，
   el-button 默认还有 margin-left: 12px）刚好一行放下。 */
.sp-tools { margin-bottom: 6px; gap: 6px; }
.sp-tools :deep(.el-button + .el-button) { margin-left: 0; }
.sp-count { font-size: 12px; white-space: nowrap; }
.sp-hint {
  font-size: 12px;
  line-height: 1.9;
  padding: 0 2px 8px;
  white-space: normal;
}
.tip-key { color: #215fbf; }
.kbd {
  display: inline-block;
  min-width: 15px;
  padding: 0 4px;
  border: 1px solid #d3dae6;
  border-bottom-width: 2px;
  border-radius: 4px;
  background: #fbfcfe;
  font-family: ui-monospace, Menlo, Consolas, monospace;
  font-size: 11px;
  line-height: 16px;
  text-align: center;
  color: #4a5568;
}

.sp-focus {
  display: flex;
  align-items: baseline;
  gap: 6px;
  /* 路径长了允许折行：窗口窄了以后「总装 › 部件 › 子件 · 第 3 层 · 每台用量 2」
     加上右侧的已保存回执，一行放不下时不要硬挤出去 */
  flex-wrap: wrap;
  font-size: 12px;
  padding: 5px 10px;
  margin-bottom: 8px;
  background: #f2f6ff;
  border: 1px solid #e2ebfb;
  border-radius: 6px;
}
.sp-focus b { color: #215fbf; font-size: 12.5px; }
.sv-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 11.5px;
  padding: 1px 8px;
  border-radius: 10px;
  white-space: nowrap;
}
.sv-chip.done { background: #e9f7ef; color: #1f7a4d; border: 1px solid #cbe9d8; }
.sv-chip.done b { color: #1f7a4d; font-size: 11.5px; }
.sv-chip.saving { background: #f2f5fa; color: #5a6474; border: 1px solid #e2e8f2; }
.sv-tip { max-width: 300px; overflow: hidden; text-overflow: ellipsis; color: #3d6b52; }
.sv-undo { padding: 0 2px !important; height: auto !important; font-size: 11.5px !important; }

/* scrollbar-gutter: stable 是这里必须的：
   容器高度固定，默认展开是 20 个节点（内容比容器高、有滚动条），
   「全部折叠」后内容变矮、滚动条消失 → 内容区一下多出 15px，
   整棵树向右跳一格。槽位常驻后展开/折叠宽度完全一致。
   高度按原 58vh/560px 缩 30%：窗口窄了，内容区还那么高就像根烟囱。 */
.sp-wrap {
  height: min(41vh, 392px);
  overflow: auto;
  scrollbar-gutter: stable;
  border: 1px solid #eef1f6;
  border-radius: 8px;
  padding: 4px 2px;
}
.sp-tree { font-size: 12.5px; }
.sp-tree :deep(.el-tree-node__content) { height: 30px; }

/* 一行一栅格：名称吃剩余宽度，后四列定宽。
   缩进由 el-tree 加在 content 上，.tn 右边界逐层一致 → 后四列永远同一条竖线。
   最后一列（操作键）也定宽：它是 auto 的话，没有按钮的行会让计划列顶到最右边，
   整列就又歪了。 */
.tn {
  display: grid;
  /* 96 状态 / 92 工时 / 44 数量 / 70 计划 / 140 操作键：
     宽度按实测内容宽度定（截图沙盒量过 scrollWidth），少 1px 就会把
     「12 道 · 12.34h」截成省略号。剩下的全给件名，缩到 730px 窗口时约 150px。
     操作键 7 个（✎ ＋ ← → ↑ ↓ ✕）× 19px ≈ 133px。 */
  grid-template-columns: minmax(0, 1fr) 96px 92px 44px 70px 140px;
  align-items: center;
  column-gap: 6px;
  width: 100%;
  padding-right: 4px;
}
.tn-product > .tn-name { font-weight: 600; color: #1f2733; }
.tn-name { font-size: 12.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
/* 状态列：产品行的「N 项 / 已发 N」比件名的长，超了给省略号，全文在 title 里。
   justify-self: start 让标签只包住文字，不被拉成一条横杠。 */
.tn-state { justify-self: start; max-width: 100%; overflow: hidden; text-overflow: ellipsis; }
.tn-hrs {
  font-size: 11.5px;
  color: #8d96a5;
  text-align: right;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
/* 工时单元格是按钮（点开编工序）：把原生按钮的边框/底色/字号全抹掉，
   只留「一个能点的数字」。padding 归零，不然右侧那列会跟着错位。 */
.tn-hrs-btn {
  border: none;
  background: none;
  padding: 0;
  margin: 0;
  font-family: inherit;
  line-height: inherit;
  cursor: pointer;
  border-radius: 3px;
}
.tn-hrs-btn:hover { color: #1f5fbf; background: #eef4ff; }
/* 没编工艺的件：这儿就是最该被点的东西，不能长得像空白 */
.tn-hrs-btn.no-proc { color: #b8762c; font-weight: 600; }
.tn-hrs-btn.no-proc:hover { color: #8a5a12; background: #fdf3e7; }
.tn-qty { font-size: 11.5px; color: #6b7484; cursor: pointer; text-align: right; }
.tn-qty:hover, .tn-qty.many { color: #c1801f; }
/* 计划数量：可点改，人工值偏离应有数量时标黄（缺件停线比多做几件严重得多） */
.tn-plan-slot { text-align: right; overflow: hidden; }
.tn-plan {
  display: inline-block;
  font-size: 11.5px;
  color: #4b5565;
  cursor: pointer;
  padding: 0 4px;
  border-radius: 3px;
  background: #f1f4f9;
}
.tn-plan:hover { background: #e3ebf7; color: #1f5fbf; }
.tn-plan.off { color: #a8571a; background: #fdf1e4; font-weight: 600; }
/* 占位格子：只负责撑住栅格轨道，不画任何东西 */
.tn-blank { display: block; }
.node-hint { font-size: 12px; line-height: 1.7; margin-top: 2px; }
.node-tip {
  font-size: 12px;
  line-height: 1.7;
  padding: 7px 10px;
  background: #f7f9fc;
  border: 1px solid #eaeef4;
  border-radius: 6px;
}
.tn-focus { background: #eef4ff; border-radius: 5px; box-shadow: inset 0 0 0 1px #b9d1f7; }

/* 状态标签：视觉等价 el-tag(size=small, effect=plain)，零组件实例。
   树是「一节点一插槽」的结构，这里每省一个组件都要乘上 541。 */
.tstate {
  flex: 0 0 auto;
  font-size: 11px;
  line-height: 16px;
  padding: 0 5px;
  border-radius: 4px;
  border: 1px solid;
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}
.s-released { color: #3b6d11; background: #f0f9eb; border-color: #d5ecc0; }
.s-draft    { color: #b8762c; background: #fdf6ec; border-color: #f5dab1; }
.s-archived { color: #85898f; background: #f4f4f5; border-color: #e2e3e6; }
.s-none     { color: #85898f; background: #f4f4f5; border-color: #e2e3e6; }
.s-danger   { color: #c45656; background: #fef0f0; border-color: #fbc4c4; }

/* 行内操作键：hover 才显形，但因为是原生 button，隐藏的行也不花组件成本。
   定宽列里靠右排，6 个键留出一点余量（138px 轨道 / 每个约 21px） */
.tn-keys { display: inline-flex; justify-content: flex-end; gap: 0; opacity: 0; transition: opacity 0.12s; }
.tn:hover .tn-keys, .tn-focus .tn-keys { opacity: 1; }
.tn-btn {
  border: none;
  background: none;
  margin: 0;
  padding: 0 3px;
  height: 20px;
  line-height: 20px;
  font-size: 13px;
  font-family: inherit;
  color: #5a6474;
  cursor: pointer;
  border-radius: 3px;
}
.tn-btn:hover { color: #2f6fd0; background: #e6eefc; }
/* 编工序是最常点的那个：常态就给一点颜色，别等人悬停猜 */
.tn-proc { color: #2f6fd0; }
.tn-btn.tn-del:hover { color: #c0392b; background: #fdf0ef; }

.an-body { font-size: 12.5px; }
.an-rule { font-size: 12px; line-height: 1.7; margin-bottom: 8px; }
.an-sum { margin: 6px 0 8px; }
.an-list { max-height: 320px; overflow: auto; scrollbar-gutter: stable; border: 1px solid #eef1f6; border-radius: 8px; }
.an-row {
  display: grid;
  grid-template-columns: 28px 1.3fr 1.3fr 1.1fr 1.1fr;
  gap: 8px;
  padding: 3px 10px;
  font-size: 12.5px;
  border-bottom: 1px solid #f4f6fa;
  align-items: center;
}
.an-head { background: #f7f9fc; color: #6b7484; position: sticky; top: 0; }
.ell { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ok { color: #1f7a4d; }
</style>
