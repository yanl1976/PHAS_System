<template>
  <el-dialog
    v-model="visible"
    :title="title"
    width="720px"
    :close-on-click-modal="false"
    class="sheet-task-dialog"
    @open="onOpen"
  >
    <!-- 结果面板：改挂成功后先看清后果再关，别让一句 toast 把提醒冲掉 -->
    <div v-if="result" class="res-wrap">
      <div class="res-head" :class="{ bad: failedCount }">
        <b>{{ resultHead }}</b>
      </div>
      <div v-if="result.notices?.length" class="res-block danger">
        <div class="res-title">新订单有 {{ result.notices.length }} 条工艺升版待确认</div>
        <div v-for="(n, i) in result.notices" :key="i" class="res-line">· {{ n.message }}</div>
        <div class="res-line muted">可到「生产任务」抽屉里逐条「采用新版 / 保留现版」，确认前这张单按哪一版核定额是说不清的。</div>
      </div>
      <div v-if="warnChecks.length" class="res-block warn">
        <div class="res-title">需要留意的 {{ warnChecks.length }} 项</div>
        <div v-for="(c, i) in warnChecks" :key="i" class="res-line">· {{ c.message }}</div>
      </div>
      <div v-if="infoChecks.length" class="res-block info">
        <div v-for="(c, i) in infoChecks" :key="i" class="res-line">· {{ c.message }}</div>
      </div>
      <div v-if="failedRows.length" class="res-block danger">
        <div class="res-title">未改挂 {{ failedRows.length }} 张</div>
        <div v-for="(f, i) in failedRows" :key="i" class="res-line">· 单据 #{{ f.sheet_id }}：{{ f.reason }}</div>
      </div>
    </div>

    <template v-else>
      <div class="cur-row">
        <span class="muted">当前归属</span>
        <b v-if="current">{{ current.task_no }}</b>
        <span v-else class="muted">未归集</span>
        <span v-if="sheetIds.length > 1" class="cur-n">· 本次改挂 {{ sheetIds.length }} 张单</span>
        <span v-if="sheetPeriod" class="cur-n">· 单据期间 {{ sheetPeriod }}</span>
      </div>

      <el-input
        v-model="q"
        placeholder="搜索任务号 / 订单名 / 产品 / 客户"
        clearable
        style="margin: 10px 0 8px"
        @input="onSearch"
      />

      <div class="pick-list" v-loading="loading">
        <div v-if="!options.length" class="pick-empty muted">
          没有匹配的订单。可先在「生产任务」里建好订单，再回来改挂。
        </div>
        <div
          v-for="t in options"
          :key="t.id"
          class="pick-row"
          :class="{ on: picked === t.id, disabled: lockedStatus(t.status) }"
          @click="pick(t)"
        >
          <div class="pick-main">
            <span class="pick-no">{{ t.task_no }}</span>
            <el-tag v-if="t.status === 'closed'" size="small" type="info">已结案</el-tag>
            <el-tag v-else-if="t.status === 'cancelled'" size="small" type="info">已取消</el-tag>
            <el-tag v-else-if="t.status === 'draft'" size="small" type="info">草稿</el-tag>
            <el-tag v-if="sheetPeriod && t.period_ym && t.period_ym !== sheetPeriod" size="small" type="warning" effect="plain">
              跨期 {{ t.period_ym }}
            </el-tag>
            <el-tag v-if="t.id === current?.id" size="small" type="success" effect="plain">当前</el-tag>
          </div>
          <div class="pick-sub muted">
            {{ t.title || '—' }}
            <span v-if="t.product"> · {{ t.product }}</span>
            <span> · 单据 {{ t.sheet_count }}</span>
            <span> · 实报 {{ t.actual_hours }}h</span>
            <span v-if="t.notice_count" class="pick-warn"> · 升版待确认 {{ t.notice_count }}</span>
          </div>
        </div>
      </div>

      <div class="opt-row">
        <el-checkbox v-model="attachRoutes">同时补挂工艺引用（引用已发布基线，不复制工艺）</el-checkbox>
        <el-checkbox v-model="force">允许跨期改挂</el-checkbox>
      </div>
      <div class="opt-tip muted">
        跨期改挂会让两个月的工时汇总同时变化，确有必要才勾选；核算期已锁定的单据一律不允许改挂。
      </div>

      <div v-if="errMsg" class="err-bar">{{ errMsg }}</div>
    </template>

    <template #footer>
      <template v-if="result">
        <el-button type="primary" @click="close">完成</el-button>
      </template>
      <template v-else>
        <el-button @click="close">取消</el-button>
        <el-button v-if="current && sheetIds.length === 1" :loading="busy" @click="detach">摘下归属</el-button>
        <el-button type="primary" :loading="busy" :disabled="!picked" @click="submit()">
          {{ sheetIds.length > 1 ? `改挂 ${sheetIds.length} 张` : '改挂到该订单' }}
        </el-button>
      </template>
    </template>
  </el-dialog>
</template>

<script setup>
import { computed, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  // 单张或批量：一律用数组，避免两套分支
  sheetIds: { type: Array, default: () => [] },
  current: { type: Object, default: null },
  sheetPeriod: { type: String, default: '' }
})
const emit = defineEmits(['update:modelValue', 'done'])

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v)
})

const title = computed(() =>
  props.sheetIds.length > 1 ? `改挂任务 · ${props.sheetIds.length} 张单据` : '改挂任务'
)

const q = ref('')
const loading = ref(false)
const busy = ref(false)
const options = ref([])
const picked = ref(null)
const attachRoutes = ref(false)
const force = ref(false)
const errMsg = ref('')
const result = ref(null)

const failedRows = computed(() => result.value?.failed || [])
const failedCount = computed(() => failedRows.value.length)
const allChecks = computed(() => result.value?.checks || [])
const warnChecks = computed(() => allChecks.value.filter((c) => c.level !== 'info'))
const infoChecks = computed(() => allChecks.value.filter((c) => c.level === 'info'))
const resultHead = computed(() => {
  const r = result.value
  if (!r) return ''
  if (r.moved !== undefined) {
    const to = r.to_task?.task_no || ''
    return `已改挂 ${r.moved} 张${to ? ` → ${to}` : ''}${r.failed?.length ? `，${r.failed.length} 张未成功` : ''}`
  }
  return r.message || '已处理'
})

function lockedStatus(s) {
  return s === 'closed' || s === 'cancelled'
}

function pick(t) {
  if (lockedStatus(t.status)) {
    errMsg.value = `订单 ${t.task_no} 已${t.status === 'closed' ? '结案' : '取消'}，不再接收单据`
    return
  }
  errMsg.value = ''
  picked.value = t.id
}

let timer = null
function onSearch() {
  clearTimeout(timer)
  timer = setTimeout(() => search(), 260)
}

async function search() {
  loading.value = true
  try {
    const r = await api.tasks({ q: q.value || '', size: 30 })
    options.value = r.items || []
  } finally {
    loading.value = false
  }
}

function onOpen() {
  result.value = null
  errMsg.value = ''
  picked.value = null
  attachRoutes.value = false
  force.value = false
  q.value = ''
  search()
}

async function submit(isForce = false) {
  if (!props.sheetIds.length) return
  busy.value = true
  errMsg.value = ''
  const payload = {
    task_id: picked.value,
    user: 'operator',
    attach_routes: attachRoutes.value,
    force: isForce || force.value
  }
  try {
    let res
    if (props.sheetIds.length > 1) {
      res = await api.batchMoveSheetTask({ sheet_ids: props.sheetIds, ...payload })
    } else {
      res = await api.moveSheetTask(props.sheetIds[0], payload)
    }
    result.value = res
    emit('done', res)
  } catch (e) {
    const msg = e?.response?.data?.detail || '改挂失败'
    errMsg.value = msg
    // 跨期是「需要确认」而不是「不能做」：把开关点亮，让他再点一次
    if (!isForce && msg.includes('跨期')) force.value = true
    else ElMessage.error(msg)
  } finally {
    busy.value = false
  }
}

async function detach() {
  try {
    await ElMessageBox.confirm(
      `把这张单从 ${props.current?.task_no || '当前订单'} 上摘下来？它会变为未归集，不再计入任何任务的实报工时。`,
      '摘下任务归属',
      { type: 'warning', confirmButtonText: '摘下', cancelButtonText: '取消' }
    )
  } catch {
    return
  }
  busy.value = true
  try {
    const res = await api.moveSheetTask(props.sheetIds[0], {
      task_id: null,
      user: 'operator'
    })
    result.value = res
    emit('done', res)
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '操作失败')
  } finally {
    busy.value = false
  }
}

function close() {
  visible.value = false
}
</script>

<style scoped>
.cur-row {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  padding: 8px 10px;
  background: #f6f8fb;
  border: 1px solid #e7ecf3;
  border-radius: 6px;
}
.cur-n { color: #8a94a6; font-size: 12px; }

.pick-list {
  max-height: 268px;
  overflow-y: auto;
  border: 1px solid #e7ecf3;
  border-radius: 6px;
  padding: 4px;
  min-height: 96px;
}
.pick-empty { padding: 28px 12px; text-align: center; font-size: 12.5px; }
.pick-row {
  padding: 7px 9px;
  border-radius: 5px;
  cursor: pointer;
  border: 1px solid transparent;
}
.pick-row:hover { background: #f4f7fc; }
.pick-row.on { background: #eef4ff; border-color: #b9cff5; }
.pick-row.disabled { opacity: 0.5; cursor: not-allowed; }
.pick-main { display: flex; align-items: center; gap: 6px; }
.pick-no { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 12.5px; font-weight: 600; }
.pick-sub { font-size: 12px; margin-top: 2px; line-height: 1.5; }
.pick-warn { color: #c0392b; font-weight: 600; }

.opt-row { display: flex; gap: 22px; margin-top: 10px; }
.opt-tip { font-size: 11.5px; line-height: 1.6; margin-top: 2px; }

.err-bar {
  margin-top: 8px;
  padding: 7px 10px;
  background: #fdecea;
  border: 1px solid #f5c6c2;
  border-radius: 6px;
  color: #b3261e;
  font-size: 12.5px;
  line-height: 1.6;
}

.res-wrap { display: flex; flex-direction: column; gap: 8px; }
.res-head {
  font-size: 13.5px;
  padding: 9px 11px;
  background: #eefaf3;
  border: 1px solid #bfe8d2;
  border-radius: 6px;
  color: #1f6b45;
}
.res-head.bad { background: #fdf3e7; border-color: #f3d3a8; color: #8a5a12; }
.res-block { padding: 8px 11px; border-radius: 6px; font-size: 12.5px; line-height: 1.7; }
.res-block.danger { background: #fdecea; border: 1px solid #f5c6c2; color: #a3231c; }
.res-block.warn { background: #fff8e6; border: 1px solid #f2dfae; color: #7a5a12; }
.res-block.info { background: #f4f7fb; border: 1px solid #e3e9f2; color: #4a5566; }
.res-title { font-weight: 600; margin-bottom: 3px; }
.res-line { padding-left: 2px; }
</style>
