<template>
  <div class="page">
    <div class="page-head">
      <span class="page-title">工时组合</span>
      <span class="page-sub">
        产品 × 件名 是工艺的身份。默认全是草稿——工艺员点「发布」之后，这条工艺才会出现在任务挂线、单据核对和工艺卡里
      </span>
      <div class="spacer"></div>
      <el-button size="small" type="primary" :disabled="!summary.ready" @click="openBatch">
        批量发布{{ summary.ready ? `（${summary.ready}）` : '' }}
      </el-button>
      <el-button size="small" @click="openCreate">新增组合</el-button>
      <el-button size="small" @click="reload">刷新</el-button>
    </div>

    <div class="summary-bar">
      <span class="sb-item"><span class="muted">组合</span><b class="mono">{{ summary.total ?? 0 }}</b></span>
      <span class="sb-sep"></span>
      <span class="sb-item"><span class="muted">已发布</span><b class="mono ok">{{ summary.released ?? 0 }}</b></span>
      <span class="sb-item"><span class="muted">草稿</span><b class="mono">{{ summary.draft ?? 0 }}</b></span>
      <span class="sb-item"><span class="muted">已归档（停用）</span><b class="mono">{{ summary.archived ?? 0 }}</b></span>
      <span class="sb-item" :class="{ warn: summary.demoted }">
        <span class="muted">待确认（迁移降级）</span><b class="mono">{{ summary.demoted ?? 0 }}</b>
      </span>
      <span class="sb-item"><span class="muted">可发布</span><b class="mono">{{ summary.ready ?? 0 }}</b></span>
      <span class="sb-item"><span class="muted">无路线</span><b class="mono">{{ summary.unbuilt ?? 0 }}</b></span>
      <span class="sb-sep"></span>
      <span class="sb-item" :class="{ warn: summary.issue }">
        <span class="muted">产品名为空</span><b class="mono">{{ summary.issue ?? 0 }}</b>
      </span>
      <div class="spacer"></div>
      <span class="sb-note muted">未发布的工艺不参与任务挂线、核对校验与工艺卡</span>
    </div>

    <div class="card">
      <div class="row-flex tools">
        <el-input
          v-model="kw"
          size="small"
          placeholder="搜索产品或件名"
          clearable
          style="width: 190px"
          @keyup.enter="reload"
          @clear="reload"
        />
        <el-select v-model="state" size="small" style="width: 124px" @change="reload">
          <el-option value="" label="全部状态" />
          <el-option value="released" label="已发布" />
          <el-option value="draft" label="草稿" />
          <el-option value="archived" label="已归档" />
          <el-option value="none" label="无路线" />
        </el-select>
        <el-checkbox v-model="onlyIssue" size="small" @change="reload">只看产品名为空</el-checkbox>
        <el-checkbox v-model="onlyActive" size="small" @change="reload">只看历史工时在用</el-checkbox>
        <el-button size="small" @click="reload">查询</el-button>
        <div class="spacer"></div>
        <el-button size="small" type="primary" plain @click="openStructure(null)">产品结构</el-button>
      </div>

      <!-- 清单：台账。结构调整不在这里做，走上面的「产品结构」弹窗 -->
        <el-table :data="rows" size="small" v-loading="loading" style="width: 100%" height="calc(100vh - 344px)">
          <el-table-column label="产品" min-width="160" show-overflow-tooltip>
            <template #default="{ row }">
              <span v-if="row.product">{{ row.product }}</span>
              <el-tag v-else size="small" type="danger" effect="plain">产品名为空</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="part" label="件名" min-width="140" show-overflow-tooltip />
          <!-- 层级与上级合成一列：光有「部件」两个字看不出它装在哪台机器的哪一块上，
               装配关系要一眼能看到，所以把上级件名直接写出来；点这一格就能改 -->
          <el-table-column label="层级 · 装在" width="186">
            <template #default="{ row }">
              <div class="lv-cell" title="点击打开产品结构，调整它装在哪" @click="openStructure(row)">
                <span class="mono lv" :class="'lv' + row.level">{{ levelText(row.level) }}</span>
                <span v-if="row.parent_part" class="lv-at">装在</span>
                <span class="lv-parent" :class="{ none: !row.parent_part }">
                  {{ row.parent_part ? row.parent_part : '最上层' }}
                </span>
                <el-tooltip v-if="row.level_src === 'manual'" content="层级由人工指定" placement="top">
                  <el-icon class="lv-manual"><EditPen /></el-icon>
                </el-tooltip>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="工序" width="66" align="right">
            <template #default="{ row }">
              <span class="mono">{{ row.has_route ? row.step_count : '—' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="定额合计" width="96" align="right">
            <template #default="{ row }">
              <span class="mono">{{ row.has_route ? (row.route_hours || 0).toFixed(3) : '—' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="历史样本" width="82" align="right">
            <template #default="{ row }">
              <span class="mono">{{ row.samples || '—' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="104">
            <template #default="{ row }">
              <el-tag size="small" :type="stateType(row.status)" effect="plain">{{ stateText(row.status) }}</el-tag>
              <el-tooltip v-if="row.demoted_from" content="原为迁移脚本批量标记的已发布，未经工艺员确认" placement="top">
                <el-tag size="small" type="warning" effect="plain" style="margin-left: 3px">待确认</el-tag>
              </el-tooltip>
            </template>
          </el-table-column>
          <el-table-column label="发布" width="92" align="center">
            <template #default="{ row }">
              <el-button
                v-if="row.status === 'draft'"
                size="small"
                type="primary"
                :disabled="!row.has_route"
                @click="openRelease(row)"
              >
                发布
              </el-button>
              <el-button v-else-if="row.status === 'released'" size="small" text @click="doWithdraw(row)">
                撤回
              </el-button>
              <span v-else class="muted">—</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="318" fixed="right">
            <template #default="{ row }">
              <el-button size="small" text type="primary" @click="openDetail(row)">实测工时</el-button>
              <el-button size="small" text @click="openRename(row)">改名</el-button>
              <el-button size="small" text @click="openStructure(row)">结构</el-button>
              <el-button size="small" text :type="row.active ? 'warning' : 'success'" @click="toggleActive(row)">
                {{ row.active ? '停用' : '启用' }}
              </el-button>
              <el-button size="small" text type="primary" :disabled="!row.product" @click="goBuild(row)">
                编排
              </el-button>
            </template>
          </el-table-column>
        </el-table>

        <div class="row-flex" style="justify-content: flex-end; margin-top: 10px">
          <el-pagination
            v-model:current-page="page"
            :page-size="size"
            :total="total"
            layout="prev, pager, next, total"
            @current-change="load"
          />
        </div>
    </div>

    <!-- 发布确认 -->
    <el-dialog v-model="releaseVisible" title="确认并发布工艺" width="min(560px, 94vw)">
      <div v-if="releaseTarget" class="rel-body">
        <div class="rel-title">
          <b>{{ releaseTarget.product || '（产品名为空）' }}</b>
          <span class="muted"> / {{ releaseTarget.part }}</span>
        </div>
        <div class="rel-meta muted">
          第 {{ releaseTarget.version || 1 }} 版 · {{ releaseStepCount }} 道工序 · 定额合计
          {{ (releaseHours || 0).toFixed(3) }} h
        </div>

        <el-alert
          v-if="releaseBlocks.length"
          type="error"
          :closable="false"
          show-icon
          class="rel-alert"
          :title="`有 ${releaseBlocks.length} 项不能发布，先修好再来`"
        >
          <div v-for="(x, i) in releaseBlocks" :key="i">· {{ x.message }}</div>
        </el-alert>
        <el-alert
          v-for="(x, i) in releaseWarns"
          :key="'w' + i"
          type="warning"
          :closable="false"
          show-icon
          class="rel-alert"
          :title="x.message"
        />

        <el-alert
          type="info"
          :closable="false"
          show-icon
          class="rel-alert"
          title="发布后：这条工艺会出现在任务挂线、单据核对的多做/漏做判断、以及工艺卡里。撤回会让它立刻从这三处消失。"
        />

        <el-form label-width="62px" style="margin-top: 8px">
          <el-form-item label="备注">
            <el-input
              v-model="releaseNote"
              size="small"
              placeholder="可不填；与历史实测偏差大时建议写清依据"
            />
          </el-form-item>
        </el-form>
      </div>
      <template #footer>
        <el-button size="small" @click="releaseVisible = false">取消</el-button>
        <el-button
          size="small"
          type="primary"
          :disabled="releaseBlocks.length > 0"
          :loading="releasing"
          @click="submitRelease"
        >
          确认发布
        </el-button>
      </template>
    </el-dialog>

    <!-- 批量发布 -->
    <el-dialog v-model="batchVisible" title="批量确认发布" width="min(720px, 96vw)">
      <div v-loading="batchLoading">
        <div class="row-flex" style="gap: 10px; margin-bottom: 10px">
          <span class="muted">产品</span>
          <el-select v-model="batchProduct" size="small" clearable placeholder="全部产品" style="width: 220px" @change="previewBatch">
            <el-option v-for="p in productOptions" :key="p.product" :value="p.product" :label="`${p.product}（${p.draft} 条草稿）`" />
          </el-select>
          <div class="spacer"></div>
          <span class="muted">
            将发布 <b class="mono">{{ batchPre.released?.length || 0 }}</b> 条，
            跳过 <b class="mono">{{ batchPre.skipped?.length || 0 }}</b> 条
          </span>
        </div>

        <el-alert
          type="warning"
          :closable="false"
          show-icon
          class="rel-alert"
          title="只发布「有路线、有工序、定额大于 0」的。没建线的不会被顺手发出一个空基准。发布前请确认这批工艺工艺员已经看过。"
        />

        <div class="batch-cols">
          <div class="batch-col">
            <div class="batch-head">将发布（{{ batchPre.released?.length || 0 }}）</div>
            <div class="batch-list">
              <div v-for="x in batchPre.released || []" :key="x.id" class="batch-item">
                <span class="mono">{{ x.product || '(空)' }}</span> / {{ x.part }}
              </div>
              <div v-if="!(batchPre.released || []).length" class="muted">没有可发布的</div>
            </div>
          </div>
          <div class="batch-col">
            <div class="batch-head">跳过（{{ batchPre.skipped?.length || 0 }}）</div>
            <div class="batch-list">
              <div v-for="x in batchPre.skipped || []" :key="'s' + x.id" class="batch-item skip">
                <span class="mono">{{ x.product || '(空)' }}</span> / {{ x.part }}
                <span class="muted">— {{ x.reason }}</span>
              </div>
              <div v-if="!(batchPre.skipped || []).length" class="muted">无</div>
            </div>
          </div>
        </div>
      </div>
      <template #footer>
        <el-button size="small" @click="batchVisible = false">取消</el-button>
        <el-button
          size="small"
          type="primary"
          :disabled="!(batchPre.released || []).length"
          :loading="batchRunning"
          @click="doBatchRelease"
        >
          发布这 {{ batchPre.released?.length || 0 }} 条
        </el-button>
      </template>
    </el-dialog>

    <!-- 实测工时明细 -->
    <el-drawer v-model="detailVisible" size="620px" :title="detailTitle">
      <div v-loading="detailLoading">
        <div class="row-flex detail-meta">
          <el-tag v-if="detail.combo?.status" size="small" :type="stateType(detail.combo.status)">
            {{ stateText(detail.combo.status) }}
          </el-tag>
          <span class="muted">共 {{ detail.steps?.length || 0 }} 道工序 · 单件合计 {{ detail.unit_sum || 0 }} h</span>
          <div class="spacer"></div>
          <el-button size="small" :disabled="!detail.product" @click="goBuild(detail)">去编排</el-button>
        </div>

        <el-alert
          type="info"
          :closable="false"
          show-icon
          class="detail-tip"
          title="这些是历史实测统计出的单件工时，改它只影响「核对时的比对基准」和「建线建议」，不会改已识别单据已算出的工时。"
        />

        <el-table :data="detail.steps || []" size="small" style="width: 100%">
          <el-table-column label="工序" min-width="120">
            <template #default="{ row }">
              <el-input v-model="row.process" size="small" :disabled="row.id > 0" placeholder="工序名" />
            </template>
          </el-table-column>
          <el-table-column label="单件工时 (h)" width="140">
            <template #default="{ row }">
              <el-input-number
                v-model="row.unit_hours"
                size="small"
                :min="0"
                :step="0.1"
                :precision="4"
                :controls="false"
                style="width: 100%"
              />
            </template>
          </el-table-column>
          <el-table-column label="样本" width="76" align="right">
            <template #default="{ row }">
              <span class="mono">{{ row.id ? row.samples : '—' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="108" align="right">
            <template #default="{ row }">
              <el-button v-if="row.id" size="small" text type="primary" @click="saveStep(row)">保存</el-button>
              <el-button size="small" text type="danger" @click="removeStep(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>

        <div class="row-flex" style="margin-top: 10px">
          <el-button size="small" @click="addStepRow">加一道工序</el-button>
        </div>
      </div>
    </el-drawer>

    <!-- 改名 -->
    <el-dialog v-model="renameVisible" title="修改产品 / 件名" width="min(520px, 94vw)">
      <el-alert
        type="warning"
        :closable="false"
        show-icon
        style="margin-bottom: 12px"
        title="历史工时与已建路线会一起改名，否则同一组合会在台账里变成两条。"
      />
      <el-form label-width="88px">
        <el-form-item label="产品">
          <el-input v-model="renameForm.new_product" size="small" placeholder="留空表示不改" />
        </el-form-item>
        <el-form-item label="件名">
          <el-input v-model="renameForm.new_part" size="small" placeholder="留空表示不改" />
        </el-form-item>
        <el-form-item v-if="renameForm.state === 'released'">
          <span class="muted">该组合已发布，改名会同步改路线（旧版可追溯性不受影响）</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button size="small" @click="renameVisible = false">取消</el-button>
        <el-button size="small" type="primary" :loading="renaming" @click="submitRename">确定改名</el-button>
      </template>
    </el-dialog>


    <!-- 产品结构（装配关系）：总装 → 部件 → 子件。
         结构和工艺是两件独立的事，所以它有自己的窗口，不在台账页里就地改。
         尺寸与编排页保持一致（缩 30%）：窗口一宽，行尾导航键离件名就太远了 -->
    <el-dialog
      v-model="structVisible"
      title="产品结构 · 总装 → 部件 → 子件"
      width="min(730px, 92vw)"
      top="7vh"
      destroy-on-close
    >
      <StructurePanel
        v-if="structVisible"
        :product="structProduct"
        :focus-part="structPart"
        @changed="onStructureChanged"
      />
    </el-dialog>


    <!-- 新增组合 -->
    <el-dialog v-model="createVisible" title="新增组合" width="min(560px, 94vw)">
      <el-alert
        type="info"
        :closable="false"
        show-icon
        style="margin-bottom: 10px"
        title="新建的组合是草稿，要在「编排」里建好路线再回来发布。"
      />
      <el-form label-width="88px">
        <el-form-item label="产品">
          <el-input v-model="createForm.product" size="small" placeholder="如 YUJ18BZ制丸机" />
        </el-form-item>
        <el-form-item label="件名">
          <el-input v-model="createForm.part" size="small" placeholder="如 刀具" />
        </el-form-item>
        <el-form-item label="工序">
          <div class="create-steps">
            <div v-for="(s, i) in createForm.steps" :key="i" class="create-step">
              <el-input v-model="s.process" size="small" placeholder="工序名" style="width: 130px" />
              <el-input-number v-model="s.unit_hours" size="small" :min="0" :step="0.1" :precision="4" style="width: 120px" />
              <span class="muted">小时</span>
              <el-button size="small" text type="danger" @click="createForm.steps.splice(i, 1)">删</el-button>
            </div>
            <el-button size="small" @click="createForm.steps.push({ process: '', unit_hours: 0 })">加一道</el-button>
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button size="small" @click="createVisible = false">取消</el-button>
        <el-button size="small" type="primary" :loading="creating" @click="submitCreate">创建</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { EditPen } from '@element-plus/icons-vue'
import { api } from '../api'
import StructurePanel from '../components/StructurePanel.vue'

const router = useRouter()
const route = useRoute()

const rows = ref([])
const total = ref(0)
const summary = ref({})
const page = ref(1)
const size = ref(50)
const kw = ref('')
const state = ref('')
const onlyIssue = ref(false)
const onlyActive = ref(false)
const loading = ref(false)

// 产品下拉只给「批量发布」用；结构树由结构弹窗自己拉
const productOptions = ref([])

const detailVisible = ref(false)
const detailLoading = ref(false)
const detail = ref({})
const detailTitle = computed(() => {
  const p = detail.value.product || '（产品名为空）'
  return `${p} / ${detail.value.part || ''}`
})

const renameVisible = ref(false)
const renaming = ref(false)
const renameForm = reactive({ product: '', part: '', new_product: '', new_part: '', state: '' })

const createVisible = ref(false)
const creating = ref(false)
const createForm = reactive({ product: '', part: '', steps: [] })

const releaseVisible = ref(false)
const releasing = ref(false)
const releaseTarget = ref(null)
const releaseNote = ref('')
const releaseBlocks = computed(() => (releaseTarget.value?.issues || []).filter((x) => x.level === 'block'))
const releaseWarns = computed(() => (releaseTarget.value?.issues || []).filter((x) => x.level === 'warn'))
const releaseStepCount = computed(() => releaseTarget.value?.step_count || 0)
const releaseHours = computed(() => releaseTarget.value?.unit_sum || 0)

const batchVisible = ref(false)
const batchLoading = ref(false)
const batchRunning = ref(false)
const batchProduct = ref('')
const batchPre = ref({ released: [], skipped: [] })

function stateText(s) {
  return { released: '已发布', draft: '草稿', archived: '已归档', none: '无路线' }[s] || s || '—'
}

function stateType(s) {
  return { released: 'success', draft: 'warning', archived: 'info', none: 'danger' }[s] || 'info'
}

function levelText(lv) {
  if (lv === 0) return '总装'
  if (lv === 1) return '部件'
  if (lv === 2) return '子件'
  return lv > 2 ? `L${lv}` : '—'
}

async function load() {
  loading.value = true
  try {
    const d = await api.combos({
      q: kw.value,
      state: state.value,
      only_issue: onlyIssue.value,
      include_disabled: !onlyActive.value,
      limit: size.value,
      offset: (page.value - 1) * size.value
    })
    rows.value = d.items || []
    total.value = d.total || 0
    summary.value = d.summary || {}
    syncQuery()
  } finally {
    loading.value = false
  }
}

function syncQuery() {
  const q = {}
  if (kw.value) q.q = kw.value
  if (state.value) q.state = state.value
  if (onlyIssue.value) q.issue = '1'
  if (onlyActive.value) q.active = '1'
  if (page.value > 1) q.page = String(page.value)
  router.replace({ path: '/combos', query: q })
}

function reload() {
  page.value = 1
  load()
  loadProducts()
}

// 批量发布弹窗的产品下拉要用；结构弹窗自己会拉全量结构，不依赖这里
async function loadProducts() {
  try {
    const d = await api.comboTrees({ structure: 1 })
    productOptions.value = d.products || []
  } catch (e) {
    // 下拉只是辅助，拉不到不该挡住台账
  }
}


async function openDetail(row) {
  detailVisible.value = true
  detailLoading.value = true
  detail.value = { ...row, steps: [] }
  try {
    const d = await api.comboDetail(row.product, row.part)
    detail.value = d
  } finally {
    detailLoading.value = false
  }
}

async function openRelease(row) {
  releaseTarget.value = { ...row }
  releaseNote.value = ''
  releaseVisible.value = true
  try {
    const d = await api.comboDetail(row.product, row.part)
    releaseTarget.value = { ...releaseTarget.value, ...d.combo, ...d, issues: d.issues || [] }
  } catch (e) {
    ElMessage.error('读取发布前检查失败：' + (e.detailText || e.message))
  }
}

async function submitRelease() {
  const t = releaseTarget.value
  if (!t) return
  releasing.value = true
  try {
    const r = await api.comboRelease({ product: t.product, part: t.part, note: releaseNote.value })
    ElMessage.success(`已发布 v${r.version}${r.warnings?.length ? '（有 ' + r.warnings.length + ' 条提示）' : ''}`)
    releaseVisible.value = false
    await load()
  } catch (e) {
    ElMessage.error(e.detailText || e.message || '发布失败')
  } finally {
    releasing.value = false
  }
}

async function doWithdraw(row) {
  let reason = ''
  try {
    const res = await ElMessageBox.prompt(
      `撤回后「${row.part}」的工艺立刻从任务挂线、核对校验、工艺卡里消失。请写明原因。`,
      '撤回发布',
      {
        inputPlaceholder: '如：定额有误，重新核定',
        inputValidator: (v) => (v && v.trim() ? true : '原因必填'),
        type: 'warning',
        confirmButtonText: '确认撤回'
      }
    )
    reason = res.value
  } catch (e) {
    return
  }
  try {
    await api.comboWithdraw({ product: row.product, part: row.part, reason })
    ElMessage.success('已撤回为草稿')
  } catch (e) {
    // 被在制任务引用时后端会列单号并拒绝，这里二次确认后强制撤回
    const msg = e.detailText || e.message || ''
    if (!msg.includes('在制任务')) {
      ElMessage.error(msg || '撤回失败')
      return
    }
    try {
      await ElMessageBox.confirm(msg + '\n\n仍然要撤回吗？这些任务会失去核对基准。', '有在制任务引用', {
        type: 'warning',
        confirmButtonText: '仍然撤回'
      })
    } catch (e2) {
      return
    }
    await api.comboWithdraw({ product: row.product, part: row.part, reason, force: true })
    ElMessage.success('已强制撤回')
  }
  await load()
}

async function openBatch() {
  batchVisible.value = true
  batchProduct.value = ''
  await previewBatch()
}

async function previewBatch() {
  batchLoading.value = true
  try {
    batchPre.value = await api.comboBatchRelease({
      dry_run: true,
      product: batchProduct.value,
      limit: 500
    })
  } finally {
    batchLoading.value = false
  }
}

async function doBatchRelease() {
  batchRunning.value = true
  try {
    const r = await api.comboBatchRelease({
      dry_run: false,
      product: batchProduct.value,
      limit: 500
    })
    ElMessage.success(`已发布 ${r.released.length} 条，跳过 ${r.skipped.length} 条`)
    batchVisible.value = false
    await load()
  } catch (e) {
    ElMessage.error(e.detailText || e.message || '批量发布失败')
  } finally {
    batchRunning.value = false
  }
}

function addStepRow() {
  if (!detail.value.steps) detail.value.steps = []
  detail.value.steps.push({ id: 0, process: '', unit_hours: 0, samples: 0 })
}

async function saveStep(row) {
  if (!row.id) {
    if (!row.process) {
      ElMessage.warning('先填工序名')
      return
    }
    await api.comboAddStep({
      product: detail.value.product,
      part: detail.value.part,
      process: row.process,
      unit_hours: row.unit_hours
    })
    ElMessage.success('已新增工序')
  } else {
    await api.comboUpdateStep(row.id, { process: row.process, unit_hours: row.unit_hours, samples: row.samples })
    ElMessage.success('已保存')
  }
  await refreshDetail()
  await load()
}

async function removeStep(row) {
  await ElMessageBox.confirm(
    row.id ? `删除工序「${row.process}」的工时记录？` : '移除这行未保存的工序？',
    '删除工序',
    { type: 'warning' }
  )
  if (row.id) {
    await api.comboDeleteStep(row.id)
    await refreshDetail()
    await load()
  } else {
    detail.value.steps = detail.value.steps.filter((s) => s !== row)
  }
  ElMessage.success('已删除')
}

async function refreshDetail() {
  const d = await api.comboDetail(detail.value.product, detail.value.part)
  detail.value = d
}

function openRename(row) {
  renameForm.product = row.product
  renameForm.part = row.part
  renameForm.new_product = row.product
  renameForm.new_part = row.part
  renameForm.state = row.status || row.state
  renameVisible.value = true
}

async function submitRename() {
  renaming.value = true
  try {
    const res = await api.comboRename({
      product: renameForm.product,
      part: renameForm.part,
      new_product: renameForm.new_product,
      new_part: renameForm.new_part
    })
    ElMessage.success(`已改名：工时 ${res.changed} 条，路线 ${res.routes} 条`)
    renameVisible.value = false
    await load()
    if (detailVisible.value) await refreshDetail()
  } finally {
    renaming.value = false
  }
}

async function toggleActive(row) {
  const next = !row.active
  await ElMessageBox.confirm(
    next
      ? '启用后这条组合回到它停用前的状态：路线还在发布态就是「已发布」，否则是「草稿」。'
      : '停用后组合状态变为「已归档」：不再参与任务挂线、核对校验与工艺卡打印，也不会出现在默认视图里。已建的路线不会被动，重新启用即可恢复。',
    next ? '启用组合' : '停用组合',
    { type: 'warning' }
  )
  const res = await api.comboSetActive({ product: row.product, part: row.part, active: next })
  const st = stateText(res?.status)
  ElMessage.success(next ? `已启用，当前状态：${st}` : '已停用，状态：已归档')
  await load()
  if (detailVisible.value) await refreshDetail()
}

function fmtQty(v) {
  const n = Number(v)
  if (!Number.isFinite(n)) return '1'
  if (Number.isInteger(n)) return String(n)
  return n.toFixed(2).replace(/0+$/, '').replace(/\.$/, '')
}

// ---- 产品结构：独立弹窗
//
// 「结构」不是台账的一行属性，它有自己的窗口。为什么不在这页就地展开：
// 台账是「哪些件有工艺、能不能发布」，结构是「谁装在谁下面」，
// 两件事在一屏里会让人分不清自己在改工艺还是在改装配关系。
const structVisible = ref(false)
const structProduct = ref('')
const structPart = ref('')

function openStructure(row) {
  structProduct.value = row?.product || ''
  structPart.value = row?.part || ''
  structVisible.value = true
}

async function onStructureChanged() {
  // 结构动过，台账里的「层级 · 装在」列要跟着变
  await load()
  loadProducts()
}

function goBuild(row) {
  if (!row.product) {
    ElMessage.warning('产品名为空，无法编排；请先改名补上产品')
    return
  }
  router.push({ path: '/routes', query: { product: row.product, part: row.part } })
}

function openCreate() {
  createForm.product = ''
  createForm.part = ''
  createForm.steps = [{ process: '', unit_hours: 0 }]
  createVisible.value = true
}

async function submitCreate() {
  if (!createForm.part.trim()) {
    ElMessage.warning('件名必填')
    return
  }
  creating.value = true
  try {
    const res = await api.comboCreate({
      product: createForm.product,
      part: createForm.part,
      steps: createForm.steps.filter((s) => s.process)
    })
    ElMessage.success(`已创建组合，含 ${res.steps} 道工序（草稿）`)
    createVisible.value = false
    await reload()
  } finally {
    creating.value = false
  }
}

onMounted(() => {
  // 地址栏里带条件就先按它来：分享出去的链接打开就是同一份清单
  if (route.query.q) kw.value = String(route.query.q)
  if (route.query.state) state.value = String(route.query.state)
  onlyIssue.value = String(route.query.issue ?? '') === '1'
  onlyActive.value = String(route.query.active ?? '') === '1'
  if (route.query.page) page.value = Math.max(1, Number(route.query.page) || 1)
  // 从工艺卡/任务跳过来时可以直接落到某一件上：?structure=产品&part=件名
  if (route.query.structure) {
    openStructure({ product: String(route.query.structure), part: String(route.query.part || '') })
  }
  load()
  loadProducts()
})
</script>

<style scoped>
.summary-bar {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 7px 14px;
  background: #fff;
  border: 1px solid #e6eaf2;
  border-radius: 8px;
  margin-bottom: 12px;
  font-size: 12.5px;
}
.sb-item { display: inline-flex; align-items: baseline; gap: 5px; }
.sb-item b { font-size: 14px; color: #1f2733; }
.sb-item b.ok { color: #1f9d63; }
.sb-item.warn b { color: #d9534f; }
.sb-sep { width: 1px; height: 14px; background: #e6eaf2; }
.sb-note { font-size: 12px; }

.tools { margin-bottom: 10px; }
.detail-meta { margin-bottom: 10px; }
.detail-tip { margin-bottom: 10px; }

.lv { font-size: 11.5px; padding: 1px 5px; border-radius: 4px; background: #eef2f8; color: #5a6474; }
.lv0 { background: #e8f0ff; color: #2f6fd0; font-weight: 600; }
.lv1 { background: #f0f4fa; color: #4c5666; }
.lv2 { background: #f6f7f9; color: #78818f; }
.lv-manual { margin-left: 3px; color: #b08a2e; vertical-align: -2px; }

/* 清单里的「层级 · 装在」：整格可点，鼠标移上去给下划线，暗示这里能改 */
.lv-cell { display: inline-flex; align-items: center; gap: 6px; max-width: 100%; cursor: pointer; }
.lv-parent { font-size: 12px; color: #4a5361; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.lv-at { font-size: 11.5px; color: #9aa3b0; }
.lv-parent.none { color: #a2aab7; }
.lv-cell:hover .lv-parent { color: #2f6fd0; text-decoration: underline dotted; }

/* 装配结构：快捷键提示、当前行、行内箭头 */
.kbd {
  display: inline-block;
  min-width: 16px;
  padding: 0 4px;
  margin: 0 1px;
  border: 1px solid #d7dee9;
  border-bottom-width: 2px;
  border-radius: 4px;
  background: #fafbfd;
  font-size: 11px;
  font-family: ui-monospace, monospace;
  color: #4a5568;
}
.tree-focus {
  display: flex;
  align-items: baseline;
  gap: 6px;
  font-size: 12px;
  padding: 5px 10px;
  margin-bottom: 8px;
  background: #f2f6ff;
  border: 1px solid #e2ebfb;
  border-radius: 6px;
}
.tree-focus b { color: #215fbf; font-size: 12.5px; }
/* 保存回执：方向键没有保存按钮，这一行就是「已经进库了」的唯一凭据 */
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
.sv-tip { max-width: 320px; overflow: hidden; text-overflow: ellipsis; color: #3d6b52; }
.sv-undo { padding: 0 2px !important; height: auto !important; font-size: 11.5px !important; }
.tip-key { color: #215fbf; }
.tn-focus { background: #eef4ff; border-radius: 5px; box-shadow: inset 0 0 0 1px #b9d1f7; }
.tn-keys { display: inline-flex; gap: 0; opacity: 0; transition: opacity 0.12s; }
.tn-keys :deep(.el-button) { padding: 0 5px; margin: 0; height: 20px; font-size: 13px; color: #5a6474; }
.tn-keys :deep(.el-button:hover) { color: #2f6fd0; background: #e6eefc; }
.tn:hover .tn-keys,
.tn-focus .tn-keys { opacity: 1; }
.tn-qty {
  font-size: 11.5px;
  color: #8d96a5;
  padding: 0 4px;
  border-radius: 4px;
  cursor: pointer;
}
.tn-qty.many { color: #b06a1e; background: #fdf4e6; }
.tn-qty:hover { background: #eef2f8; color: #2f6fd0; }
.tn-virtual { font-style: italic; }

/* 一键归位的预演清单 */
.an-body { font-size: 13px; min-height: 120px; }
.an-tip { font-size: 12px; line-height: 1.7; margin-bottom: 8px; }
.an-sum { font-size: 12.5px; margin-bottom: 6px; }
.an-sum b { color: #215fbf; }
.an-list { border: 1px solid #eef1f6; border-radius: 6px; max-height: 320px; overflow: auto; }
.an-row {
  display: grid;
  grid-template-columns: 30px 1.3fr 1.4fr 1.2fr 1.1fr;
  gap: 8px;
  padding: 3px 10px;
  font-size: 12.5px;
  border-bottom: 1px solid #f4f6fa;
  align-items: center;
}
.an-row :deep(.el-checkbox) { height: 20px; margin-right: 0; }
.an-row:last-child { border-bottom: 0; }
.an-head { background: #f7f9fc; font-size: 12px; color: #5a6474; position: sticky; top: 0; }
.an-row .ok { color: #215fbf; font-weight: 600; }
.ell { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.tree-panel { display: flex; flex-direction: column; height: calc(100vh - 402px); }
.tree-wrap { flex: 1 1 auto; min-height: 0; overflow: auto; }
.tree-tip { font-size: 12px; padding: 0 2px 8px; }
.combo-tree { font-size: 12.5px; }
.combo-tree :deep(.el-tree-node__content) { height: 30px; }
.tn { display: flex; align-items: center; gap: 6px; width: 100%; padding-right: 8px; }
.tn-product { font-weight: 600; color: #2b3340; }
.tn-name { max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tn-meta { font-size: 11.5px; color: #8d96a5; }

.rel-body { font-size: 13px; }
.rel-title { font-size: 15px; margin-bottom: 2px; }
.rel-meta { font-size: 12px; margin-bottom: 10px; }
.rel-alert { margin-bottom: 8px; }

.batch-cols { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.batch-col { min-width: 0; }
.batch-head { font-size: 12.5px; font-weight: 600; margin-bottom: 6px; color: #48505e; }
.batch-list {
  max-height: 280px;
  overflow: auto;
  border: 1px solid #eef1f6;
  border-radius: 6px;
  padding: 6px 8px;
  font-size: 12px;
}
.batch-item { padding: 2px 0; line-height: 1.5; }
.batch-item.skip { color: #8d96a5; }
.batch-item .muted { font-size: 11.5px; }

.create-steps { display: flex; flex-direction: column; gap: 8px; width: 100%; }
.create-step { display: flex; align-items: center; gap: 8px; }

@media (max-width: 1100px) {
  .batch-cols { grid-template-columns: 1fr; }
}
</style>
