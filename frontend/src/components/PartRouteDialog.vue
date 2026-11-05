<template>
  <el-dialog
    v-model="visible"
    :title="title"
    width="min(1040px, 94vw)"
    top="6vh"
    append-to-body
    destroy-on-close
    :close-on-click-modal="false"
  >
    <div v-if="loading" class="muted" style="padding: 24px 0; text-align: center">加载工序…</div>

    <template v-else>
      <!-- 状态横幅写在最上面，因为「这一版到底生效不生效」是这里最容易弄错的一件事：
           草稿改了不生效、已发布不能就地改、归档的连改都别想 -->
      <div class="scope-tip" :class="`tip-${mode}`">
        <b>{{ tip.title }}</b>
        <span>{{ tip.text }}</span>
      </div>

      <StepTable :steps="steps" :process-options="processOptions" :readonly="!editable">
        <template #tools>
          <el-button v-if="editable" size="small" @click="fromHistory">从历史生成</el-button>
        </template>
      </StepTable>

      <div class="sum-bar">
        <span class="muted">工序数 <b class="mono">{{ steps.length }}</b></span>
        <span class="muted">单件合计 <b class="mono">{{ unitSum.toFixed(4) }}</b> h</span>
        <span class="muted">准备合计 <b class="mono">{{ setupSum.toFixed(3) }}</b> h</span>
      </div>
    </template>

    <template #footer>
      <span class="footer-note muted">{{ footerNote }}</span>
      <el-button size="small" @click="visible = false">{{ editable ? '取消' : '关闭' }}</el-button>
      <template v-if="mode === 'released'">
        <el-button size="small" type="primary" :loading="busy" @click="startRevision">升版修改</el-button>
      </template>
      <template v-else-if="editable">
        <el-button size="small" :loading="busy" @click="saveDraft">存为草稿</el-button>
        <el-button size="small" type="primary" :loading="busy" @click="saveAndPublish">保存并发布</el-button>
      </template>
    </template>
  </el-dialog>
</template>

<script setup>
/**
 * 单件工序编制：在「产品结构」弹窗里就地给某个件编工艺，不用跳去编排页。
 *
 * 与「生产任务 → 编辑工序」(RouteEditDialog) 的关系：
 *   - **表格是同一张**（StepTable），列、上下移、重排完全一致；
 *   - **弹窗不能复用**：那个弹窗存的是「某个订单下的这条路线」，后端接口是
 *     `PUT /api/tasks/{task}/routes/{tr}/steps`（带着任务、副本/基线两套语义）。
 *     结构里编的是**全厂标准工艺**，没有订单，走的是
 *     `POST/PUT /api/routes` + `/publish` + `/revision`，对象都不一样。
 *     硬把一个套进另一个，只会让「改的是全厂还是本单」这件事更难说清。
 *
 * 状态机（后端语义的镜像，别自己发明）：
 *   无路线      → 新建草稿 → 可发布
 *   draft       → 就地改 → 发布后才生效
 *   released    → **不许就地改**（会改掉历史单据的比对基准）→ 升版出一份新草稿，
 *                 旧版继续生效；新草稿发布时旧版才归档
 *   archived    → 只读，要救回得上编排页「恢复」
 */
import { computed, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api'
import { store } from '../store'
import StepTable from './StepTable.vue'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  product: { type: String, default: '' },
  part: { type: String, default: '' },
  // 结构树节点上带的 route_id（没有就是这件还没编过工艺）
  routeId: { type: Number, default: null }
})
const emit = defineEmits(['update:modelValue', 'saved'])

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v)
})

const loading = ref(false)
const busy = ref(false)
const route = ref(null)
const steps = ref([])
let uid = 0

const nextUid = () => {
  uid += 1
  return `s${uid}`
}

const mode = computed(() => route.value?.status || 'new')
const editable = computed(() => mode.value === 'new' || mode.value === 'draft')

const title = computed(
  () => `${props.product || '—'} / ${props.part || '—'}　工序编制`
)

const processOptions = computed(() =>
  (store.dicts.processes || []).map((p) => ({ code: p.code, name: p.name, category: p.category }))
)

const unitSum = computed(() => steps.value.reduce((s, r) => s + (Number(r.unit_hours) || 0), 0))
const setupSum = computed(() => steps.value.reduce((s, r) => s + (Number(r.setup_hours) || 0), 0))

const tip = computed(() => {
  const v = route.value?.version
  return {
    new: {
      title: '还没编工艺',
      text: '这个件在结构里有位置，但工艺表里还没有它的路线。存草稿只是留底，点「保存并发布」才会成为「产品 × 件名」的核对基准（单据核对、工艺卡都按它比对）。'
    },
    draft: {
      title: `草稿 v${v ?? 1}：还没生效`,
      text: '草稿不参与核对，单据不会按它比对。确认无误后点「保存并发布」。'
    },
    released: {
      title: `已发布 v${v}：这是核对基准`,
      text: '已发布的工艺**不能就地改** —— 改了会动到历史单据的比对基准。要改点「升版修改」：生成一份新草稿，旧版继续生效，直到新草稿发布。'
    },
    archived: {
      title: `已归档 v${v}：只读`,
      text: '这一版已经退出基准，只能看。要恢复或再改，请到「工艺及工时编排」页的版本下拉里撤销归档。'
    }
  }[mode.value]
})

const footerNote = computed(
  () =>
    ({
      new: '新建后是草稿，发布才生效',
      draft: '改草稿随时可改，发布后才生效',
      released: '改的是全厂标准：先升版，再发布',
      archived: '归档版本只读'
    })[mode.value]
)

async function load() {
  loading.value = true
  route.value = null
  steps.value = []
  try {
    let rid = props.routeId
    if (!rid) {
      // 结构树上的 route_id 来自工时组合行，可能是旧的（别处刚建/删过）。
      // 再按产品×件名查一次兜底，避免对着已存在的工艺又建一个新版本。
      const d = await api.routes({ product: props.product, part: props.part, limit: 50 })
      const list = (d.items || [])
        .filter((r) => r.status !== 'archived')
        .sort((a, b) => (b.version || 0) - (a.version || 0))
      rid = list[0]?.id || null
    }
    if (rid) {
      const d = await api.route(rid)
      route.value = d
      steps.value = (d.steps || []).map((s) => ({ ...s, uid: nextUid() }))
    }
  } catch (e) {
    ElMessage.error(e.detailText || e.message || '工序加载失败')
  } finally {
    loading.value = false
  }
}

watch(
  () => props.modelValue,
  (open) => {
    if (open) load()
  }
)

function stepRows() {
  return steps.value.map((r, i) => ({
    seq: Number(r.seq) || (i + 1) * 10,
    process_code: (r.process_code || '').trim(),
    process_name: (r.process_name || r.process_code || '').trim(),
    op_type: r.op_type || 'self',
    category: (r.category || '').trim(),
    work_center: (r.work_center || '').trim(),
    setup_hours: Number(r.setup_hours) || 0,
    unit_hours: Number(r.unit_hours) || 0,
    hours_src: r.hours_src || 'manual',
    samples: Number(r.samples) || 0,
    is_key: !!r.is_key,
    inspect_required: !!r.inspect_required,
    remark: (r.remark || '').trim()
  }))
}

function check(publishing) {
  const rows = stepRows()
  const blank = rows.findIndex((r) => !r.process_name)
  if (blank >= 0) {
    ElMessage.warning(`第 ${blank + 1} 行还没选工序`)
    return null
  }
  if (publishing && !rows.length) {
    ElMessage.warning('空路线不能发布：至少要有一道工序')
    return null
  }
  return rows
}

// 落到库里（新建 or 改草稿），不涉及发布
async function writeDraft(rows) {
  let d
  if (route.value) {
    d = await api.updateRoute(route.value.id, { steps: rows })
  } else {
    d = await api.createRoute({
      product: props.product,
      part: props.part,
      steps: rows,
      created_by: 'operator'
    })
  }
  route.value = d
  steps.value = (d.steps || []).map((s) => ({ ...s, uid: nextUid() }))
  return d
}

async function saveDraft() {
  if (!ensureProduct()) return
  const rows = check(false)
  if (!rows) return
  busy.value = true
  try {
    const d = await writeDraft(rows)
    ElMessage.success(`已存为草稿 v${d.version}，发布后才生效`)
    emit('saved')
    visible.value = false
  } catch (e) {
    ElMessage.error(e.detailText || e.message || '保存失败')
  } finally {
    busy.value = false
  }
}

async function saveAndPublish() {
  if (!ensureProduct()) return
  const rows = check(true)
  if (!rows) return
  try {
    await ElMessageBox.confirm(
      '发布后这条工艺成为「产品 × 件名」的核对基准：单据核对按它比对，工艺卡沿它展开。\n' +
        '引用它的在制订单不会自动认可新版，会各自亮起「升版待确认」。确定发布？',
      '发布为核对基准',
      { type: 'warning', confirmButtonText: '确定发布' }
    )
  } catch {
    return
  }
  busy.value = true
  try {
    await writeDraft(rows)
    const d = await api.publishRoute(route.value.id)
    if ((d.affected_tasks || []).length) {
      ElMessageBox.alert(
        `已发布 v${d.version}（上一版 v${d.previous_version ?? '—'} 归档）。\n` +
          `以下 ${d.affected_count} 个在制订单仍引用旧版，已标记「升版待确认」：\n` +
          d.affected_tasks.slice(0, 8).map((t) => `· ${t.task_no}（${t.part || '—'}）`).join('\n') +
          (d.affected_count > 8 ? `\n… 另有 ${d.affected_count - 8} 单` : '') +
          '\n\n已确认单据需重校验后，告警才会按新基准更新。',
        '升版已生效',
        { type: 'warning', confirmButtonText: '知道了' }
      ).catch(() => {})
    } else {
      ElMessage.success('已发布为核对基准')
    }
    emit('saved')
    visible.value = false
  } catch (e) {
    ElMessage.error(e.detailText || e.message || '发布失败')
  } finally {
    busy.value = false
  }
}

// 已发布 → 升版：生成新草稿，旧版继续当基准（不会出现「没有基准」的空窗）
async function startRevision() {
  busy.value = true
  const oldV = route.value?.version
  try {
    const d = await api.revisionRoute(route.value.id)
    route.value = d
    steps.value = (d.steps || []).map((s) => ({ ...s, uid: nextUid() }))
    ElMessage.success(
      `已生成 v${d.version} 草稿：旧版 v${oldV} 继续生效，改完点「保存并发布」`
    )
    emit('saved')
  } catch (e) {
    ElMessage.error(e.detailText || e.message || '升版失败')
  } finally {
    busy.value = false
  }
}

async function fromHistory() {
  if (!ensureProduct()) return
  try {
    const d = await api.routeSuggest(props.product, props.part)
    if (!d.steps || !d.steps.length) {
      ElMessage.info('这个组合没有历史工时数据，可手工加行')
      return
    }
    steps.value = d.steps.map((s) => ({
      seq: s.seq,
      process_code: s.process_code,
      process_name: s.process_name,
      op_type: s.op_type,
      category: s.category,
      work_center: s.work_center,
      setup_hours: s.setup_hours,
      unit_hours: s.unit_hours,
      hours_src: s.hours_src,
      samples: s.samples,
      is_key: s.is_key,
      inspect_required: s.inspect_required,
      remark: s.remark,
      not_in_dict: s.not_in_dict,
      uid: nextUid()
    }))
    ElMessage.success(`已按历史生成 ${d.steps.length} 道工序，请核对后保存`)
  } catch (e) {
    ElMessage.error(e.detailText || e.message || '历史工时查询失败')
  }
}

function ensureProduct() {
  if (!props.product) {
    ElMessage.warning('这个件没有产品归属，先在结构里补上产品再编工艺')
    return false
  }
  if (!props.part) {
    ElMessage.warning('件名为空，不能编工艺')
    return false
  }
  return true
}
</script>

<style scoped>
.scope-tip {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 12.5px;
  line-height: 1.7;
  border-radius: 6px;
  padding: 8px 12px;
  margin-bottom: 10px;
}
.scope-tip b { font-weight: 600; white-space: nowrap; }
.tip-new { background: #fdf3e7; border: 1px solid #f3ddbc; color: #8a5a12; }
.tip-new b { color: #7a4a06; }
.tip-draft { background: #f5f8fd; border: 1px solid #e2eaf6; color: #40526b; }
.tip-draft b { color: #14539f; }
.tip-released { background: #eef7f0; border: 1px solid #cfe7d5; color: #2f6b45; }
.tip-released b { color: #1f7a4d; }
.tip-archived { background: #f4f5f7; border: 1px solid #e2e3e6; color: #6b7280; }
.tip-archived b { color: #4b5563; }

.sum-bar {
  display: flex;
  align-items: center;
  gap: 16px;
  margin-top: 8px;
  font-size: 12.5px;
}
.footer-note { font-size: 12px; margin-right: auto; }
</style>
