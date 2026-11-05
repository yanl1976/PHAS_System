<template>
  <el-dialog
    v-model="visible"
    width="720px"
    :close-on-click-modal="false"
    class="task-edit-dialog"
  >
    <template #header>
      <div class="te-head">
        <span class="te-title">编辑任务基本信息</span>
        <span v-if="task" class="te-no mono">{{ task.task_no }}</span>
        <el-tag v-if="task" size="small" effect="plain">当前 v{{ task.version || 1 }}</el-tag>
      </div>
    </template>

    <el-form v-if="task" :model="form" label-width="86px" size="small">
      <div class="form-grid">
        <el-form-item label="任务号">
          <el-input v-model="form.task_no" placeholder="全局唯一，撞号会直接报错" />
        </el-form-item>
        <el-form-item label="期间">
          <el-select v-model="form.period_id" placeholder="选择期间" clearable style="width: 100%">
            <el-option
              v-for="p in periods"
              :key="p.id"
              :value="p.id"
              :label="`${p.ym}${p.status === 'locked' ? '（已锁定）' : ''}`"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="订单名">
          <el-input v-model="form.title" placeholder="便于识别的短名" />
        </el-form-item>
        <el-form-item label="客户">
          <el-input v-model="form.customer" placeholder="选填" />
        </el-form-item>
        <el-form-item label="订单数量">
          <el-input-number
            v-model="form.qty"
            :min="0"
            :precision="0"
            controls-position="right"
            style="width: 100%"
          />
        </el-form-item>
        <el-form-item label="下单日期">
          <el-date-picker
            v-model="form.order_date"
            type="date"
            value-format="YYYY-MM-DD"
            placeholder="选择日期"
            style="width: 100%"
          />
        </el-form-item>
        <el-form-item label="交期">
          <el-date-picker
            v-model="form.due_date"
            type="date"
            value-format="YYYY-MM-DD"
            placeholder="选择日期"
            style="width: 100%"
          />
        </el-form-item>
        <el-form-item label="产品">
          <!-- 产品取第一条路线的产品，是派生字段：这里不能改，改了就和路线对不上 -->
          <el-input :model-value="task.product || '未认领'" disabled />
        </el-form-item>
      </div>

      <el-form-item label="修订说明">
        <el-input v-model="form.note" maxlength="80" placeholder="选填，如「客户追加 20 台」；会记进修订历史" />
      </el-form-item>

      <div v-if="changedLabels.length" class="te-change">
        <span class="te-change-label">本次将升版 v{{ (task.version || 1) + 1 }}</span>
        <span class="te-change-body">改动了：{{ changedLabels.join('、') }}</span>
      </div>
      <div v-else class="te-change none">
        <span class="te-change-label muted">未改动</span>
        <span class="te-change-body muted">没有改动口径字段，保存不会产生新版本</span>
      </div>

      <div class="hint-box">
        任务号、订单名、客户、数量、下单日期、交期、核算期间属于<b>口径字段</b>：改动会升版并留痕，
        因为「这单当初按多少台、什么时候交」是事后对账的锚点。状态与备注走各自入口，不算改口径。
        产品由工艺路线决定，要改请调整路线。
      </div>
    </el-form>

    <template #footer>
      <el-button size="small" @click="visible = false">取消</el-button>
      <el-button size="small" type="primary" :loading="saving" :disabled="!changedLabels.length" @click="submit">
        {{ changedLabels.length ? `保存并升到 v${(task?.version || 1) + 1}` : '保存' }}
      </el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  // 完整任务详情（task_detail），至少要带 version / notices
  task: { type: Object, default: null }
})
const emit = defineEmits(['update:modelValue', 'saved'])

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v)
})

const saving = ref(false)
const periods = ref([])
const form = ref(empty())

// 口径字段：与后端 VERSIONED_TASK_FIELDS 一一对应
const FIELDS = [
  ['task_no', '任务号'],
  ['title', '订单名'],
  ['customer', '客户'],
  ['qty', '订单数量'],
  ['order_date', '下单日期'],
  ['due_date', '交期'],
  ['period_id', '核算期间']
]

function empty() {
  return {
    task_no: '',
    title: '',
    customer: '',
    qty: 0,
    order_date: '',
    due_date: '',
    period_id: null,
    note: ''
  }
}

function fmtQty(v) {
  const n = Number(v || 0)
  return Number.isInteger(n) ? n : Number(n.toFixed(4))
}

function hydrate() {
  const t = props.task
  if (!t) return
  form.value = {
    task_no: t.task_no || '',
    title: t.title || '',
    customer: t.customer || '',
    qty: fmtQty(t.qty),
    order_date: t.order_date || '',
    due_date: t.due_date || '',
    period_id: t.period_id ?? null,
    note: ''
  }
  loadPeriods()
}

// 抽屉里点「编辑基本信息」时详情可能已经换了一条任务，重开要重新灌值
watch(
  () => [props.modelValue, props.task?.id],
  ([open]) => {
    if (open) hydrate()
  }
)

async function loadPeriods() {
  if (periods.value.length) return
  try {
    const d = await api.periods()
    periods.value = d.items || d || []
  } catch {
    periods.value = []
  }
}

// 只比口径字段：改状态/备注不在这儿做
function norm(field, v) {
  if (field === 'qty') return fmtQty(v)
  if (field === 'period_id') return v ?? null
  return String(v ?? '').trim()
}

const changed = computed(() => {
  const t = props.task
  if (!t) return []
  return FIELDS.filter(([f]) => norm(f, form.value[f]) !== norm(f, t[f])).map(([f]) => f)
})

const changedLabels = computed(() =>
  changed.value.map((f) => (FIELDS.find(([k]) => k === f) || [, f])[1])
)

async function submit() {
  const t = props.task
  if (!t) return

  // 升版待确认时先问一句：改基本信息等于把这张单的归属口径往后推一步，
  // 而工艺基线还没人认，两件事叠在一起最容易出现「按哪版算」说不清
  const notices = t.notices || []
  if (notices.length) {
    const lines = notices.slice(0, 3).map((n) => `· ${n.part || '—'}：${n.message}`).join('\n')
    const more = notices.length > 3 ? `\n… 另有 ${notices.length - 3} 条` : ''
    try {
      await ElMessageBox.confirm(
        `本单有 ${notices.length} 条工艺引用「基线已升版、尚未确认」：\n${lines}${more}\n\n` +
          '改基本信息不会消除这些提醒，建议保存后回抽屉用「采用新版 / 保留现版」处理。仍要保存吗？',
        '升版待确认',
        { type: 'warning', confirmButtonText: '仍然保存', cancelButtonText: '先去确认' }
      )
    } catch {
      return
    }
  }

  const payload = { user: 'operator', note: form.value.note || '' }
  changed.value.forEach((f) => {
    payload[f] = f === 'qty' ? fmtQty(form.value[f]) : form.value[f]
  })

  saving.value = true
  try {
    const r = await api.patchTask(t.id, payload)
    if (r.bumped) {
      ElMessage.success(r.message || `已升到 v${r.version}`)
    } else {
      ElMessage.info(r.message || '已保存')
    }
    visible.value = false
    // 改核算期间这类动作会连带影响别的账，不能只给一句成功
    if ((r.warnings || []).length) {
      ElMessageBox.alert(r.warnings.join('\n'), '已保存，但请注意', {
        type: 'warning',
        confirmButtonText: '知道了'
      }).catch(() => {})
    }
    emit('saved', r)
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.te-head {
  display: flex;
  align-items: center;
  gap: 10px;
  color: #1f2329;
}
.te-title { font-weight: 600; font-size: 14.5px; }
.te-no { font-size: 12.5px; color: #5a6474; }
.form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0 12px;
}
.te-change {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin: 2px 0 10px;
  padding: 7px 10px;
  border-radius: 6px;
  background: #fff8ec;
  border: 1px solid #f5e2c0;
  font-size: 12.5px;
  line-height: 1.6;
}
.te-change.none { background: #f7f9fc; border-color: #e7ebf1; }
.te-change-label { font-weight: 600; color: #a86400; white-space: nowrap; }
.te-change.none .te-change-label { font-weight: 500; color: #8a94a6; }
.te-change-body { color: #5a6474; }
.hint-box {
  font-size: 12px;
  color: #5a6474;
  background: #f5f8fd;
  border: 1px solid #e2eaf6;
  border-radius: 6px;
  padding: 8px 10px;
  line-height: 1.7;
}
</style>
