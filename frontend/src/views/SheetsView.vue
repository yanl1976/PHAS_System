<template>
  <div class="page">
    <div class="page-head">
      <span class="page-title">报工台账</span>
      <span class="page-sub">识别单与扫码报工都在这里查（纸面：《加工路线单》）</span>
    </div>

    <div class="card">
      <!-- 来源：两条通道的凭证形态完全不同（一张有照片和逐格复核，
           一张是工序快照 + 签字核对），台账层面合起来看，明细互不干扰 -->
      <div class="row-flex" style="margin-bottom: 10px">
        <el-radio-group v-model="source" size="small" @change="onSourceChange">
          <el-radio-button value="ocr">识别单据</el-radio-button>
          <el-radio-button value="scan">扫码报工</el-radio-button>
        </el-radio-group>
        <span class="muted" v-if="source === 'scan'">
          以预印单为主键；报过的按报工月份、没报的按打印月份列在这里
        </span>
        <span class="muted" v-else>按状态、关键字检索已识别的报工单（纸面：《加工路线单》）</span>
      </div>

      <div class="row-flex" style="margin-bottom: 12px">
        <el-radio-group v-if="source === 'ocr'" v-model="status" size="small" @change="reload">
          <el-radio-button value="">全部</el-radio-button>
          <el-radio-button value="recognized">待核对</el-radio-button>
          <el-radio-button value="verified">已确认</el-radio-button>
          <el-radio-button value="rejected">已作废</el-radio-button>
        </el-radio-group>
        <el-radio-group v-else v-model="scanStatus" size="small" @change="loadScan">
          <el-radio-button value="">全部</el-radio-button>
          <el-radio-button value="printed">待报工</el-radio-button>
          <el-radio-button value="reported">已报工</el-radio-button>
          <el-radio-button value="void">已作废</el-radio-button>
        </el-radio-group>
        <el-input v-model="keyword" size="small" placeholder="件名 / 产品 / 人员 / 工序" style="width: 220px" clearable
          @keyup.enter="reload" />
        <el-checkbox v-if="source === 'ocr'" v-model="onlyFlagged" size="small" @change="reload">只看有告警</el-checkbox>
        <el-button size="small" type="primary" @click="reload">查询</el-button>
        <div class="spacer"></div>
        <!-- 批量改挂：月底集中发现一批单归错时，一张一张点太费事 -->
        <template v-if="source === 'ocr' && selected.length">
          <span class="sel-tip">已选 {{ selected.length }} 张</span>
          <el-button size="small" type="warning" plain @click="openBatchMove">批量改挂</el-button>
          <el-button size="small" text @click="clearSelection">取消选择</el-button>
        </template>
        <span class="muted">共 {{ source === 'ocr' ? total : scanTotal }} 张</span>
      </div>

      <el-table
        v-if="source === 'ocr'"
        ref="tableRef"
        :data="rows"
        size="small"
        v-loading="loading"
        class="ledger-table"
        style="width: 100%"
        @selection-change="onSelectionChange"
      >
        <el-table-column type="selection" width="40" align="center" />
        <el-table-column label="#" width="58" align="center" />
        <el-table-column label="缩略图" width="92" align="center">
          <template #default="{ row }">
            <img v-if="row.image?.thumb_url" :src="row.image.thumb_url" class="mini-thumb" @click="preview(row)" />
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="产品/批次" min-width="168" show-overflow-tooltip>
          <template #default="{ row }">
            <span>{{ row.header?.product_name || '—' }}</span>
            <span v-if="row.header?.batch_no" class="muted"> · {{ row.header.batch_no }}</span>
          </template>
        </el-table-column>
        <el-table-column label="任务" min-width="206" show-overflow-tooltip>
          <template #default="{ row }">
            <template v-if="row.task_no">
              <span>{{ row.task_no }}</span>
              <!-- 归属订单的工艺基线升过版、还没人认：台账里也要标出来 -->
              <el-tag
                v-if="row.task_notice_count"
                size="small"
                type="danger"
                effect="dark"
                style="margin-left: 4px"
                title="该订单引用的工艺基线已升版，尚未确认"
              >
                升版待确认
              </el-tag>
            </template>
            <span v-else class="muted">未归集</span>
            <!-- 归集是自动的，就一定会归错；入口放在归属旁边，谁看见谁改 -->
            <button class="relink" title="改挂到其它订单" @click="openMove(row)">改挂</button>
          </template>
        </el-table-column>
        <!-- 这张纸如果是系统预印的，单号在这里：一眼能看出它和扫码报工那张是同一张 -->
        <el-table-column label="预印单号" width="122" show-overflow-tooltip>
          <template #default="{ row }">
            <span v-if="row.print_form_no" class="mono">{{ row.print_form_no }}</span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="明细" width="72" align="center" show-overflow-tooltip>
          <template #default="{ row }">{{ row.item_count ?? '—' }}</template>
        </el-table-column>
        <el-table-column label="工时" width="96" align="right" show-overflow-tooltip>
          <template #default="{ row }">
            <span class="hours-cell">{{ (row.total_hours || 0).toFixed(2) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="告警" width="78" align="center" show-overflow-tooltip>
          <template #default="{ row }">
            <el-tag v-if="row.flag_count" size="small" type="warning">{{ row.flag_count }}</el-tag>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="92" align="center" show-overflow-tooltip>
          <template #default="{ row }">
            <el-tag size="small" :type="statusType(row.status)">{{ statusText(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="确认人/时间" min-width="158" show-overflow-tooltip>
          <template #default="{ row }">
            <span v-if="row.verify_at">
              {{ row.verify_user }}<span class="muted"> · {{ fmtTime(row.verify_at) }}</span>
            </span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="250" fixed="right">
          <template #default="{ row }">
            <el-button size="small" text type="primary" @click="$router.push(`/review/${row.id}`)">
              {{ row.status === 'verified' ? '查看' : '核对' }}
            </el-button>
            <el-button size="small" text @click="printSheet(row)">打印</el-button>
            <el-button v-if="row.status === 'verified'" size="small" text type="warning" @click="reopen(row)">撤销</el-button>
            <!-- 作废可逆、删除不可逆：作废错了能恢复，错拍/重传的单要能彻底清掉 -->
            <el-button v-if="row.status === 'rejected'" size="small" text type="success" @click="restore(row)">恢复</el-button>
            <el-button v-if="row.status !== 'verified'" size="small" text type="danger" @click="removeRow(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>

      <!-- 扫码报工一侧：以预印单为主键，只读。它能编辑的东西（数量/签字）
           在扫码报工页上，台账不该给第二个改数的入口 -->
      <el-table
        v-else
        :data="scanRows"
        size="small"
        v-loading="scanLoading"
        class="ledger-table"
        style="width: 100%"
        empty-text="这个月还没有扫码报工的预印单"
      >
        <el-table-column prop="form_no" label="单号" width="128" />
        <el-table-column label="产品/批次" min-width="160" show-overflow-tooltip>
          <template #default="{ row }">
            <span>{{ row.product_name || '—' }}</span>
            <span v-if="row.batch_no" class="muted"> · {{ row.batch_no }}</span>
          </template>
        </el-table-column>
        <el-table-column label="件号/件名" min-width="170" show-overflow-tooltip>
          <template #default="{ row }">
            <span class="mono">{{ row.part_no || '—' }}</span>
            <span class="muted"> · {{ row.part_name || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="任务" width="140" show-overflow-tooltip>
          <template #default="{ row }">
            <span v-if="row.task_no">{{ row.task_no }}</span>
            <span v-else class="muted">未归集</span>
          </template>
        </el-table-column>
        <el-table-column label="件数" width="72" align="right">
          <template #default="{ row }">{{ fmtQty(row.part_qty) }}</template>
        </el-table-column>
        <el-table-column label="明细" width="66" align="center">
          <template #default="{ row }">{{ row.item_count || '—' }}</template>
        </el-table-column>
        <el-table-column label="工时" width="92" align="right">
          <template #default="{ row }">
            <!-- 没报工的单显示的是纸上定额合计，不是实报工时：两者别混 -->
            <span
              class="hours-cell"
              :title="row.item_count ? '实际报工工时' : '还没报工，这里是纸上定额合计'"
            >{{ (row.hours || 0).toFixed(2) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="92" align="center">
          <template #default="{ row }">
            <el-tag size="small" :type="SCAN_STATUS[row.status]?.type || 'info'" effect="plain">
              {{ SCAN_STATUS[row.status]?.text || row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="打印/报工" min-width="170" show-overflow-tooltip>
          <template #default="{ row }">
            <span class="muted">{{ (row.printed_at || '').slice(5, 16) }} · {{ row.printed_by || '—' }}</span>
            <template v-if="row.reported_at">
              <br /><span>{{ row.reported_at.slice(5, 16) }} · {{ row.reported_by }}</span>
            </template>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="96" align="center" fixed="right">
          <template #default="{ row }">
            <el-button size="small" text type="primary" @click="openDetail(row.form_no)">明细</el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="row-flex" style="justify-content: flex-end; margin-top: 12px">
        <el-pagination
          v-if="source === 'ocr'"
          v-model:current-page="page"
          :page-size="size"
          :total="total"
          layout="prev, pager, next, total"
          @current-change="load"
        />
        <el-pagination
          v-else
          v-model:current-page="scanPage"
          :page-size="size"
          :total="scanTotal"
          layout="prev, pager, next, total"
          @current-change="loadScan"
        />
      </div>
    </div>

    <el-dialog v-model="previewVisible" title="原图预览" width="720px">
      <img v-if="previewRow" :src="previewRow.image.processed_url" style="width: 100%" />
    </el-dialog>

    <!-- 改挂：单张与批量共用同一个弹窗，口径不会走偏 -->
    <SheetTaskDialog
      v-model="moveVisible"
      :sheet-ids="moveIds"
      :current="moveCurrent"
      :sheet-period="store.ym || ''"
      @done="onMoved"
    />

    <!-- 扫码报工的只读明细：纸上印了什么 vs 实际报了谁多少工时 -->
    <el-dialog v-model="detailVisible" :title="`扫码报工明细 · ${detail?.form?.form_no || ''}`" width="880px">
      <div v-loading="detailLoading">
        <template v-if="detail?.form">
          <div class="row-flex" style="margin-bottom: 10px; flex-wrap: wrap; gap: 14px">
            <span><span class="muted">产品</span> {{ detail.form.product_name || '—' }}</span>
            <span><span class="muted">件号</span> <b class="mono">{{ detail.form.part_no || '—' }}</b></span>
            <span><span class="muted">件名</span> {{ detail.form.part_name || '—' }}</span>
            <span><span class="muted">件数</span> {{ fmtQty(detail.form.part_qty) }}</span>
            <span><span class="muted">打印</span> {{ (detail.form.printed_at || '').slice(5) }} · {{ detail.form.printed_by || '—' }}</span>
            <span v-if="detail.form.reported_at">
              <span class="muted">报工</span> {{ detail.form.reported_at.slice(5) }} · {{ detail.form.reported_by }}
            </span>
          </div>
          <el-alert v-if="detail.form.note" :title="detail.form.note" type="info" :closable="false"
            style="margin-bottom: 10px" />

          <el-table :data="detail.items" size="small" empty-text="这张单还没有报工明细">
            <el-table-column prop="seq" label="行" width="52" align="center" />
            <el-table-column prop="process" label="工序" min-width="120" show-overflow-tooltip />
            <el-table-column prop="worker_name" label="操作者" width="96" />
            <el-table-column label="完成/合格/返修/废品" min-width="180">
              <template #default="{ row }">
                <span class="mono">{{ fmtQty(row.qty_done) }} / {{ fmtQty(row.qualified_qty) }} /
                  {{ fmtQty(row.rework_qty) }} / {{ fmtQty(row.scrap_qty) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="单件" width="72" align="right">
              <template #default="{ row }"><span class="mono">{{ fmtQty(row.unit_hours) }}</span></template>
            </el-table-column>
            <el-table-column label="工时" width="82" align="right">
              <template #default="{ row }"><span class="hours-cell">{{ (row.total_hours || 0).toFixed(2) }}</span></template>
            </el-table-column>
            <el-table-column prop="adjust_reason" label="调整原因" min-width="120" show-overflow-tooltip />
            <el-table-column prop="inspector" label="检验员" width="86" />
          </el-table>

          <div class="section-title" style="margin-top: 14px">纸上印的工序（预印快照）</div>
          <el-table :data="detail.lines" size="small" max-height="220">
            <el-table-column prop="row_no" label="行" width="52" align="center" />
            <el-table-column prop="process_name" label="工序" min-width="140" show-overflow-tooltip />
            <el-table-column label="单件" width="72" align="right">
              <template #default="{ row }"><span class="mono">{{ fmtQty(row.unit_hours) }}</span></template>
            </el-table-column>
            <el-table-column label="计划件数" width="88" align="right">
              <template #default="{ row }">{{ fmtQty(row.qty_plan) }}</template>
            </el-table-column>
            <el-table-column label="定额合计" width="88" align="right">
              <template #default="{ row }"><span class="mono">{{ fmtQty(row.total_hours) }}</span></template>
            </el-table-column>
          </el-table>
        </template>
        <div v-else-if="!detailLoading" class="muted">没有可显示的明细</div>
      </div>
      <template #footer>
        <el-button size="small" @click="detailVisible = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api'
import { refreshStats, store } from '../store'
import SheetTaskDialog from '../components/SheetTaskDialog.vue'

const rows = ref([])
const total = ref(0)
const page = ref(1)
const size = ref(15)
const status = ref('')
const keyword = ref('')
const onlyFlagged = ref(false)
const loading = ref(false)
const previewVisible = ref(false)
const previewRow = ref(null)
const tableRef = ref(null)
const selected = ref([])

// ---- 来源：识别单 / 扫码报工。两条通道的凭证形态完全不同，
// 台账层面合起来看就行，不必也不该把 PrintSheet 塞进 sheet 表。
const source = ref('ocr')
const scanRows = ref([])
const scanTotal = ref(0)
const scanPage = ref(1)
const scanStatus = ref('')
const scanLoading = ref(false)
const detailVisible = ref(false)
const detailLoading = ref(false)
const detail = ref(null)
const SCAN_STATUS = {
  printed: { text: '待报工', type: 'warning' },
  reported: { text: '已报工', type: 'success' },
  void: { text: '已作废', type: 'info' }
}

// 改挂弹窗：moveIds 是「本次要改的几张」，moveCurrent 只在单张时给出
const moveVisible = ref(false)
const moveIds = ref([])
const moveCurrent = ref(null)

function statusText(s) {
  return { recognized: '待核对', verified: '已确认', rejected: '已作废' }[s] || s
}

function statusType(s) {
  return { recognized: 'warning', verified: 'success', rejected: 'danger' }[s] || 'info'
}

function fmtTime(s) {
  return s ? s.replace('T', ' ').slice(5, 16) : ''
}

function printSheet(row) {
  window.open(api.sheetPrintUrl(row.id), '_blank')
}

async function load() {
  loading.value = true
  try {
    const r = await api.sheets({
      ym: store.ym,
      status: status.value || undefined,
      keyword: keyword.value || undefined,
      only_flagged: onlyFlagged.value || undefined,
      page: page.value,
      size: size.value
    })
    rows.value = r.items
    total.value = r.total
  } finally {
    loading.value = false
  }
}

function reload() {
  if (source.value === 'scan') {
    scanPage.value = 1
    loadScan()
    return
  }
  page.value = 1
  load()
}

function onSourceChange() {
  clearSelection()
  if (source.value === 'scan') loadScan()
  else load()
}

// 扫码报工一侧：以预印单为主键。报过的按报工月份、没报的按打印月份，
// 所以「这个月印了却一直没报」的单也看得见 —— 那正是月底要追的。
async function loadScan() {
  scanLoading.value = true
  try {
    const r = await api.reportLedger({
      ym: store.ym,
      status: scanStatus.value || undefined,
      keyword: keyword.value || undefined,
      page: scanPage.value,
      size: size.value
    })
    scanRows.value = r.items || []
    scanTotal.value = r.total || 0
  } finally {
    scanLoading.value = false
  }
}

async function openDetail(formNo) {
  detail.value = null
  detailVisible.value = true
  detailLoading.value = true
  try {
    const d = await api.reportLedgerDetail(formNo)
    detail.value = d.ok ? d : null
    if (!d.ok) ElMessage.error(d.reason || '查不到这张单')
  } catch (e) {
    ElMessage.error(e?.message || '查询失败')
  } finally {
    detailLoading.value = false
  }
}

function fmtQty(v) {
  if (v === null || v === undefined || v === '') return '—'
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  return Number.isInteger(n) ? String(n) : n.toFixed(2)
}

async function reopen(row) {
  await api.reopenSheet(row.id)
  ElMessage.success('已撤销确认')
  await load()
  await refreshStats()
}

async function restore(row) {
  await api.restoreSheet(row.id)
  ElMessage.success(`单据 #${row.id} 已恢复为待核对`)
  await load()
  await refreshStats()
}

async function removeRow(row) {
  let reason = ''
  try {
    const r = await ElMessageBox.prompt(
      `将删除单据 #${row.id}（${row.total_hours?.toFixed(2) || 0} 小时）及其明细、原图，不可恢复。请填原因。`,
      '彻底删除单据',
      { inputPlaceholder: '如：重复上传 / 拍糊了重拍', type: 'warning', confirmButtonText: '下一步' }
    )
    reason = (r.value || '').trim()
  } catch {
    return
  }
  if (!reason) return ElMessage.warning('请填删除原因')
  try {
    await ElMessageBox.confirm('确认删除？删除后无法恢复。', '再次确认', {
      type: 'error',
      confirmButtonText: '确认删除',
      cancelButtonText: '取消'
    })
  } catch {
    return
  }
  await api.deleteSheet(row.id, reason)
  ElMessage.success('已删除')
  await load()
  await refreshStats()
}

function preview(row) {
  previewRow.value = row
  previewVisible.value = true
}

function onSelectionChange(list) {
  selected.value = list
}

function clearSelection() {
  tableRef.value?.clearSelection()
  selected.value = []
}

function openMove(row) {
  moveIds.value = [row.id]
  moveCurrent.value = row.task_id ? { id: row.task_id, task_no: row.task_no } : null
  moveVisible.value = true
}

function openBatchMove() {
  moveIds.value = selected.value.map((r) => r.id)
  moveCurrent.value = null
  moveVisible.value = true
}

async function onMoved() {
  // 归属一改，任务的实报工时与升版提醒都会变，列表必须重新取而不是就地改
  await load()
  await refreshStats()
  if (selected.value.length) clearSelection()
}

onMounted(load)
</script>

<style scoped>
/* 台账表格：每格内容强制单行，超出即裁剪（省略号 + 悬停看全文），
   避免长产品名/批次把行高撑开，列宽则由 min-width 交给 el-table 自适应分配 */
.ledger-table :deep(.cell) {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  word-break: keep-all;
}
.ledger-table :deep(.el-table__cell) {
  padding: 5px 0;
}
.ledger-table :deep(th .cell) {
  font-weight: 600;
}

.mini-thumb {
  width: 62px;
  height: 44px;
  object-fit: contain;
  background: #f4f6f9;
  border-radius: 4px;
  cursor: pointer;
  border: 1px solid #e7ebf1;
}

/* 改挂：常态低调，鼠标到行上才明显 —— 它是纠正手段，不是日常主操作 */
.relink {
  margin-left: 8px;
  border: none;
  background: none;
  padding: 0;
  font-size: 11.5px;
  color: #3b6fd4;
  cursor: pointer;
  opacity: 0.55;
}
.relink:hover { opacity: 1; text-decoration: underline; }
.sel-tip { font-size: 12.5px; color: #8a5a12; font-weight: 600; }
</style>
