<template>
  <div class="page">
    <div class="page-head">
      <span class="page-title">工资核算</span>
      <span class="page-sub">
        {{ store.ym }} · 口径：报工工时（合格件数 × 单件工时）× 该月生效工时单价；识别单据需已确认，扫码报工提交即入账
      </span>
      <div class="spacer"></div>
      <el-checkbox v-model="includeUnverified" size="small" @change="load">
        含未确认单据（预览）
      </el-checkbox>
      <el-button size="small" @click="goRates">设置工时单价</el-button>
      <el-button size="small" @click="load">刷新</el-button>
      <el-button size="small" @click="openExports">最近导出</el-button>
      <el-button size="small" type="primary" :loading="exporting" @click="doExport()">
        <el-icon><Download /></el-icon>导出工资表
      </el-button>
    </div>

    <!-- 下载被浏览器拦掉时（内嵌预览、禁下载策略），这里还能直接把文件拿下来 -->
    <el-dialog v-model="exportsVisible" title="最近导出的工时工资表" width="620px">
      <el-table :data="exports" size="small" empty-text="还没有导出过工资表">
        <el-table-column prop="name" label="文件" min-width="220" show-overflow-tooltip />
        <el-table-column label="大小" width="72" align="center">
          <template #default="{ row }">{{ fmtSize(row.size) }}</template>
        </el-table-column>
        <el-table-column label="生成时间" width="152" align="center">
          <template #default="{ row }">
            <span class="dl-time">{{ (row.created_at || '').replace('T', ' ') }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="76" align="center">
          <template #default="{ row }">
            <a :href="row.url" target="_blank" class="dl-link">下载</a>
          </template>
        </el-table-column>
      </el-table>
      <template #footer>
        <span class="dl-hint">文件生成在服务端 data/exports 目录，本列表与「导出与勾稽」共用</span>
        <el-button size="small" @click="exportsVisible = false">关闭</el-button>
      </template>
    </el-dialog>

    <!-- 两件事会让人发出错的工资：有人没单价、还有单据没确认。都摆在最上面。 -->
    <el-alert
      v-if="data.totals.missing_rate"
      type="warning"
      :closable="false"
      show-icon
      class="banner"
      title="有人还没设工时单价，他们的金额按 0 元算"
    >
      <template #default>
        <span class="banner-text">
          {{ data.totals.missing_rate }} 人、共 {{ fmt(data.totals.missing_hours) }} 小时未定价
          <span v-if="data.missing?.length">（{{ missingNames }}）</span>
          —— 这些人不是「工资 0 元」，是「算不出来」，发出前必须补齐。
        </span>
        <el-button size="small" text type="primary" @click="goRates">去设置</el-button>
      </template>
    </el-alert>
    <el-alert
      v-if="data.totals.pending_sheets"
      type="info"
      :closable="false"
      show-icon
      class="banner"
      title="本核算期还有报工单没确认入账，这份工资表只是半成品"
    >
      <template #default>
        <span class="banner-text">
          还有 {{ data.totals.pending_sheets }} 张单据未确认。确认后工时会补进来，工资随之变化。
        </span>
        <el-button size="small" text type="primary" @click="goSheets">去核对</el-button>
      </template>
    </el-alert>

    <div class="kpi-row">
      <div class="kpi accent">
        <div class="kpi-label">合计工资</div>
        <div class="kpi-value">
          <span class="kpi-unit">¥</span>{{ money(data.totals.amount) }}
        </div>
      </div>
      <div class="kpi ok">
        <div class="kpi-label">报工工时</div>
        <div class="kpi-value">{{ fmt(data.totals.hours) }}<span class="kpi-unit">h</span></div>
      </div>
      <div class="kpi">
        <div class="kpi-label">参与人员</div>
        <div class="kpi-value">{{ data.totals.persons ?? 0 }}</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">加权平均单价</div>
        <div class="kpi-value">{{ fmt(data.totals.avg_rate) }}<span class="kpi-unit">元/h</span></div>
      </div>
      <div class="kpi" :class="{ warn: data.totals.missing_rate }">
        <div class="kpi-label">未设单价</div>
        <div class="kpi-value">{{ data.totals.missing_rate ?? 0 }}<span class="kpi-unit">人</span></div>
      </div>
    </div>

    <div class="card">
      <div class="section-title">
        人员工资明细
        <span class="muted">按金额降序 · 单价取「生效月 ≤ {{ store.ym }}」的最近一条</span>
      </div>
      <el-table
        :data="data.persons"
        size="small"
        class="wage-table"
        v-loading="loading"
        max-height="520"
        :default-sort="{ prop: 'amount', order: 'descending' }"
      >
        <el-table-column prop="worker_name" label="人员" min-width="92" fixed />
        <el-table-column prop="process" label="工种" min-width="96" show-overflow-tooltip />
        <el-table-column label="工时" width="96" align="right">
          <template #default="{ row }">
            <b class="mono">{{ fmt(row.hours) }}</b>
          </template>
        </el-table-column>
        <el-table-column label="工时单价" width="104" align="right">
          <template #default="{ row }">
            <span v-if="row.has_rate" class="mono">{{ fmt(row.rate) }}</span>
            <el-tag v-else size="small" type="warning" effect="plain">未设单价</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="工时工资" width="118" align="right">
          <template #default="{ row }">
            <b class="money-cell" :class="{ muted: !row.has_rate }">{{ money(row.amount) }}</b>
          </template>
        </el-table-column>
        <el-table-column label="工序构成" min-width="200">
          <template #default="{ row }">
            <span class="tag-list">
              <el-tag v-for="pr in row.processes" :key="pr.process" size="small" effect="plain">
                {{ pr.process }} {{ fmt(pr.hours) }}
              </el-tag>
            </span>
          </template>
        </el-table-column>
        <el-table-column label="明细" width="88" align="right">
          <template #default="{ row }">
            <span class="muted">{{ row.item_count }} 行 / {{ row.sheet_count }} 单</span>
          </template>
        </el-table-column>
      </el-table>

      <el-empty
        v-if="!data.persons?.length"
        description="本核算期暂无已确认的报工单"
        :image-size="70"
      />

      <div v-if="data.persons?.length" class="total-row">
        <span class="muted">合计</span>
        <span>工时 <b class="mono">{{ fmt(data.totals.hours) }}</b> h</span>
        <span>工资 <b class="mono money-cell">{{ money(data.totals.amount) }}</b> 元</span>
      </div>
    </div>

    <div class="card tip-card">
      <div class="section-title">这张工资表怎么来的</div>
      <ol class="calc-list">
        <li>
          工人按工艺卡干完活，回报工单：加工件数、合格 / 返修 / 废品、单件与合计工时、检验员、日期。
        </li>
        <li>
          系统重算工时：<b>合格件数 × 单件工时</b>（返修、废品的折算系数默认 0，即不计工时）；
          手写合计只用来比对差额，不进工资。
        </li>
        <li>
          工时按核算期、按人汇总，乘以该月生效的工时单价，得到个人工资；
          合计金额由各人金额相加，不拿总工时乘平均单价 —— 那样会差几分钱。
        </li>
        <li>
          单价在「系统管理 → 工时单价」维护，按生效月留档：调价只影响当月及以后，
          已经发过的月份不会变。
        </li>
      </ol>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api'
import { store } from '../store'

const router = useRouter()
const data = ref({ persons: [], totals: {}, missing: [] })
const includeUnverified = ref(false)
const loading = ref(false)
const exporting = ref(false)
const exports = ref([])
const exportsVisible = ref(false)

const missingNames = computed(() =>
  (data.value.missing || [])
    .slice(0, 6)
    .map((x) => x.worker_name)
    .join('、')
)

function fmt(v) {
  return Number(v || 0).toFixed(2)
}

function money(v) {
  return Number(v || 0).toLocaleString('zh-CN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2
  })
}

async function load() {
  loading.value = true
  try {
    data.value = await api.wages(store.ym, includeUnverified.value)
  } catch (e) {
    if (e?.response?.status === 404) {
      data.value = { persons: [], totals: {}, missing: [] }
      ElMessage.warning(e.response.data?.detail || '该核算期不存在')
    } else throw e
  } finally {
    loading.value = false
  }
}

function goRates() {
  router.push({ path: '/admin', query: { card: 'rates' } })
}

function goSheets() {
  router.push('/sheets')
}

// 导出前的「能不能导」检查：与后端同一口径，只是提前把理由摆到桌面上。
// 后端也会拦（不能靠前端守门），但让人下回来一个半成品工资表、
// 打开才发现少了人，比当场告诉他为什么不能导要糟得多。
async function exportBlockers() {
  const t = data.value?.totals || {}
  const out = []
  if (t.pending_sheets) out.push(`${t.pending_sheets} 张识别单还在待核对，这部分工时没进账`)
  if (t.missing_rate) out.push(`${t.missing_rate} 人没设工时单价（${t.missing_hours} h 会算成 0 元）`)
  try {
    const rep = await api.reconcile(store.ym)
    for (const it of rep.issues || []) {
      if (it.level === 'error') out.push(it.message)
    }
  } catch (e) {
    out.push('勾稽校验没跑起来，无法确认账是否平')
  }
  return out
}

async function doExport(preview = false) {
  // 空月份让它导出，只会得到一个「只有表头」的空表，人还以为导出坏了
  if (!(data.value?.persons || []).length) {
    ElMessage.warning('该核算期没有可核算的报工数据，先确认单据或换个月份')
    return
  }
  if (!preview) {
    const problems = await exportBlockers()
    if (problems.length) {
      try {
        await ElMessageBox.confirm(
          `工资表还不能作为发放依据：\n\n${problems.map((p, i) => `${i + 1}. ${p}`).join('\n')}\n\n可以先导出「预览版」核对数据（文件名带 preview，不能拿去发钱）。`,
          '还不能导出工资表',
          { type: 'warning', confirmButtonText: '导出预览版', cancelButtonText: '回去处理' }
        )
      } catch (e) {
        return
      }
      preview = true
    }
  }
  exporting.value = true
  try {
    // 走同源 URL 下载，而不是取 blob 再 <a download>：
    // blob 下载在内嵌浏览器/受限环境里会被静默拦掉（表现为「点了没反应」）。
    // target=_blank 是必须的：接口报错时返回的是 JSON 而不是附件，
    // 同标签导航会把整个页面跳到那串 JSON 上 —— 页面就「丢」了。
    const url = api.wageExportUrl(store.ym, includeUnverified.value, preview)
    const a = document.createElement('a')
    a.href = url
    a.target = '_blank'
    a.rel = 'noopener'
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    ElMessage.success(
      preview
        ? '已导出预览版（仅供核对，不能作为发放依据）'
        : '已开始生成工资表，稍候在浏览器下载栏查看'
    )
    // 文件是后端现算现存的，落到 data/exports 需要一点时间；
    // 晚一点刷新「最近导出」，万一浏览器把下载拦了，还有这条路可走
    setTimeout(loadExports, 2500)
  } catch (e) {
    ElMessage.error(e?.message || '导出失败')
  } finally {
    setTimeout(() => (exporting.value = false), 800)
  }
}

async function loadExports() {
  try {
    const rows = await api.wageExports()
    exports.value = (rows || []).filter((x) => x.name.includes('工时工资表')).slice(0, 8)
  } catch (e) {
    exports.value = []
  }
}

function openExports() {
  exportsVisible.value = true
  loadExports()
}

function fmtSize(n) {
  if (!n) return '-'
  return n > 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.round(n / 1024)} KB`
}

// 顶栏切核算期就跟着算 —— 工资是月度概念，切了月份还显示上个月的钱最容易出事
watch(() => store.ym, load)

onMounted(load)
</script>

<style scoped>
.banner { margin-bottom: 10px; }
.banner-text { font-size: 12.5px; line-height: 1.6; }
.kpi-unit { font-size: 12px; margin: 0 2px; color: #6b7280; }

.money-cell { font-variant-numeric: tabular-nums; }
.total-row {
  display: flex;
  align-items: center;
  gap: 20px;
  justify-content: flex-end;
  padding-top: 10px;
  font-size: 13px;
}
.tag-list { display: inline-flex; flex-wrap: wrap; gap: 4px; }

.calc-list { margin: 4px 0 0 0; padding-left: 20px; font-size: 12.5px; line-height: 1.9; color: #4b5565; }
.calc-list b { color: #2b3444; }

.dl-link { color: #2f7be5; text-decoration: none; }
.dl-link:hover { text-decoration: underline; }
/* 时间戳不能被折断成两行：折了就分不清哪行是哪行 */
.dl-time { white-space: nowrap; font-variant-numeric: tabular-nums; }
.dl-hint { font-size: 12px; color: #8a93a4; margin-right: 12px; }
</style>
