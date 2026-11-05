<template>
  <el-dialog
    v-model="visible"
    :title="title"
    width="1080px"
    top="6vh"
    append-to-body
    destroy-on-close
    :close-on-click-modal="false"
    class="route-edit-dialog"
  >
    <div v-if="loading" class="muted" style="padding: 24px 0; text-align: center">加载工序…</div>

    <template v-else>
      <!-- 两种路线两种语义，写在最上面：这是最容易改错的地方 -->
      <div class="scope-tip" :class="isBaseline ? 'tip-baseline' : 'tip-copy'">
        <template v-if="isBaseline">
          <b>全厂标准工艺</b>
          <span>
            已发布的标准<b>不直接改</b>，两种保存方式：
            「存为草稿 v{{ (route?.version || 0) + 1 }}」只生成新草稿、现行基准不动；
            「保存并发布」立即切换 <b class="mono">{{ refCount }}</b> 个在制订单到新版本，旧版自动归档留痕。
          </span>
        </template>
        <template v-else>
          <b>本单私有副本</b>
          <span>
            只影响这一个订单，标准工艺不动；来源基线
            <b class="mono">v{{ tr.baseline_version || '—' }}</b>。
          </span>
        </template>
      </div>

      <!-- 工序表是共用组件：任务弹窗和「结构 → 编制工序」用的是同一张表，
           列宽、上下移、重排全在一处，改一次两边一起变 -->
      <StepTable :steps="steps" :process-options="processOptions" />

      <div class="sum-bar">
        <span class="muted">单件合计 <b class="mono">{{ unitSum.toFixed(4) }}</b> h</span>
        <span class="muted">准备合计 <b class="mono">{{ setupSum.toFixed(3) }}</b> h</span>
        <template v-if="baseline">
          <el-divider direction="vertical" />
          <span class="muted">基线 v{{ baseline.version }}：{{ baseline.step_count }} 道 / {{ baselineUnit.toFixed(4) }} h</span>
          <span :class="delta === 0 ? 'ok' : 'warn'">
            当前较基线 <b class="mono">{{ delta > 0 ? '+' : '' }}{{ delta.toFixed(4) }}</b> h
          </span>
        </template>
      </div>
    </template>

    <template #footer>
      <span class="footer-note muted">
        {{ isBaseline ? '改的是全厂标准：可先存草稿，确认无误再发布' : '只改本单，随时可再改' }}
      </span>
      <el-button size="small" @click="visible = false">取消</el-button>
      <template v-if="isBaseline">
        <el-button size="small" :loading="saving" :disabled="loading" @click="save(false)">
          存为草稿 v{{ (route?.version || 0) + 1 }}
        </el-button>
        <el-button
          size="small"
          type="primary"
          :loading="saving"
          :disabled="loading"
          @click="save(true)"
        >
          保存并发布新版本
        </el-button>
      </template>
      <el-button v-else size="small" type="primary" :loading="saving" :disabled="loading" @click="save(true)">
        保存
      </el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api'
import { store } from '../store'
import StepTable from './StepTable.vue'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  taskId: { type: Number, default: null },
  tr: { type: Object, default: null }
})
const emit = defineEmits(['update:modelValue', 'saved'])

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v)
})

const loading = ref(false)
const saving = ref(false)
const route = ref(null)
const baseline = ref(null)
const steps = ref([])
let uid = 0

const isBaseline = computed(() => (props.tr?.origin || 'baseline') !== 'copy')
const refCount = computed(() => Math.max(1, Number(props.tr?.ref_count) || 1))

const title = computed(() => {
  const t = props.tr
  if (!t) return '编辑工艺路线'
  const kind = isBaseline.value ? '标准工艺' : '本单副本'
  return `${t.product || ''} / ${t.part || '—'}　${kind}`
})

const processOptions = computed(() =>
  (store.dicts.processes || []).map((p) => ({ code: p.code, name: p.name, category: p.category }))
)

const unitSum = computed(() => steps.value.reduce((s, r) => s + (Number(r.unit_hours) || 0), 0))
const setupSum = computed(() => steps.value.reduce((s, r) => s + (Number(r.setup_hours) || 0), 0))
const baselineUnit = computed(() => (baseline.value ? Number(baseline.value.unit_sum) || 0 : 0))
const delta = computed(() => (baseline.value ? unitSum.value - baselineUnit.value : 0))

function nextUid() {
  uid += 1
  return uid
}

async function load() {
  if (!props.tr) return
  loading.value = true
  route.value = null
  baseline.value = null
  steps.value = []
  try {
    const mine = await api.route(props.tr.route_id)
    route.value = mine
    steps.value = (mine.steps || []).map((s) => ({ ...s, uid: nextUid() }))
    const bid = props.tr.baseline_id
    if (bid && bid !== props.tr.route_id) {
      try {
        baseline.value = await api.route(bid)
      } catch {
        baseline.value = null
      }
    }
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '工序加载失败')
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

function payload() {
  return steps.value.map((r, i) => ({
    seq: Number(r.seq) || (i + 1) * 10,
    process_code: (r.process_code || '').trim(),
    process_name: (r.process_name || r.process_code || '').trim(),
    op_type: r.op_type || 'self',
    category: (r.category || '').trim(),
    work_center: (r.work_center || '').trim(),
    setup_hours: Number(r.setup_hours) || 0,
    unit_hours: Number(r.unit_hours) || 0,
    is_key: !!r.is_key,
    inspect_required: !!r.inspect_required,
    remark: (r.remark || '').trim()
  }))
}

async function save(publish = true) {
  const rows = payload()
  const blank = rows.findIndex((r) => !r.process_name)
  if (blank >= 0) {
    ElMessage.warning(`第 ${blank + 1} 行还没选工序`)
    return
  }
  if (!rows.length) {
    if (isBaseline.value) {
      ElMessage.warning('标准工艺至少要保留一道工序')
      return
    }
    try {
      await ElMessageBox.confirm('清空后本单这条路线将没有工序，确定？', '清空工序', { type: 'warning' })
    } catch {
      return
    }
  }
  // 只有「保存并发布」才需要确认影响面；存草稿不改任何现行基准，不必吓人
  if (isBaseline.value && publish) {
    try {
      await ElMessageBox.confirm(
        `保存会发布 v${(route.value?.version || 0) + 1}，${refCount.value} 个在制订单的这条路线立即换到新版本（旧版归档）。确定修改标准工艺？`,
        '修改全厂标准',
        { type: 'warning', confirmButtonText: '确定修改' }
      )
    } catch {
      return
    }
  }

  saving.value = true
  try {
    const r = await api.editTaskRouteSteps(props.taskId, props.tr.id, { steps: rows, publish })
    if (r.mode === 'draft') {
      // 草稿不生效：现行基准一个字节都没动，任务仍按旧版核对
      ElMessage.success(r.message || `草稿 v${r.version} 已保存，发布后才生效`)
    } else if (r.mode === 'baseline') {
      if (r.unchanged) ElMessage.info('内容没有变化，未生成新版本')
      else ElMessage.success(`标准已升到 v${r.version}，${r.synced_tasks} 个订单同步更新`)
      // 改的是全厂标准：受影响的是别的订单，必须点名，让他们去确认
      const affected = r.affected_tasks || []
      if (affected.length) {
        ElMessageBox.alert(
          `v${r.from_version} → v${r.version} 已发布。以下订单引用这条标准，` +
            `会亮起「升版待确认」，需要它们各自确认采用新版还是保留现版：\n` +
            affected.map((t) => `· ${t.task_no}（${t.part || '—'}）`).join('\n'),
          '已影响其它订单',
          { type: 'warning', confirmButtonText: '知道了' }
        ).catch(() => {})
      }
    } else {
      ElMessage.success(`本单工艺已保存（${rows.length} 道工序）`)
    }
    emit('saved', r)
    visible.value = false
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.tools { margin-bottom: 8px; }

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
.scope-tip b { font-weight: 600; }
.tip-baseline { background: #fdf3e7; border: 1px solid #f3ddbc; color: #8a5a12; }
.tip-baseline b { color: #7a4a06; }
.tip-copy { background: #f5f8fd; border: 1px solid #e2eaf6; color: #40526b; }
.tip-copy b { color: #14539f; }

.sum-bar {
  display: flex;
  align-items: center;
  gap: 16px;
  margin-top: 8px;
  font-size: 12.5px;
}
.ok { color: #1f8b4c; }
.warn { color: #c0392b; }
.footer-note { font-size: 12px; margin-right: auto; }
</style>
