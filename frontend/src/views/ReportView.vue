<template>
  <div class="page">
    <div class="page-head">
      <span class="page-title">扫码报工</span>
      <span class="page-sub">
        纸单签字交回后，扫右上角的码 → 核对数量 → 提交。工时按 合格数 × 定额 自动算，不经识别
      </span>
      <div class="spacer"></div>
      <el-button size="small" @click="printVisible = true">打印预印单</el-button>
      <el-button size="small" @click="recentVisible = true; loadRecent()">最近单据</el-button>
    </div>

    <!-- 扫码区：扫码枪打完回车即触发，所以输入框常驻并自动聚焦 -->
    <div class="scan-bar">
      <el-icon class="scan-icon"><FullScreen /></el-icon>
      <input
        ref="codeInput"
        v-model="code"
        class="scan-input"
        placeholder="把光标放这里，扫纸单右上角的二维码（扫码枪自动回车）；也可以手输单号后回车"
        @keyup.enter="doScan"
      />
      <el-button type="primary" :loading="scanning" @click="doScan">识别</el-button>
      <el-button v-if="form" text @click="reset">清空</el-button>
    </div>

    <!-- 扫码失败的三种情况处理方式完全不同，所以分开说 -->
    <el-alert
      v-if="errMsg"
      :type="errCode === 'ALREADY_REPORTED' ? 'warning' : 'error'"
      :closable="false"
      show-icon
      class="banner"
      :title="errMsg"
    >
      <template v-if="errCode === 'ALREADY_REPORTED'" #default>
        <span class="banner-text">
          同号重复提交会把工时算两遍、工资多发一遍，所以系统拦住了。
          如果确实要重报，先撤销上一次报工（会删掉它产生的明细）。
        </span>
        <el-button size="small" type="warning" @click="doUndo(errFormNo)">撤销上一次报工</el-button>
      </template>
    </el-alert>

    <template v-if="form">
      <div class="sheet-head">
        <div class="sh-main">
          <div class="sh-no">{{ form.form_no }}</div>
          <div class="sh-prod">{{ form.product_name }}</div>
          <div class="sh-part">
            <span class="tag-part">{{ form.part_no }}</span>{{ form.part_name }}
          </div>
        </div>
        <div class="sh-meta">
          <div><span class="k">投产台数</span>{{ fmt(form.qty_plan) }} 台</div>
          <div><span class="k">加工件数</span>{{ fmt(form.part_qty) }} 件</div>
          <div><span class="k">定额合计</span>{{ fmt(form.total_hours) }} h</div>
          <div><span class="k">打印</span>{{ form.printed_by || '-' }} · {{ (form.printed_at || '').replace('T', ' ').slice(5) }}</div>
        </div>
      </div>

      <!-- 数量默认按计划带入：车间绝大多数是「整批全合格」，只让异常的几行动手。
           但「没做的工序」必须清零，否则会按计划把没干的活也计了工资 -->
      <div class="fill-tip">
        <el-icon><InfoFilled /></el-icon>
        <span>
          数量已按计划件数带入，请对纸单核对；<b>没做的工序把完成数改成 0</b>。
          返修/废品的合格数需人工核对后点「复核」。
        </span>
        <div class="spacer"></div>
        <el-select
          v-model="bulkWorker"
          size="small"
          filterable
          clearable
          placeholder="本单统一操作者"
          class="bulk-worker"
        >
          <el-option v-for="w in workers" :key="w.name" :label="w.name" :value="w.name" />
        </el-select>
        <el-button link type="primary" size="small" :disabled="!bulkWorker" @click="applyWorker">
          填到所有行
        </el-button>
        <el-button link type="primary" size="small" @click="fillPlan">全部按计划</el-button>
        <el-button link size="small" @click="clearAll">全部清零</el-button>
      </div>

      <el-table :data="rows" size="small" class="report-table" border :row-class-name="rowCls">
        <el-table-column prop="row_no" label="#" width="42" align="center" />
        <el-table-column label="工序" min-width="108">
          <template #default="{ row }">
            <span class="proc">{{ row.process_name }}</span>
            <span v-if="row.is_key" class="key-mark">★</span>
            <span v-if="row.category" class="cat">{{ row.category }}</span>
          </template>
        </el-table-column>
        <el-table-column label="定额/件" width="70" align="right">
          <template #default="{ row }">{{ fmt(row.unit_hours) }} h</template>
        </el-table-column>
        <el-table-column label="计划件数" width="68" align="right">
          <template #default="{ row }">{{ fmt(row.qty_plan) }}</template>
        </el-table-column>
        <el-table-column label="完成数" width="88" align="center">
          <template #default="{ row }">
            <el-input-number
              v-model="row.qty_done"
              :min="0"
              :controls="false"
              size="small"
              class="num-in"
              :class="{ 'off-plan': offPlan(row) }"
              @change="onDone(row)"
            />
          </template>
        </el-table-column>
        <el-table-column label="合格" width="82" align="center">
          <template #default="{ row }">
            <el-input-number
              v-model="row.qualified"
              :min="0"
              :controls="false"
              size="small"
              class="num-in"
              :class="{ 'need-review': needReview(row) }"
              @change="onQualified(row)"
            />
          </template>
        </el-table-column>
        <el-table-column label="返修" width="70" align="center">
          <template #default="{ row }">
            <el-input-number
              v-model="row.rework"
              :min="0"
              :controls="false"
              size="small"
              class="num-in"
              @change="onDefect(row)"
            />
          </template>
        </el-table-column>
        <el-table-column label="废品" width="70" align="center">
          <template #default="{ row }">
            <el-input-number
              v-model="row.scrap"
              :min="0"
              :controls="false"
              size="small"
              class="num-in warn"
              @change="onDefect(row)"
            />
          </template>
        </el-table-column>
        <el-table-column label="操作者" width="118">
          <template #default="{ row }">
            <el-select
              v-model="row.worker_name"
              size="small"
              filterable
              allow-create
              default-first-option
              placeholder="签字人"
              :class="{ 'need-worker': needWorker(row) }"
            >
              <el-option
                v-for="w in workers"
                :key="w.name"
                :label="w.name"
                :value="w.name"
              >
                <span>{{ w.name }}</span>
                <span class="opt-sub">{{ w.default_process || w.team || '' }}</span>
              </el-option>
            </el-select>
          </template>
        </el-table-column>
        <el-table-column label="检验员" width="84">
          <template #default="{ row }">
            <el-input v-model="row.inspector" size="small" />
          </template>
        </el-table-column>
        <el-table-column label="调整原因 / 复核" min-width="146">
          <template #default="{ row }">
            <div class="reason-cell">
              <el-select
                v-if="needReason(row)"
                v-model="row.adjust_reason"
                size="small"
                placeholder="报废/返修必填"
                filterable
                allow-create
                class="reason-sel"
              >
                <el-option v-for="r in REASONS" :key="r" :label="r" :value="r" />
              </el-select>
              <span v-else class="muted-cell">—</span>
              <el-button
                v-if="needReason(row)"
                size="small"
                :type="row.reviewed ? 'success' : 'warning'"
                :plain="!row.reviewed"
                class="review-btn"
                @click="markReviewed(row)"
              >
                {{ row.reviewed ? '已复核' : '复核' }}
              </el-button>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="核定工时" width="82" align="right">
          <template #default="{ row }">
            <span class="hours-cell">{{ fmt(rowHours(row)) }} h</span>
          </template>
        </el-table-column>
      </el-table>

      <div class="foot-bar">
        <div class="foot-left">
          <el-checkbox v-model="signVerified">
            纸上操作者签字已核对
          </el-checkbox>
          <span class="hint">这一勾是纸质凭证与电子账的那颗铆钉，不勾不能提交</span>
        </div>
        <div class="foot-right">
          <span class="sum">
            合计 <b>{{ fmt(totalHours) }}</b> h · <b>{{ fmt(filledCount) }}</b> 行
          </span>
          <el-date-picker
            v-model="reportDate"
            type="date"
            size="small"
            value-format="YYYY-MM-DD"
            placeholder="报工日期"
            style="width: 130px"
          />
          <el-button type="primary" :loading="submitting" @click="doSubmit">提交报工</el-button>
        </div>
      </div>

      <div v-if="warnings.length" class="warn-box">
        <div class="warn-title">提交时系统提示（不影响入账，但请核对）</div>
        <ul>
          <li v-for="(w, i) in warnings" :key="i">{{ w }}</li>
        </ul>
      </div>
    </template>

    <!-- 还没扫码时的空状态：把「这张纸怎么来的」讲清楚，比一句"暂无数据"有用 -->
    <div v-else-if="!errMsg" class="empty-box">
      <div class="empty-title">扫一张预印单开始报工</div>
      <div class="empty-steps">
        <div class="step"><span class="n">1</span>生产任务里「打印预印单」，纸的右上角带二维码</div>
        <div class="step"><span class="n">2</span>工人加工完在纸上填数量并签字</div>
        <div class="step"><span class="n">3</span>计划员收单，扫码 → 核数 → 提交</div>
      </div>
      <div class="empty-note">
        识别通道仍然保留：历史补录、外协件、断网时拍纸单上传照样能入账，
        两边以「印刷体单号」互认，同一张纸不会被算两次。
      </div>
    </div>

    <!-- 打印预印单 -->
    <el-dialog v-model="printVisible" title="打印预印单" width="620px">
      <el-form label-width="88px" size="small">
        <el-form-item label="生产任务">
          <el-select v-model="printTaskId" filterable placeholder="选择任务" style="width: 100%">
            <el-option
              v-for="t in tasks"
              :key="t.id"
              :label="`${t.task_no} · ${t.title || t.product}（${t.qty} 台）`"
              :value="t.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="重印">
          <el-checkbox v-model="forcePrint">作废已打印的旧纸，重新编号打印</el-checkbox>
        </el-form-item>
      </el-form>
      <div class="dlg-tip">
        一个件名印一张纸：车间是拿一张纸去干一件活、签完字随件流转的。
        纸张右上角的码是本单唯一入口，作废或报过工都扫不进来。
      </div>
      <template #footer>
        <el-button size="small" @click="printVisible = false">关闭</el-button>
        <el-button size="small" :disabled="!printTaskId" :loading="generating" @click="doGenerate">
          生成单据
        </el-button>
        <el-button size="small" type="primary" :disabled="!printTaskId" @click="doPrintPdf">
          打印 PDF
        </el-button>
      </template>
    </el-dialog>

    <!-- 最近单据 -->
    <el-dialog v-model="recentVisible" title="最近的预印单" width="820px">
      <el-table :data="recent" size="small" empty-text="还没有打印过预印单">
        <el-table-column prop="form_no" label="单号" width="128" />
        <el-table-column prop="product_name" label="产品" min-width="120" show-overflow-tooltip />
        <el-table-column prop="part_name" label="件名" min-width="120" show-overflow-tooltip />
        <el-table-column label="件数" width="66" align="right">
          <template #default="{ row }">{{ fmt(row.part_qty) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="92" align="center">
          <template #default="{ row }">
            <el-tag :type="STATUS[row.status]?.type || 'info'" size="small" effect="plain">
              {{ STATUS[row.status]?.text || row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="打印" width="130" align="center">
          <template #default="{ row }">
            {{ (row.printed_at || '').slice(5) }} · {{ row.printed_by || '-' }}
          </template>
        </el-table-column>
        <el-table-column label="报工" width="130" align="center">
          <template #default="{ row }">
            <span v-if="row.reported_at">{{ row.reported_at.slice(5) }} · {{ row.reported_by }}</span>
            <span v-else class="muted-cell">—</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="150" align="center">
          <template #default="{ row }">
            <el-button v-if="row.status === 'reported'" link type="warning" size="small" @click="doUndo(row.form_no)">
              撤销报工
            </el-button>
            <!-- 零件不做了 / 单子印错了：必须能作废，否则这张纸永远停在「待报工」，
                 每次「打印全部待报工单」都被重新印出来 -->
            <el-button v-else-if="row.status === 'printed'" link type="danger" size="small" @click="doVoid(row.form_no)">
              作废
            </el-button>
            <span v-else class="muted-cell">已作废</span>
          </template>
        </el-table-column>
      </el-table>
      <template #footer>
        <a :href="pdfUrl('')" target="_blank" class="dl-link">打印当前全部待报工单</a>
        <el-button size="small" @click="recentVisible = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api'
import { refreshStats, setPeriod, store } from '../store'

const REASONS = ['尺寸超差', '材料缺陷', '设备故障', '图纸错误', '操作失误', '返修再加工', '其他']
const STATUS = {
  printed: { text: '待报工', type: 'primary' },
  reported: { text: '已报工', type: 'success' },
  void: { text: '已作废', type: 'info' }
}

const codeInput = ref(null)
const code = ref('')
const scanning = ref(false)
const submitting = ref(false)
const generating = ref(false)

const form = ref(null)
const rows = ref([])
const signVerified = ref(false)
const reportDate = ref('')
const warnings = ref([])
const errMsg = ref('')
const errCode = ref('')
const errFormNo = ref('')

const printVisible = ref(false)
const recentVisible = ref(false)
const printTaskId = ref(null)
const forcePrint = ref(false)
const tasks = ref([])
const recent = ref([])
const workers = ref([])
const bulkWorker = ref('')

const filledRows = computed(() =>
  rows.value.filter((r) => Number(r.qualified || 0) + Number(r.rework || 0) + Number(r.scrap || 0) > 0)
)
const filledCount = computed(() => filledRows.value.length)
const totalHours = computed(() =>
  rows.value.reduce((s, r) => s + Number(r.qualified || 0) * Number(r.unit_hours || 0), 0)
)

function fmt(v) {
  const n = Number(v || 0)
  return Number.isInteger(n) ? String(n) : n.toFixed(2)
}

// 本地日期。不能用 toISOString().slice(0,10)：那是 UTC，
// 北京时间 0~8 点会算成「昨天」—— 每月 1 号凌晨报工，工时会记到上个月。
function todayLocal() {
  const d = new Date()
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

// 有数量的行就必须有操作者：没有人，这笔工时算不出工资，
// 只会以「未匹配」留在汇总里，月底没人认领，再追就追不回来了
function needWorker(row) {
  return Number(row.qualified || 0) > 0 && !(row.worker_name || '').trim()
}

function rowHours(row) {
  return Number(row.qualified || 0) * Number(row.unit_hours || 0)
}

// 报废或返修出现时必须说清原因：不然「这个月废了多少、为什么」永远答不上来，
// 质量分析就无从做起 —— 而这个下拉只要多填一次
function needReason(row) {
  return Number(row.scrap || 0) > 0 || Number(row.rework || 0) > 0
}

// 有返修/报废的行，合格数不能由系统悄悄算：这是「和计划不一样」的地方，
// 必须人眼对着纸单核一遍再点复核。没点复核就不许提交。
function needReview(row) {
  return needReason(row) && !row.reviewed
}

// 完成数与计划件数不一致 = 计划员动过这一行，标出来方便复核
function offPlan(row) {
  const plan = Number(row.qty_plan || 0)
  return plan > 0 && Number(row.qty_done || 0) !== plan
}

function rowCls({ row }) {
  return needReview(row) ? 'row-need-review' : ''
}

// 常见情况是全合格：计划件数直接带进完成数与合格数，人不改就是整批全合格，
// 有废品再把合格数改小。比让人从 0 开始一行行按数字键快得多。
function onDone(row) {
  const done = Number(row.qty_done || 0)
  if (needReason(row)) {
    // 有返修/报废时不覆盖人工填过的合格数，只把「复核」标记清掉
    row.reviewed = false
    return
  }
  row.qualified = done
  row.reviewed = true
}

// 人工改合格数 = 已经看过这一行了，直接视为复核过；
// 但「合格+返修+废品 ≠ 完成数」时仍然提醒（后端也会再校验一次）
function onQualified(row) {
  if (needReason(row)) row.reviewed = true
}

// 出现返修/废品：给一个「完成数 - 返修 - 废品」的建议值，
// 但**不替人确认** —— 必须点「复核」才算核过
function onDefect(row) {
  const done = Number(row.qty_done || 0)
  const bad = Number(row.rework || 0) + Number(row.scrap || 0)
  if (bad > 0) {
    row.qualified = Math.max(0, done - bad)
    row.reviewed = false
  } else {
    row.qualified = done
    row.reviewed = true
  }
}

function markReviewed(row) {
  row.reviewed = true
}

// 一张单的工序常由同一个师傅完成：选一次名字填到所有行，
// 再单独改例外的那几行 —— 比一行一行点下拉快得多，也不容易点错人
function applyWorker() {
  const name = (bulkWorker.value || '').trim()
  if (!name) return
  for (const r of rows.value) r.worker_name = name
}

function fillPlan() {
  for (const r of rows.value) {
    const plan = Number(r.qty_plan || 0)
    r.qty_done = plan
    r.qualified = plan
    r.rework = 0
    r.scrap = 0
    r.reviewed = true
  }
}

function clearAll() {
  for (const r of rows.value) {
    r.qty_done = 0
    r.qualified = 0
    r.rework = 0
    r.scrap = 0
    r.reviewed = true
  }
}

function focusInput() {
  nextTick(() => codeInput.value?.focus())
}

function reset() {
  form.value = null
  rows.value = []
  code.value = ''
  warnings.value = []
  errMsg.value = ''
  errCode.value = ''
  signVerified.value = false
  bulkWorker.value = ''
  focusInput()
}

async function doScan() {
  const raw = (code.value || '').trim()
  if (!raw) {
    focusInput()
    return
  }
  scanning.value = true
  errMsg.value = ''
  errCode.value = ''
  try {
    // 只输单号（RG2611-00001）也能扫：计划员常是照着纸念号手输的
    const q = raw.includes(':') ? raw : `HY1:${raw}:`
    const d = await api.reportScan(q)
    if (!d.ok) {
      errMsg.value = d.reason || '这张单不能报工'
      errCode.value = d.code || ''
      errFormNo.value = d.form_no || raw
      form.value = null
      rows.value = []
      return
    }
    form.value = d.form
    // 默认按计划件数带入完成数/合格数：纸单上绝大多数行就是「整批全合格」，
    // 让计划员只处理异常的几行。auto=true 只用来提示，不影响提交。
    rows.value = (d.lines || []).map((l) => ({
      ...l,
      qty_done: Number(l.qty_plan || 0),
      qualified: Number(l.qty_plan || 0),
      rework: 0,
      scrap: 0,
      reviewed: true,
      auto: true,
      worker_name: l.worker_name || '',
      inspector: '',
      adjust_reason: ''
    }))
    warnings.value = []
    signVerified.value = false
    reportDate.value = todayLocal()
    ElMessage.success(`${d.form.form_no} · ${d.form.part_name} 已载入`)
    // 扫行码时把那一行顶出来：计划员是拿纸对着屏幕核一行
    if (d.focus_row) ElMessage.info(`定位到第 ${d.focus_row} 行`)
  } finally {
    scanning.value = false
  }
}

async function doSubmit() {
  if (!form.value) return
  if (!signVerified.value) {
    ElMessage.warning('请先确认纸上操作者已签字')
    return
  }
  if (!filledCount.value) {
    ElMessage.warning('一行数量都没填')
    return
  }
  const bad = filledRows.value.find((r) => needReason(r) && !(r.adjust_reason || '').trim())
  if (bad) {
    ElMessage.warning(`第 ${bad.row_no} 行有报废/返修，请先选调整原因`)
    return
  }
  const unreviewed = filledRows.value.find((r) => needReview(r))
  if (unreviewed) {
    ElMessage.warning(
      `第 ${unreviewed.row_no} 行有返修/报废，请对着纸单核对合格数后点「复核」`
    )
    return
  }
  const over = filledRows.value.find(
    (r) =>
      Number(r.qualified || 0) + Number(r.rework || 0) + Number(r.scrap || 0) >
      Number(r.qty_done || 0) + 0.001
  )
  if (over) {
    ElMessage.warning(
      `第 ${over.row_no} 行：合格+返修+废品 超过了完成数，请核对`
    )
    return
  }
  const noWorker = filledRows.value.find((r) => !(r.worker_name || '').trim())
  if (noWorker) {
    ElMessage.warning(`第 ${noWorker.row_no} 行没选操作者 —— 没人认领的工时算不出工资`)
    return
  }
  submitting.value = true
  try {
    const payload = {
      form_no: form.value.form_no,
      sign_verified: true,
      report_date: reportDate.value || '',
      rows: filledRows.value.map((r) => ({
        row_no: r.row_no,
        qty_done: r.qty_done,
        qualified: Number(r.qualified || 0),
        rework: Number(r.rework || 0),
        scrap: Number(r.scrap || 0),
        worker_name: r.worker_name || '',
        inspector: r.inspector || '',
        adjust_reason: r.adjust_reason || ''
      }))
    }
    const d = await api.reportSubmit(payload)
    if (!d.ok) {
      ElMessage.error(d.reason || '提交失败')
      return
    }
    warnings.value = d.warnings || []
    ElMessage.success(
      `已入账：${d.items} 行 / ${d.hours} h（${d.period}）` + (warnings.value.length ? '，有提示请核对' : '')
    )
    // 工时报进了 A 月、页面还停在 B 月，是最容易被当成「没记账」的情况：
    // 看着工资页没变化，人会再报一遍。这里直接跟着切过去。
    if (d.period && d.period !== store.ym) {
      setPeriod(d.period)
      ElMessage.info(`本次工时记入 ${d.period}，已切换到该核算期`)
    } else {
      // 同一个月也要刷新顶栏：否则刚报完的工时在抬头还显示 0，
      // 人第一反应就是「没记上」，然后再报一遍
      refreshStats()
    }
    const keepWarn = warnings.value
    reset()
    warnings.value = keepWarn
  } finally {
    submitting.value = false
  }
}

async function doUndo(formNo) {
  if (!formNo) return
  let reason = ''
  try {
    const r = await ElMessageBox.prompt(
      `撤销 ${formNo} 的报工？这会删掉它产生的报工明细，工时从账上退出。\n请写明原因 —— 撤销是删钱的动作，只留一条「撤销」等于没留痕。`,
      '撤销报工',
      {
        type: 'warning',
        confirmButtonText: '撤销',
        cancelButtonText: '取消',
        inputPlaceholder: '例如：合格数敲错、扫错单',
        inputValidator: (v) => (v || '').trim().length >= 2 || '请填写原因（至少 2 个字）'
      }
    )
    reason = (r.value || '').trim()
  } catch (e) {
    return
  }
  const d = await api.reportUndo(formNo, reason)
  if (!d.ok) {
    ElMessage.error(d.reason || '撤销失败')
    return
  }
  ElMessage.success(`已撤销，删除 ${d.removed} 行明细`)
  reset()
  if (recentVisible.value) loadRecent()
}

// 作废：零件不做了、单子印错件名，纸还在工人手里 —— 不作废它就会永远
// 停在「待报工」，每次「打印全部待报工单」都被重新印出来
async function doVoid(formNo) {
  if (!formNo) return
  let reason = ''
  try {
    const r = await ElMessageBox.prompt(
      `作废 ${formNo}？作废后这张纸上的二维码立刻失效，不能再扫码报工。\n请写明作废原因（会写进审计，事后能查到是谁、为什么废的）。\n已报过工的单不能直接作废，必须先撤销报工。`,
      '作废预印单',
      {
        type: 'warning',
        confirmButtonText: '作废',
        cancelButtonText: '取消',
        inputPlaceholder: '例如：零件报废不做了、单子印错件名',
        inputValidator: (v) => (v || '').trim().length >= 2 || '请填写原因（至少 2 个字）'
      }
    )
    reason = (r.value || '').trim()
  } catch (e) {
    return
  }
  const d = await api.reportVoid({ form_no: formNo, reason })
  if (!d.ok) {
    ElMessage.error(d.reason || '作废失败')
    return
  }
  ElMessage.success(`${formNo} 已作废`)
  loadRecent()
}

function pdfUrl(taskId) {
  return `/api/report/forms/pdf?${taskId ? `task_id=${taskId}` : 'status=printed'}`
}

async function doGenerate() {
  if (!printTaskId.value) return
  generating.value = true
  try {
    const d = await api.reportCreateForms({
      task_id: printTaskId.value,
      force: forcePrint.value
    })
    if (d.created?.length || d.reused?.length || d.already_reported?.length) {
      if (d.already_reported?.length) ElMessage.warning(d.message || '部分件名已报工')
      else ElMessage.success(d.message || '已生成')
    } else {
      ElMessage.warning(d.message || '没有可打印的件名')
    }
    loadRecent()
  } finally {
    generating.value = false
  }
}

async function doPrintPdf() {
  if (!printTaskId.value) return
  // 先确保单号存在再打开 PDF：直接点「打印 PDF」而没先「生成单据」时，
  // 后端找不到可打印的单，只会弹一个 404
  try {
    const d = await api.reportCreateForms({
      task_id: printTaskId.value,
      force: forcePrint.value
    })
    if (d.already_reported?.length) {
      ElMessage.warning(
        `${d.already_reported.length} 张已报过工，沿用原单号重印（扫了也不能再报）`
      )
    }
  } catch (e) {
    ElMessage.error(e?.message || '生成单据失败')
    return
  }
  window.open(pdfUrl(printTaskId.value), '_blank')
}

async function loadRecent() {
  const d = await api.reportFormList({ limit: 50 })
  recent.value = d.items || []
}

async function loadTasks() {
  try {
    const d = await api.cardTasks()
    tasks.value = d.tasks || []
  } catch (e) {
    tasks.value = []
  }
}

// 操作者必须从名单里选（也允许临时输入）：手打名字错一个字，
// 这笔工时就会掉进「未匹配」，工资发不出去还没人发现
async function loadWorkers() {
  try {
    const d = await api.adminList('workers')
    workers.value = (d.items || []).filter((w) => w.active !== false)
  } catch (e) {
    workers.value = []
  }
}

onMounted(() => {
  focusInput()
  loadTasks()
  loadWorkers()
})
</script>

<style scoped>
.scan-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 14px;
  background: #f7f9fc;
  border: 1px solid #e3e9f2;
  border-radius: 10px;
  margin-bottom: 12px;
}
.scan-icon { color: #4a7fd0; font-size: 18px; }
.scan-input {
  flex: 1;
  height: 34px;
  padding: 0 12px;
  border: 1px solid #d7dfea;
  border-radius: 8px;
  font-size: 13px;
  outline: none;
  background: #fff;
  font-family: ui-monospace, Consolas, monospace;
}
.scan-input:focus { border-color: #6fa2e8; box-shadow: 0 0 0 2px #e8f1fd; }

.banner { margin-bottom: 10px; }
.banner-text { font-size: 12.5px; line-height: 1.6; margin-right: 8px; }

.fill-tip {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  padding: 7px 12px;
  background: #f4f8ff;
  border: 1px solid #dbe7f8;
  border-radius: 8px;
  font-size: 12.5px;
  color: #4b5c74;
}
.fill-tip .el-icon { color: #4a7fd0; }
.fill-tip b { color: #1f5fae; }
.fill-tip .spacer { flex: 1 1 auto; }
.bulk-worker { width: 132px; }

.sheet-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  gap: 18px;
  padding: 10px 14px;
  background: #f2f7ff;
  border: 1px solid #dce8f8;
  border-radius: 10px;
  margin-bottom: 10px;
}
.sh-main { display: flex; align-items: baseline; gap: 12px; }
.sh-no { font-size: 15px; font-weight: 600; color: #1f5fae; font-variant-numeric: tabular-nums; }
.sh-prod { font-size: 13px; color: #33445c; }
.sh-part { font-size: 14px; color: #16233a; font-weight: 500; }
.tag-part {
  display: inline-block;
  margin-right: 6px;
  padding: 1px 6px;
  background: #dfeafb;
  color: #1f5fae;
  border-radius: 4px;
  font-size: 11.5px;
  font-variant-numeric: tabular-nums;
}
.sh-meta { display: flex; gap: 16px; font-size: 12.5px; color: #33445c; }
.sh-meta .k { color: #8593a8; margin-right: 4px; }

.report-table :deep(.cell) { padding: 4px 6px; }
.report-table :deep(.el-input__wrapper) { padding: 0 6px; }
.proc { color: #16233a; }
.key-mark { color: #d48a1c; margin-left: 2px; }
.cat { margin-left: 6px; font-size: 11.5px; color: #8a97ab; }
.num-in { width: 100%; }
.num-in.warn :deep(.el-input__inner) { color: #c0392b; }
/* 与计划件数不一致：标成浅黄，提醒这一行是人工改过的 */
.num-in.off-plan :deep(.el-input__wrapper) { box-shadow: 0 0 0 1px #e6c07a inset; background: #fffaf0; }
/* 待复核：返修/报废行的合格数必须人眼核过，红边提示 */
.num-in.need-review :deep(.el-input__wrapper) { box-shadow: 0 0 0 1px #e6a23c inset; background: #fff7e8; }
:deep(.row-need-review > td) { background: #fffaf0 !important; }
.reason-cell { display: flex; align-items: center; gap: 6px; }
.reason-sel { flex: 1 1 auto; min-width: 0; }
.review-btn { flex: 0 0 auto; }
.need-worker :deep(.el-select__wrapper) { box-shadow: 0 0 0 1px #e6a23c inset; }
.opt-sub { float: right; color: #a0aab8; font-size: 11.5px; margin-left: 10px; }
.hours-cell { font-variant-numeric: tabular-nums; font-weight: 500; color: #16233a; }
.muted-cell { color: #c0c8d4; }

.foot-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 10px 2px 0;
}
.foot-left { display: flex; align-items: center; gap: 8px; }
.foot-left .hint { font-size: 11.5px; color: #93a0b4; }
.foot-right { display: flex; align-items: center; gap: 10px; }
.sum { font-size: 13px; color: #33445c; }
.sum b { color: #1f5fae; font-size: 15px; font-variant-numeric: tabular-nums; }

.warn-box {
  margin-top: 10px;
  padding: 8px 12px;
  background: #fffaf0;
  border: 1px solid #f0dfbc;
  border-radius: 8px;
  font-size: 12.5px;
  color: #7a5b1e;
}
.warn-title { font-weight: 500; margin-bottom: 4px; }
.warn-box ul { margin: 0; padding-left: 18px; line-height: 1.8; }

.empty-box {
  margin-top: 18px;
  padding: 26px 28px;
  border: 1px dashed #d8e0ec;
  border-radius: 12px;
  background: #fbfcfe;
}
.empty-title { font-size: 14px; font-weight: 500; color: #2b3444; margin-bottom: 12px; }
.empty-steps { display: flex; flex-direction: column; gap: 8px; }
.step { display: flex; align-items: center; gap: 10px; font-size: 13px; color: #4b5565; }
.step .n {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  border-radius: 50%;
  background: #e8f1fd;
  color: #1f6fd0;
  font-size: 11.5px;
}
.empty-note {
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px dashed #e3e9f2;
  font-size: 12px;
  line-height: 1.7;
  color: #8593a8;
}
.dlg-tip { font-size: 12px; line-height: 1.7; color: #8593a8; margin-top: 4px; }
.dl-link { font-size: 12.5px; color: #2f7be5; text-decoration: none; margin-right: 14px; }
.dl-link:hover { text-decoration: underline; }
</style>
