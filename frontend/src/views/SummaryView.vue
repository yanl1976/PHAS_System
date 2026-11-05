<template>
  <div class="page">
    <div class="page-head">
      <span class="page-title">月度汇总</span>
      <span class="page-sub">{{ store.ym }} · 汇总口径：合格件数 × 单件工时，仅统计已确认单据</span>
      <div class="spacer"></div>
      <el-checkbox v-model="includeUnverified" size="small" @change="load">含未确认单据（预览）</el-checkbox>
      <el-button size="small" @click="load">刷新</el-button>
    </div>

    <div class="kpi-row">
      <div class="kpi accent">
        <div class="kpi-label">总工时</div>
        <div class="kpi-value">{{ data.totals.hours?.toFixed(1) ?? 0 }}<span class="kpi-unit">h</span></div>
      </div>
      <div class="kpi">
        <div class="kpi-label">参与人员</div>
        <div class="kpi-value">{{ data.totals.persons ?? 0 }}</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">单据数</div>
        <div class="kpi-value">{{ data.totals.sheets ?? 0 }}</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">明细行</div>
        <div class="kpi-value">{{ data.totals.items ?? 0 }}</div>
      </div>
      <div class="kpi warn">
        <div class="kpi-label">人均工时</div>
        <div class="kpi-value">{{ avgHours }}</div>
      </div>
    </div>

    <div class="card">
      <div class="section-title">人员 × 工序 交叉表</div>
      <el-table :data="matrix.rows" size="small" class="matrix-table" v-loading="loading" max-height="420">
        <el-table-column prop="worker_name" label="人员" width="110" fixed />
        <el-table-column v-for="(p, i) in matrix.processes" :key="p" :label="p" width="92" align="right">
          <template #default="{ row }">
            <span :class="{ muted: !row.cells[i] }">{{ row.cells[i] ? row.cells[i].toFixed(2) : '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="合计" width="100" align="right" fixed="right">
          <template #default="{ row }">
            <b class="hours-cell">{{ row.total.toFixed(2) }}</b>
          </template>
        </el-table-column>
      </el-table>
      <div v-if="matrix.rows.length" class="row-flex" style="justify-content: flex-end; padding-top: 8px; font-size: 13px">
        <span class="muted">工序合计：</span>
        <span v-for="(t, i) in matrix.columns_total" :key="i" style="margin-right: 14px">
          {{ matrix.processes[i] }} <b class="mono">{{ t.toFixed(2) }}</b>
        </span>
        <el-divider direction="vertical" />
        <span>总计 <b class="mono">{{ matrix.grand_total?.toFixed(2) }}</b> h</span>
      </div>
    </div>

    <div class="card">
      <div class="section-title">人员工时排行</div>
      <div v-for="p in data.persons" :key="p.worker_name" class="person-row">
        <div class="person-name">{{ p.worker_name }}</div>
        <div class="person-bar">
          <div class="bar-track">
            <div class="bar-fill" :style="{ width: barWidth(p.hours) }"></div>
          </div>
        </div>
        <div class="person-meta">
          <span class="muted">{{ p.item_count }} 行 / {{ p.sheet_count }} 单</span>
          <span class="tag-list">
            <el-tag v-for="pr in p.processes" :key="pr.process" size="small" effect="plain">
              {{ pr.process }} {{ pr.hours.toFixed(2) }}
            </el-tag>
          </span>
        </div>
        <div class="person-hours hours-cell">{{ p.hours.toFixed(2) }} h</div>
      </div>
      <el-empty v-if="!data.persons?.length" description="本核算期暂无已确认单据" :image-size="70" />
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'
import { store } from '../store'

const data = ref({ persons: [], totals: {}, processes: [], products: [] })
const matrix = ref({ rows: [], processes: [], columns_total: [], grand_total: 0 })
const includeUnverified = ref(false)
const loading = ref(false)

const avgHours = computed(() => {
  const t = data.value.totals || {}
  if (!t.persons) return '0.0'
  return (t.hours / t.persons).toFixed(1)
})

const maxHours = computed(() => Math.max(1, ...(data.value.persons || []).map((p) => p.hours)))

function barWidth(h) {
  return `${Math.max(2, (h / maxHours.value) * 100)}%`
}

async function load() {
  loading.value = true
  try {
    const [s, m] = await Promise.all([
      api.summary(store.ym, includeUnverified.value),
      api.matrix(store.ym)
    ])
    data.value = s
    matrix.value = m
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.person-row {
  display: grid;
  grid-template-columns: 84px 1fr 320px 90px;
  align-items: center;
  gap: 12px;
  padding: 7px 0;
  border-bottom: 1px dashed #eef1f6;
}

.person-name { font-size: 13px; font-weight: 500; }
.person-meta { display: flex; align-items: center; gap: 8px; font-size: 12px; flex-wrap: wrap; }
.person-hours { text-align: right; }
</style>
