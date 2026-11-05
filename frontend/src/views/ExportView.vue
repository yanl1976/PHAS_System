<template>
  <div class="page">
    <div class="page-head">
      <span class="page-title">导出与勾稽</span>
      <span class="page-sub">三级勾稽校验通过后方可导出正式汇总单</span>
      <div class="spacer"></div>
      <el-button size="small" @click="load">重新校验</el-button>
    </div>

    <div class="kpi-row">
      <div class="kpi" :class="report.balanced ? 'ok' : 'danger'">
        <div class="kpi-label">勾稽结论</div>
        <div class="kpi-value" style="font-size: 20px">{{ report.balanced ? '平' : '不平' }}</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">明细合计</div>
        <div class="kpi-value">{{ fmt(report.levels?.detail_sum) }}<span class="kpi-unit">h</span></div>
      </div>
      <div class="kpi">
        <div class="kpi-label">人员合计</div>
        <div class="kpi-value">{{ fmt(report.levels?.person_sum) }}<span class="kpi-unit">h</span></div>
      </div>
      <div class="kpi" :class="Math.abs(report.levels?.diff || 0) > 0.5 ? 'danger' : ''">
        <div class="kpi-label">差额（容差 {{ report.tolerance }}h）</div>
        <div class="kpi-value">{{ fmt(report.levels?.diff) }}</div>
      </div>
      <div class="kpi warn">
        <div class="kpi-label">未确认工时</div>
        <div class="kpi-value">{{ fmt(report.checks?.pending_hours) }}<span class="kpi-unit">h</span></div>
      </div>
    </div>

    <div class="card">
      <div class="section-title">校验项</div>
      <div v-if="!report.issues?.length" class="alert-bar ok">
        <el-icon><CircleCheckFilled /></el-icon> 未发现问题，可以导出
      </div>
      <div v-for="(it, i) in report.issues" :key="i" class="alert-bar" :class="it.level === 'error' ? 'danger' : ''">
        <el-icon><WarningFilled v-if="it.level === 'error'" /><InfoFilled v-else /></el-icon>
        <b>{{ it.code }}</b>
        <span>{{ it.message }}</span>
      </div>

      <el-collapse v-if="hasDetail" style="margin-top: 12px">
        <el-collapse-item title="查看明细差异" name="detail">
          <div v-if="report.checks?.unassigned?.length">
            <div class="section-title" style="font-size: 13px">无人员归属</div>
            <el-table :data="report.checks.unassigned" size="small" max-height="180">
              <el-table-column prop="sheet_id" label="单据" width="80" />
              <el-table-column prop="worker_raw" label="识别姓名" />
              <el-table-column prop="hours" label="工时" width="100" />
            </el-table>
          </div>
          <div v-if="report.checks?.out_of_period?.length" style="margin-top: 10px">
            <div class="section-title" style="font-size: 13px">日期跨期</div>
            <el-table :data="report.checks.out_of_period" size="small" max-height="180">
              <el-table-column prop="sheet_id" label="单据" width="80" />
              <el-table-column prop="worker" label="人员" />
              <el-table-column prop="date" label="日期" width="120" />
            </el-table>
          </div>
          <div v-if="report.checks?.zero_qty?.length" style="margin-top: 10px">
            <div class="section-title" style="font-size: 13px">有单件工时但合格件数为 0</div>
            <el-table :data="report.checks.zero_qty" size="small" max-height="180">
              <el-table-column prop="sheet_id" label="单据" width="80" />
              <el-table-column prop="worker" label="人员" />
              <el-table-column prop="unit_hours" label="单件工时" width="110" />
            </el-table>
          </div>
          <div v-if="report.checks?.duplicates?.length" style="margin-top: 10px">
            <div class="section-title" style="font-size: 13px">疑似重复</div>
            <el-table :data="report.checks.duplicates" size="small" max-height="180">
              <el-table-column prop="count" label="条数" width="70" />
              <el-table-column label="标识">
                <template #default="{ row }">{{ row.key.join(' / ') }}</template>
              </el-table-column>
            </el-table>
          </div>
        </el-collapse-item>
      </el-collapse>
    </div>

    <div class="card">
      <div class="section-title">人员合计（勾稽明细）</div>
      <el-table :data="report.per_person" size="small" max-height="300">
        <el-table-column prop="worker" label="人员" width="120" />
        <el-table-column prop="items" label="明细行数" width="110" />
        <el-table-column label="工时" width="120">
          <template #default="{ row }"><b class="hours-cell">{{ row.hours.toFixed(2) }}</b></template>
        </el-table-column>
        <el-table-column label="占比">
          <template #default="{ row }">
            <div class="bar-track">
              <div class="bar-fill" :style="{ width: pct(row.hours) }"></div>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <div class="card">
      <div class="section-title">导出</div>
      <div class="row-flex">
        <el-button type="primary" :disabled="!report.balanced" @click="doExport(false)">
          <el-icon><Download /></el-icon>导出正式汇总单
        </el-button>
        <el-button @click="doExport(true)">导出预览版（含未确认）</el-button>
        <span class="muted" style="font-size: 12.5px">
          每人一个 sheet + 总工时台账 + 口径说明；工时列为小时小数，另附「合计(时:分)」列
        </span>
      </div>

      <div v-if="!report.balanced" class="alert-bar danger" style="margin-top: 12px">
        <el-icon><WarningFilled /></el-icon> 勾稽未通过，正式导出已锁定。可先导出预览版排查。
      </div>

      <div class="section-title" style="margin-top: 18px">历史导出</div>
      <el-table :data="history" size="small">
        <el-table-column prop="name" label="文件名" show-overflow-tooltip />
        <el-table-column label="大小" width="100">
          <template #default="{ row }">{{ (row.size / 1024).toFixed(1) }} KB</template>
        </el-table-column>
        <el-table-column prop="created_at" label="生成时间" width="180" />
        <el-table-column label="操作" width="90">
          <template #default="{ row }">
            <a :href="row.url" target="_blank" class="link">下载</a>
          </template>
        </el-table-column>
      </el-table>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api'
import { store } from '../store'

const report = ref({ levels: {}, checks: {}, issues: [], per_person: [] })
const history = ref([])

const hasDetail = computed(() => {
  const c = report.value.checks || {}
  return ['unassigned', 'out_of_period', 'zero_qty', 'duplicates'].some((k) => (c[k] || []).length)
})

const maxHours = computed(() => Math.max(1, ...(report.value.per_person || []).map((p) => p.hours)))

function pct(h) {
  return `${Math.max(1, (h / maxHours.value) * 100)}%`
}

function fmt(v) {
  return Number(v || 0).toFixed(2)
}

async function load() {
  report.value = await api.reconcile(store.ym)
  history.value = await api.exportHistory()
}

function doExport(preview) {
  const url = api.exportUrl(store.ym, preview)
  window.open(url, '_blank')
  ElMessage.success(preview ? '正在生成预览版' : '正在生成正式汇总单')
  setTimeout(load, 1500)
}

onMounted(load)
</script>

<style scoped>
.link { color: #2f7be5; text-decoration: none; }
</style>
