<template>
  <div class="page">
    <div class="page-head">
      <span class="page-title">核对工作台</span>
      <span class="page-sub" v-if="sheet">单据 #{{ sheet.id }} · {{ sheet.form_type }} · 识别于 {{ fmtTime(sheet.created_at) }}</span>
      <div class="spacer"></div>
      <el-button size="small" :disabled="!sheet" @click="printSheet">
        <el-icon><Printer /></el-icon>打印
      </el-button>
      <el-button size="small" :disabled="!prevId" @click="go(prevId)"><el-icon><ArrowLeft /></el-icon>上一张</el-button>
      <el-button size="small" :disabled="!nextId" @click="go(nextId)">下一张<el-icon><ArrowRight /></el-icon></el-button>
      <el-button size="small" @click="$router.push('/import')">返回导入</el-button>
    </div>

    <div v-if="!sheet" class="card">
      <el-empty description="没有待核对的单据" :image-size="80">
        <el-button type="primary" @click="$router.push('/import')">去导入工时单</el-button>
      </el-empty>
    </div>

    <template v-else>
      <div class="alert-bar" :class="alertClass">
        <el-icon><WarningFilled v-if="blockCount" /><InfoFilled v-else /></el-icon>
        <span v-if="blockCount">存在 {{ blockCount }} 项阻断级问题，需修改后才能确认</span>
        <span v-else-if="warnCount">存在 {{ warnCount }} 项提示，请确认无误后保存</span>
        <span v-else>未发现异常，可直接确认</span>
        <span class="muted">·</span>
        <span>版面 {{ sheet.page_w }}×{{ sheet.page_h }} · 明细 {{ items.length }} 行</span>
        <div class="spacer"></div>
        <el-tag v-if="sheet.status === 'verified'" type="success" size="small">已确认 {{ sheet.verify_user }}</el-tag>
        <el-tag v-else-if="sheet.status === 'rejected'" type="danger" size="small">已作废</el-tag>
        <el-tag v-else type="warning" size="small">待核对</el-tag>
        <span class="muted mono" style="margin-left: 8px">本单工时 {{ totalHours.toFixed(2) }} h</span>
      </div>

      <!-- 归属订单：自动归集可能归错，归属与改挂入口放在一起，看见就能改 -->
      <div class="task-own-bar">
        <span class="muted">归属订单</span>
        <template v-if="sheet.task">
          <b class="mono">{{ sheet.task.task_no }}</b>
          <span v-if="sheet.task.title" class="own-title">{{ sheet.task.title }}</span>
          <el-tag v-if="sheet.task.status === 'closed'" size="small" type="info">已结案</el-tag>
          <el-tag v-else-if="sheet.task.status === 'cancelled'" size="small" type="info">已取消</el-tag>
        </template>
        <span v-else class="own-none">未归集 · 确认时按「产品+批次+期间」自动归集</span>
        <div class="spacer"></div>
        <el-button size="small" text type="primary" :disabled="periodLocked" @click="openMove">
          {{ sheet.task ? '改挂订单' : '归集到订单' }}
        </el-button>
      </div>

      <!-- 归属订单的工艺基准升过版、还没人认：这张单按哪一版核的说不清，
           复核时就要看见，不能等确认完再去任务页发现 -->
      <div v-if="sheet.task && (sheet.task.notices || []).length" class="task-notice-bar">
        <el-icon><WarningFilled /></el-icon>
        <b>归属订单 {{ sheet.task.task_no }} 的工艺基线已升版</b>
        <span>{{ sheet.task.notices.length }} 条引用待确认 · {{ sheet.task.notices[0].message }}</span>
        <div class="spacer"></div>
        <el-button
          size="small"
          text
          type="primary"
          @click="$router.push({ path: '/tasks', query: { task: sheet.task.id } })"
        >
          去确认
        </el-button>
      </div>

      <div v-if="flagList.length" class="flag-panel" :class="{ danger: blockCount }">
        <div class="flag-panel-head">
          <span>问题清单 · 共 {{ flagList.length }} 项</span>
          <span v-if="blockCount" class="flag-badge danger">{{ blockCount }} 项阻断</span>
          <span v-if="warnCount" class="flag-badge warn">{{ warnCount }} 项提示</span>
          <div class="spacer"></div>
          <el-button v-if="flagList.length > 6" size="small" text @click="showFlags = !showFlags">
            {{ showFlags ? '收起' : `展开全部 ${flagList.length} 项` }}
          </el-button>
        </div>
        <div class="flag-list">
          <div
            v-for="(f, i) in visibleFlags"
            :key="i"
            class="flag-item"
            :class="f.level"
            @click="gotoFlag(f)"
          >
            <span class="flag-row">第 {{ f.seq }} 行</span>
            <span class="flag-tag" :class="f.level">{{ f.label }}</span>
            <span class="flag-hint">{{ f.hint }}</span>
            <span v-if="f.fieldText" class="flag-field">→ {{ f.fieldText }}</span>
            <div class="spacer"></div>
            <span class="flag-goto">定位</span>
          </div>
        </div>
      </div>

      <div class="review-wrap">
        <ImageCanvas
          :src="sheet.image?.processed_url"
          :mask-src="sheet.image?.mask_url"
          :page-w="sheet.page_w"
          :page-h="sheet.page_h"
          :boxes="activeBoxes"
          :active-key="activeKey"
          :labels="fieldLabels"
          @pick="activeKey = $event"
        />

        <div>
          <div class="card" style="margin-bottom: 14px">
            <div class="row-flex" style="margin-bottom: 10px">
              <span class="section-title" style="margin: 0">单据表头</span>
              <div class="spacer"></div>
              <el-button size="small" text @click="applyHeaderToItems">表头套用到空行</el-button>
            </div>
            <div class="form-grid">
              <label>产品名称</label>
              <el-input v-model="header.product_name" size="small" placeholder="如 YUJ-18BZ制丸机" />
              <label>产品代号</label>
              <el-input v-model="header.product_code" size="small" placeholder="如 GHL-2-18.5" />
              <label>产品批号</label>
              <el-input v-model="header.batch_no" size="small" placeholder="如 河北凯诺" />
              <label>件名</label>
              <el-input v-model="header.part_name" size="small" placeholder="如 料包" />
              <label>材料</label>
              <el-input v-model="header.material" size="small" placeholder="如 304" />
              <label>加工件数</label>
              <el-input v-model="header.part_count" size="small" />
              <label>计调员</label>
              <el-input v-model="header.dispatcher" size="small" />
              <label>材料员</label>
              <el-input v-model="header.picker" size="small" />
              <label>红笔圈注</label>
              <el-input v-model="header.worker_hint" size="small" placeholder="右上角红笔姓名" />
              <label>备注</label>
              <el-input v-model="sheet.note" size="small" />
            </div>
          </div>

          <div class="card">
            <div v-if="reocring" class="reocr-bar">
              <el-icon class="spin"><Loading /></el-icon>
              <span>正在重新识别…{{ reocrElapsed }}s</span>
              <span class="muted">识别完成后本页会自动刷新，不用刷新浏览器</span>
            </div>
            <div class="row-flex" style="margin-bottom: 8px">
              <span class="section-title" style="margin: 0">工时明细（{{ items.length }} 行）</span>
              <span class="muted" style="font-size: 12px">名称 / 批次 / 件名 默认继承表头</span>
              <div class="spacer"></div>
              <el-checkbox v-model="showAllCols" size="small" label="显示重复列" />
              <el-button size="small" @click="addRow"><el-icon><Plus /></el-icon>新增行</el-button>
              <el-button size="small" :loading="saving" :disabled="readonly" @click="saveManual">保存</el-button>
            </div>

            <div class="items-table" :class="{ wide: showAllCols }">
              <div class="items-grid items-head">
                <span></span>
                <span v-if="showAllCols">产品名称</span>
                <span v-if="showAllCols">批次</span>
                <span v-if="showAllCols">件名</span>
                <span>工序</span>
                <span class="num">合格件数</span>
                <span class="num">单件工时</span>
                <span>完成日期</span>
                <span>工作者</span>
                <span>检验员</span>
                <span>备注</span>
                <span class="num">工时</span>
                <span class="num">操作</span>
              </div>

              <div
                v-for="(it, idx) in items"
                :key="it.id || 'new' + idx"
                class="items-grid items-row"
                :class="{ active: activeIdx === idx }"
                :data-idx="idx"
                @click="activeIdx = idx"
              >
                <span class="row-no">
                  {{ idx + 1 }}
                  <el-tooltip v-if="it.flags?.length" :content="flagText(it)" placement="top">
                    <i class="flag-dot" :class="hasBlock(it) ? 'block' : 'warn'"></i>
                  </el-tooltip>
                </span>

                <span v-if="showAllCols"><el-input v-model="it.product_name" size="small" :disabled="readonly" /></span>
                <span v-if="showAllCols"><el-input v-model="it.batch_no" size="small" :disabled="readonly" /></span>
                <span v-if="showAllCols"><el-input v-model="it.part_name" size="small" :disabled="readonly" /></span>

                <span>
                  <el-select v-model="it.process" size="small" filterable allow-create :disabled="readonly" placeholder="工序">
                    <el-option v-for="p in store.dicts.processes" :key="p.code" :value="p.name" :label="p.name" />
                    <!-- 字典别名（「包板折弯」挂在「折弯」下）不在选项列表里：
                         补一项当前值，否则 el-select 找不到标签会显示空白 -->
                    <el-option
                      v-if="processOrphan(it)"
                      :key="`proc-orphan-${it.process}`"
                      :value="it.process"
                      :label="it.process"
                    />
                  </el-select>
                  <div v-if="processHintText(it)" class="process-raw" :title="processHintTitle(it)">
                    {{ processHintText(it) }}
                  </div>
                </span>
                <span>
                  <el-input-number v-model="it.qualified_qty" size="small" :min="0" :controls="false" :disabled="readonly" />
                </span>
                <span>
                  <el-input-number v-model="it.unit_hours" size="small" :min="0" :step="0.25" :precision="2" :controls="false" :disabled="readonly" />
                  <!-- 小数工时与单据上的「4:10:00」是同一个数，不写出来总被当成读错 -->
                  <div v-if="hms(it.unit_hours)" class="hours-hms" :title="`${it.unit_hours} 小时 = ${hms(it.unit_hours)}`">
                    {{ hms(it.unit_hours) }}
                  </div>
                </span>
                <span class="date-cell">
                  <el-date-picker v-model="it.finish_date" type="date" size="small" value-format="YYYY-MM-DD" :disabled="readonly" />
                  <div v-if="dateRawHint(it)" class="date-hint">
                    <el-image
                      v-if="dateCropUrl(it)"
                      class="sign-crop date-crop"
                      :src="dateCropUrl(it)"
                      :preview-src-list="[dateCropUrl(it)]"
                      :preview-teleported="true"
                      fit="contain"
                      title="放大看日期原图"
                      @click.stop
                    />
                    <span class="process-raw" :title="`日期识别原文：${it.finish_date_raw}`">
                      原文 {{ it.finish_date_raw }}
                    </span>
                  </div>
                </span>
                <span class="worker-cell">
                  <el-select v-model="it.worker_id" size="small" filterable clearable :disabled="readonly" placeholder="人员">
                    <el-option v-for="w in workerOptions" :key="w.value" :value="w.value" :label="w.label" />
                    <el-option
                      v-if="orphanWorkerOption(it)"
                      :key="`orphan-${it.worker_id}`"
                      :value="it.worker_id"
                      :label="orphanWorkerOption(it).label"
                    />
                  </el-select>
                  <!-- 操作者也是手写签名，模型常常一个字都读不出来（单据 6 的「白小峰」读成 2）。
                       没认出人时给一格能点开放大的签名；放在下拉下面而不是并排——
                       工作者列只有 80px，并排会把名字挤掉 -->
                  <div v-if="workerCropUrl(it)" class="date-hint">
                    <el-image
                      class="sign-crop date-crop"
                      :src="workerCropUrl(it)"
                      :preview-src-list="[workerCropUrl(it)]"
                      :preview-teleported="true"
                      fit="contain"
                      title="放大看操作者签名"
                      @click.stop
                    />
                    <span class="process-raw">看签名</span>
                  </div>
                </span>
                <span class="inspector-cell">
                  <div class="sign-row">
                    <el-select
                      v-model="it.inspector"
                      size="small"
                      filterable
                      allow-create
                      clearable
                      :disabled="readonly"
                      placeholder="检验员"
                    >
                      <el-option v-for="w in inspectorOptions" :key="w.value" :value="w.value" :label="w.label" />
                    </el-select>
                    <el-image
                      v-if="cropUrl(it, 'inspector')"
                      class="sign-crop"
                      :src="cropUrl(it, 'inspector')"
                      :preview-src-list="[cropUrl(it, 'inspector')]"
                      :preview-teleported="true"
                      fit="contain"
                      title="放大看签名原图"
                      @click.stop
                    />
                  </div>
                  <div v-if="signRawHint(it)" class="process-raw" :title="`签名识别原文：${it.inspector_raw}`">
                    原文 {{ it.inspector_raw }}
                  </div>
                </span>
                <span><el-input v-model="it.remark" size="small" :disabled="readonly" /></span>

                <span class="num hours-cell">
                  {{ computeTotal(it).toFixed(2) }}
                  <!-- 8.33 就是单子上的 8:20:00，两行都写出来就不用再心算 -->
                  <div v-if="hms(computeTotal(it))" class="hours-hms">{{ hms(computeTotal(it)) }}</div>
                </span>
                <span class="num row-ops">
                  <el-tooltip content="在原图定位" placement="top">
                    <el-button size="small" text type="primary" @click.stop="locate(idx)"><el-icon><Aim /></el-icon></el-button>
                  </el-tooltip>
                  <el-tooltip content="删除本行" placement="top">
                    <el-button size="small" text type="danger" :disabled="readonly" @click.stop="removeRow(idx)"><el-icon><Delete /></el-icon></el-button>
                  </el-tooltip>
                </span>
              </div>
            </div>

            <div v-if="!items.length" style="padding: 12px 0">
              <el-empty description="未识别出明细行，可手工新增" :image-size="70" />
            </div>

            <div class="actions">
              <el-button type="primary" :loading="verifying" :disabled="readonly" @click="verify(false)">
                保存并确认（Ctrl+Enter）
              </el-button>
              <el-button :disabled="readonly" @click="verify(true)" v-if="blockCount">强制确认</el-button>
              <el-button @click="showAudit = true">修改留痕</el-button>
              <el-button v-if="sheet.status === 'verified' && lackDims.length" type="warning" plain @click="openDims">
                补录维度（{{ lackDims.length }} 行）
              </el-button>
              <div class="spacer"></div>
              <el-button v-if="sheet.status !== 'verified' && sheet.status !== 'rejected'" type="danger" plain @click="reject">
                作废
              </el-button>
              <el-button v-if="sheet.status === 'rejected'" type="warning" plain @click="restore">恢复</el-button>
              <el-button v-if="sheet.status !== 'verified'" type="danger" @click="removeSheet">删除</el-button>
              <el-tooltip
                :disabled="sheet.status !== 'verified'"
                content="已确认的单据要先撤销确认，才能重新识别"
                placement="top"
              >
                <span>
                  <el-button :loading="reocring" :disabled="sheet.status === 'verified' || reocring" @click="reocr">重新识别</el-button>
                </span>
              </el-tooltip>
              <el-button v-if="sheet.status === 'verified'" type="warning" plain @click="reopen">撤销确认</el-button>
            </div>
          </div>
        </div>
      </div>
    </template>

    <el-dialog v-model="dimsVisible" title="补录归集维度" width="min(720px, 94vw)">
      <p class="muted" style="margin-top: 0">
        只补「产品 / 件名 / 工序」。这三项不影响工时金额，只决定这笔工时能不能挂到成本与工艺上；
        补录会逐项写进修改留痕，确认人与确认时间保持不变。
      </p>
      <el-table :data="dimsRows" size="small" style="width: 100%">
        <el-table-column prop="seq" label="行" width="52" align="center" />
        <el-table-column label="缺什么" width="96">
          <template #default="{ row }"><el-tag size="small" type="warning">{{ row.lack }}</el-tag></template>
        </el-table-column>
        <el-table-column label="产品">
          <template #default="{ row }"><el-input v-model="row.product_name" size="small" placeholder="留空表示不改" /></template>
        </el-table-column>
        <el-table-column label="件名">
          <template #default="{ row }"><el-input v-model="row.part_name" size="small" placeholder="留空表示不改" /></template>
        </el-table-column>
        <el-table-column label="工序">
          <template #default="{ row }"><el-input v-model="row.process" size="small" placeholder="留空表示不改" /></template>
        </el-table-column>
      </el-table>
      <el-form label-width="72px" style="margin-top: 12px">
        <el-form-item label="补录依据">
          <el-input v-model="dimsReason" size="small" placeholder="如：按原图补录（产品代号 YUJ-18BZ）" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dimsVisible = false">取消</el-button>
        <el-button type="primary" @click="submitDims">保存补录</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="showReocr" title="重新识别" width="min(560px, 94vw)">
      <div class="reocr-body">
        <p class="reocr-lead">会按原图重跑一遍识别，明细行按新结果重建。</p>
        <div v-if="keeps.total_fields" class="reocr-keeps">
          <el-checkbox v-model="keepManual" label="保留下列已保存的人工修改" />
          <ul>
            <li v-for="r in keeps.rows" :key="'r' + r.seq">
              <span class="keep-row">第 {{ r.seq }} 行</span>
              <span v-for="f in r.fields" :key="f.key" class="keep-item">{{ f.label }} {{ f.value }}</span>
            </li>
            <li v-if="keeps.header.length">
              <span class="keep-row">表头</span>
              <span v-for="f in keeps.header" :key="f.key" class="keep-item">{{ f.label }} {{ f.value }}</span>
            </li>
          </ul>
          <p class="reocr-note">取消勾选则这些字段改用本次识别结果。</p>
        </div>
        <p v-else class="reocr-note">本单没有已保存的人工修改，全部按识别结果。</p>
        <div class="reocr-warn">
          <el-icon><WarningFilled /></el-icon>
          <span>表格里尚未保存的改动会丢弃。</span>
        </div>
      </div>
      <template #footer>
        <el-button @click="showReocr = false">取消</el-button>
        <el-button type="primary" :loading="reocring" @click="doReocr">开始识别</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="showAudit" title="修改留痕" width="820px">
      <el-table :data="audit" size="small" height="420">
        <el-table-column prop="ts" label="时间" width="160" />
        <el-table-column label="字段" width="170">
          <template #default="{ row }">
            <span>{{ row.field_label || row.field }}</span>
            <span v-if="row.target" class="audit-target">{{ row.target }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="old" label="原值" show-overflow-tooltip>
          <template #default="{ row }">{{ row.old || '—' }}</template>
        </el-table-column>
        <el-table-column prop="new" label="新值" show-overflow-tooltip>
          <template #default="{ row }">{{ row.new || '—' }}</template>
        </el-table-column>
        <el-table-column prop="user" label="操作人" width="90" />
      </el-table>
    </el-dialog>

    <SheetTaskDialog
      v-model="moveVisible"
      :sheet-ids="sheet ? [sheet.id] : []"
      :current="sheet?.task ? { id: sheet.task.id, task_no: sheet.task.task_no } : null"
      :sheet-period="sheet?.ym || store.ym || ''"
      @done="onTaskMoved"
    />
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api'
import { periodLocked, refreshStats, store, workerOptions, orphanWorkerOption, inspectorOptions } from '../store'
import ImageCanvas from '../components/ImageCanvas.vue'
import SheetTaskDialog from '../components/SheetTaskDialog.vue'

const route = useRoute()
const router = useRouter()

const sheet = ref(null)
const header = ref({})
const items = ref([])
const activeIdx = ref(-1)
const activeKey = ref('')
const saving = ref(false)
const verifying = ref(false)
const reocring = ref(false)
const showReocr = ref(false)
const keepManual = ref(true)
const keeps = ref({ rows: [], header: [], total_rows: 0, total_fields: 0 })
const reocrElapsed = ref(0)
let reocrTimer = null
const showAudit = ref(false)
const audit = ref([])
const queue = ref([])
// 明细默认单行紧凑显示；产品名称/批次/件名 与表头重复，需要逐行不同时再展开
const showAllCols = ref(false)
// 问题清单超过 6 条时折叠
const showFlags = ref(false)
// 改挂订单：归集是自动的，归错了在这里就地改
const moveVisible = ref(false)

function openMove() {
  moveVisible.value = true
}

async function onTaskMoved() {
  // 归属一变，单据上的任务提醒、任务列表的实报工时都要重新取
  if (sheet.value) await loadSheet(sheet.value.id)
  await refreshStats()
}

const fieldLabels = {
  unit_hours: '单件工时',
  qualified_qty: '合格件数',
  process: '工序',
  worker_raw: '工作者',
  finish_date: '日期'
}

const readonly = computed(() => reocring.value || sheet.value?.status === 'verified' || periodLocked.value || sheet.value?.period_status === 'locked')

// 工序格有两种需要说明的情况：
// 1) 单据写的不是字典里的写法（「切刀焊」→ 归并成「焊」）：把原文摆出来；
// 2) 单据写的正是字典登记的别名（「包板折弯」挂在「折弯」下）：显示照单据写，
//    但要让人知道它归到哪一道工序，否则汇总里对不上。
function processHintText(it) {
  const raw = (it?.process_raw || '').trim()
  const cur = (it?.process || '').trim()
  if (raw && raw !== cur) return `原文 ${raw}`
  const std = (it?.process_std || '').trim()
  if (std && cur && std !== cur) return `归入字典 ${std}`
  return ''
}

function processHintTitle(it) {
  const raw = (it?.process_raw || '').trim()
  const std = (it?.process_std || '').trim()
  if (raw && raw !== (it?.process || '').trim()) {
    return `单据原文：${raw}${std ? `（归入字典 ${std}）` : ''}`
  }
  return std ? `该写法是工序字典「${std}」登记的别名，汇总按「${std}」统计` : ''
}

// 别名不在下拉选项里，补一项当前值，否则 el-select 找不到标签会显示空白
function processOrphan(it) {
  const name = (it?.process || '').trim()
  if (!name) return false
  return !(store.dicts.processes || []).some((p) => p.name === name)
}

// 检验员那一格是手写签名，模型认不准属于常态（「杜文杰」被读成「定生」）。
// 原文与当前值不一致时挂在下面，让人一眼知道「这是识别结果，不是人名」。
function signRawHint(it) {
  const raw = (it?.inspector_raw || '').trim()
  return raw && raw !== (it.inspector || '').trim()
}

// 签名格裁图：直接给个能点开放大的小图，比让人对着整张照片找那一格快得多
function cropUrl(it, field) {
  if (!it?.id || !sheet.value?.id) return ''
  if (!it.bbox || !it.bbox[field]) return ''
  return `/api/sheets/${sheet.value.id}/items/${it.id}/crop?field=${field}`
}

// 日期格识别原文：手写 9.10 里那个「1」只有 2px 高，识别网络一律看不见，
// 整页读出来是「9.0」——解析不出日期。把原文摆出来，人才知道该改哪一笔。
function dateRawHint(it) {
  const raw = (it?.finish_date_raw || '').trim()
  return raw && !it.finish_date
}

// 日期没解析出来时直接给一格裁图：点开就能看清是 9.10 还是 9.1，
// 比在整张照片上找那一小格快得多（与签名缩略图同一套裁图接口）
function dateCropUrl(it) {
  if (it?.finish_date) return ''
  return cropUrl(it, 'finish_date')
}

// 操作者签名格：没认出人时给一格裁图。手写签名 OCR 基本读不出（「白小峰」读成 2，
// 草书「杜文杰」读成「定生」），人眼一秒就认出来，认完从名册里选，别名机制再接管下一张
function workerCropUrl(it) {
  if (it?.worker_id) return ''
  return cropUrl(it, 'worker_raw')
}

// 小数工时 ↔ 单据上的 H:MM:SS。4.1667 与 4:10:00 是同一个数，
// 界面上只写 4.17 会被当成「识别错了」，所以两个都写出来（返回 '4:10:00'）
function hms(hours) {
  if (hours === null || hours === undefined || hours === '' || Number.isNaN(Number(hours))) return ''
  const v = Math.abs(Number(hours))
  if (v <= 0) return ''
  let hh = Math.floor(v)
  let mm = Math.round((v - hh) * 60)
  if (mm === 60) {
    hh += 1
    mm = 0
  }
  return `${Number(hours) < 0 ? '-' : ''}${hh}:${String(mm).padStart(2, '0')}:00`
}

const activeBoxes = computed(() => {
  const it = items.value[activeIdx.value]
  return it?.bbox || {}
})

const totalHours = computed(() => items.value.reduce((s, it) => s + computeTotal(it), 0))

const blockCount = computed(() =>
  items.value.reduce((n, it) => n + (it.flags || []).filter((f) => f.level === 'block').length, 0)
)

const warnCount = computed(() =>
  items.value.reduce((n, it) => n + (it.flags || []).filter((f) => f.level !== 'block').length, 0)
)

const alertClass = computed(() => (blockCount.value ? 'danger' : warnCount.value ? '' : 'ok'))

// 告警字段的中文名：让问题清单能指出「去改哪一格」
const FIELD_TEXT = {
  unit_hours: '单件工时',
  qualified_qty: '合格件数',
  finish_date: '完成日期',
  worker_raw: '工作者',
  worker: '工作者',
  process: '工序',
  declared_total: '手写合计'
}

const flagList = computed(() => {
  const out = []
  items.value.forEach((it, idx) => {
    for (const f of it.flags || []) {
      const fieldKey = (f.field || '').split(',')[0]
      out.push({
        ...f,
        idx,
        seq: idx + 1,
        fieldKey,
        fieldText: FIELD_TEXT[fieldKey] || ''
      })
    }
  })
  // 阻断项优先，同级保持行序
  return out.sort((a, b) => (a.level === 'block' ? 0 : 1) - (b.level === 'block' ? 0 : 1))
})

const visibleFlags = computed(() => (showFlags.value ? flagList.value : flagList.value.slice(0, 6)))

function gotoFlag(f) {
  activeIdx.value = f.idx
  // 告警里的 field 可能是逗号分隔（如 "worker,unit_hours"），
  // 取第一个能在本行 bbox 中找到的键，保证左侧原图能高亮到具体格子。
  const bbox = items.value[f.idx]?.bbox || {}
  const keys = (f.field || '').split(',').filter(Boolean)
  activeKey.value = keys.find((k) => k in bbox) || Object.keys(bbox)[0] || 'unit_hours'
  requestAnimationFrame(() => {
    const el = document.querySelector(`.items-row[data-idx="${f.idx}"]`)
    el?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
  })
}

const currentIdx = computed(() => queue.value.findIndex((q) => q.id === sheet.value?.id))
const prevId = computed(() => (currentIdx.value > 0 ? queue.value[currentIdx.value - 1].id : null))
const nextId = computed(() =>
  currentIdx.value >= 0 && currentIdx.value < queue.value.length - 1 ? queue.value[currentIdx.value + 1].id : null
)

function computeTotal(it) {
  const qty = Number(it.qualified_qty || 0)
  const unit = Number(it.unit_hours || 0)
  return Math.round(qty * unit * 100) / 100
}

function hasBlock(it) {
  return (it.flags || []).some((f) => f.level === 'block')
}

function flagText(it) {
  return (it.flags || []).map((f) => `${f.label}：${f.hint}`).join('\n')
}

function fmtTime(s) {
  return s ? s.replace('T', ' ').slice(0, 16) : ''
}

// 打印：新窗口打开后端渲染的 PDF（A4 横向，与车间空白单同一份版式）
function printSheet() {
  if (!sheet.value) return
  window.open(api.sheetPrintUrl(sheet.value.id), '_blank')
}

async function loadQueue() {
  try {
    queue.value = await api.workQueue(store.ym, 50)
  } catch {
    queue.value = []
  }
}

async function loadSheet(id) {
  const data = await api.sheet(id)
  sheet.value = data
  header.value = { ...(data.header || {}) }
  items.value = (data.items || []).map((it) => ({ ...it, finish_date: it.finish_date || null }))
  activeIdx.value = items.value.length ? 0 : -1
  activeKey.value = ''
}

async function go(id) {
  if (!id) return
  router.push(`/review/${id}`)
}

function locate(idx) {
  activeIdx.value = idx
  const it = items.value[idx]
  if (it?.flags?.length) activeKey.value = it.flags[0].field?.split(',')[0] || 'unit_hours'
  else activeKey.value = 'unit_hours'
}

function addRow() {
  items.value.push({
    product_name: header.value.product_name || '',
    batch_no: header.value.batch_no || '',
    part_name: '',
    process: '',
    qualified_qty: null,
    unit_hours: null,
    finish_date: null,
    worker_id: null,
    worker_name: '',
    worker_raw: '',
    inspector: '',
    remark: '',
    flags: [],
    bbox: {}
  })
}

function removeRow(idx) {
  items.value.splice(idx, 1)
  if (activeIdx.value >= items.value.length) activeIdx.value = items.value.length - 1
}

function applyHeaderToItems() {
  items.value.forEach((it) => {
    if (!it.product_name) it.product_name = header.value.product_name || ''
    if (!it.batch_no) it.batch_no = header.value.batch_no || ''
    if (!it.part_name && items.value.length === 1) it.part_name = header.value.part_name || ''
  })
  ElMessage.success('已套用')
}

function payload() {
  return {
    header: header.value,
    note: sheet.value.note || '',
    items: items.value.map((it, i) => ({
      ...(it.id ? { id: it.id } : {}),
      seq: i + 1,
      product_name: it.product_name || '',
      batch_no: it.batch_no || '',
      part_name: it.part_name || '',
      process: it.process || '',
      qualified_qty: it.qualified_qty ?? null,
      unit_hours: it.unit_hours ?? null,
      finish_date: it.finish_date || null,
      inspector: it.inspector || '',
      worker_id: it.worker_id ?? null,
      remark: it.remark || ''
    }))
  }
}

async function save(silent = false) {
  saving.value = true
  try {
    const data = await api.updateSheet(sheet.value.id, payload())
    sheet.value = data
    items.value = (data.items || []).map((it) => ({ ...it, finish_date: it.finish_date || null }))
    if (!silent) ElMessage.success('已保存')
  } finally {
    saving.value = false
  }
}

// 手点「保存」才提醒：保存并确认里的静默保存不重复弹窗
async function saveManual() {
  const notices = sheet.value?.task?.notices || []
  if (notices.length) {
    try {
      await ElMessageBox.confirm(
        `归属订单 ${sheet.value.task.task_no} 有 ${notices.length} 条工艺引用「基线已升版、尚未确认」：\n` +
          notices.slice(0, 4).map((n) => `· ${n.part || '—'}：${n.message}`).join('\n') +
          '\n\n本单的定额核对基准可能已变，建议先确认工艺再保存核对结果。仍要保存吗？',
        '工艺基线已升版',
        { type: 'warning', confirmButtonText: '仍然保存', cancelButtonText: '先去确认工艺' }
      )
    } catch {
      return
    }
  }
  await save()
}

async function verify(force) {
  // 提交确认前再提醒一次：这张单核的是「哪一版定额」，取决于归属订单有没有
  // 认过升版后的工艺。提醒不能只挂在页面顶部，确认这个动作必须被拦住问一句。
  const notices = sheet.value?.task?.notices || []
  if (notices.length) {
    try {
      await ElMessageBox.confirm(
        `归属订单 ${sheet.value.task.task_no} 有 ${notices.length} 条工艺引用「基线已升版、尚未确认」：\n` +
          notices.slice(0, 4).map((n) => `· ${n.part || '—'}：${n.message}`).join('\n') +
          '\n\n现在确认，本单将按「归属订单当前引用的版本」核对。是否继续确认？',
        '工艺基线已升版',
        { type: 'warning', confirmButtonText: '仍然确认', cancelButtonText: '先去确认工艺' }
      )
    } catch {
      return
    }
  }
  verifying.value = true
  const nextTarget = nextId.value // 必须在刷新队列前取，否则当前单已移出队列
  try {
    await save(true)
    // 跨期单据：后端默认拒（409），必须人点头才入账。
    // 一张 7 月干完的单混进 9 月工资表，两个月的数同时错，事后查不出来。
    let confirmedCross = false
    let r = null
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        r = await api.verifySheet(sheet.value.id, { force, confirmOutOfPeriod: confirmedCross })
        break
      } catch (e) {
        if (e.detailCode === 'OUT_OF_PERIOD' && !confirmedCross) {
          const rows = e.response?.data?.detail?.items || []
          // 后端那句「确认后会计入本期账」不再重复拼一遍，这里只把行摊开讲清楚
          await ElMessageBox.confirm(
            `本单有 ${rows.length} 行完工日期不在本期（${store.ym}）：\n\n` +
              rows.slice(0, 6).map((x) => `· 第 ${x.seq} 行：${x.date}　${x.worker || ''}　${x.hours}h`).join('\n') +
              (rows.length > 6 ? `\n… 另有 ${rows.length - 6} 行` : '') +
              `\n\n确认后，这些工时会计入 ${store.ym} 的账；日期填错了就先「先去改日期」，别把跨期工时混进本月。`,
            '完工日期不在本期',
            { type: 'warning', confirmButtonText: '确认计入本期', cancelButtonText: '先去改日期' }
          )
          confirmedCross = true
          continue
        }
        throw e
      }
    }
    if (!r) return
    ElMessage.success(`已确认，本单 ${r.total_hours.toFixed(2)} 小时`)
    // 后端算出的「确认成功但有问题」不能吞掉：件名没有已发布工艺、单据归属订单的
    // 工艺基准对不上，这些不会让确认失败，却会让这条实报失去定额对照。
    // 原先只回给接口、界面没接，等于白算。
    if (r.warnings && r.warnings.length) {
      await ElMessageBox.alert(
        `本单已确认，但有 ${r.warnings.length} 项需要跟进：\n\n` +
          r.warnings.map((w) => `· ${w}`).join('\n'),
        '已确认，附带走查项',
        { type: 'warning', confirmButtonText: '知道了' }
      ).catch(() => {})
    }
    await refreshStats()
    await loadQueue()
    if (nextTarget) {
      router.push(`/review/${nextTarget}`)
      return
    }
    const q = await api.workQueue(store.ym, 1)
    if (q.length) router.push(`/review/${q[0].id}`)
    else await loadSheet(sheet.value.id)
  } finally {
    verifying.value = false
  }
}

async function reject() {
  const { value } = await ElMessageBox.prompt('请输入作废原因', '作废单据', { inputPlaceholder: '如：照片拍糊、非本车间单据' })
  await api.rejectSheet(sheet.value.id, value || '')
  ElMessage.success('已作废')
  await refreshStats()
  await loadSheet(sheet.value.id)
}

const LABELS = { qualified_qty: '合格件数', unit_hours: '单件工时', process: '工序', worker_id: '工作者', finish_date: '完工日期' }
const QTY_KEYS = ['qualified_qty', 'unit_hours', 'process', 'worker_id', 'finish_date']

function briefValue(it, key) {
  if (key === 'worker_id') return it.worker_name || it.worker_raw || '空'
  if (key === 'process') return it.process || '空'
  const v = it[key]
  return v === null || v === undefined || v === '' ? '空' : String(v)
}

// 识别前后对比：把「这次被改掉的值」直接说出来，避免用户以为识别没执行
function diffItems(before, after) {
  const changes = []
  after.forEach((next, i) => {
    const prev = before[i]
    if (!prev) return
    QTY_KEYS.forEach((key) => {
      const a = briefValue(prev, key)
      const b = briefValue(next, key)
      if (a !== b) changes.push(`第${i + 1}行 ${LABELS[key] || key}：${a} → ${b}`)
    })
  })
  if (before.length !== after.length) changes.push(`明细行数：${before.length} → ${after.length}`)
  return changes
}

async function reocr() {
  if (reocring.value) return
  // 先把「哪些人工改过的字段会被保留」查出来，让人在弹窗里自己决定
  try {
    keeps.value = await api.sheetKeeps(sheet.value.id)
  } catch {
    keeps.value = { rows: [], header: [], total_rows: 0, total_fields: 0 }
  }
  keepManual.value = keeps.value.total_fields > 0
  showReocr.value = true
}

function startReocrTimer() {
  reocrElapsed.value = 0
  clearInterval(reocrTimer)
  reocrTimer = setInterval(() => (reocrElapsed.value += 1), 1000)
}

function stopReocrTimer() {
  clearInterval(reocrTimer)
  reocrTimer = null
}

async function doReocr() {
  if (reocring.value) return
  showReocr.value = false
  reocring.value = true
  startReocrTimer()
  const sheetId = sheet.value.id
  const imageId = sheet.value.image_id
  try {
    const r = await api.reocrSheet(sheetId, keepManual.value)
    // 立刻按数据库现状刷一次：没保存的改动就此丢弃，界面也不再停留在
    // 「手填的数字」上——之前正是这里没刷新，看上去像识别没生效。
    await loadSheet(sheetId)
    const before = items.value.map((it) => ({ ...it }))
    ElMessage.info(
      r.kept_fields
        ? `已开始识别，${r.kept_fields} 处人工修改会保留…`
        : '已开始识别…'
    )
    await waitRecognized({ sheetId, imageId, before, keptFields: keepManual.value ? r.kept_fields : 0 })
  } catch {
    /* 错误提示由请求拦截器统一弹出 */
  } finally {
    stopReocrTimer()
    reocring.value = false
  }
}

async function waitRecognized({ sheetId, imageId, before, keptFields = 0 }, timeoutMs = 180000) {
  const started = Date.now()
  // 必须确认「这一轮真的跑过」：直接看到 done 有可能是上一轮的残留状态
  let sawRunning = false
  while (Date.now() - started < timeoutMs) {
    await new Promise((r) => setTimeout(r, 1200))
    let img
    try {
      img = await api.image(imageId)
    } catch {
      continue
    }
    if (img?.status === 'pending' || img?.status === 'processing') {
      sawRunning = true
      continue
    }
    if (img?.status === 'failed') {
      ElMessage.error(`识别失败：${img.error || '未知原因'}`)
      await loadSheet(sheetId)
      return
    }
    if (img?.status !== 'done') continue

    if (!sawRunning) {
      // 一次都没见到 pending/processing：再确认一遍，别拿旧结果刷新界面
      await new Promise((r) => setTimeout(r, 1500))
      let again = null
      try {
        again = await api.image(imageId)
      } catch {
        /* 忽略，按完成处理 */
      }
      if (again?.status === 'pending' || again?.status === 'processing') {
        sawRunning = true
        continue
      }
      if (again?.status === 'failed') {
        ElMessage.error(`识别失败：${again.error || '未知原因'}`)
        await loadSheet(sheetId)
        return
      }
    }

    await loadSheet(sheetId)
    await loadQueue()
    const changes = diffItems(before, items.value)
    if (changes.length) {
      ElMessage({
        type: 'success',
        duration: 9000,
        message: `识别完成，共 ${changes.length} 处变化${keptFields ? `（${keptFields} 处人工修改按你的选择保留）` : ''}：${changes.slice(0, 3).join('；')}${changes.length > 3 ? ' …' : ''}`
      })
    } else if (keptFields) {
      ElMessage({
        type: 'success',
        duration: 8000,
        message: `识别完成：其余字段与识别前一致，${keptFields} 处人工修改按你的选择保留`
      })
    } else {
      ElMessage.success('识别完成，结果与之前一致')
    }
    return
  }
  ElMessage.warning('识别还在队列中，稍后会自动重试；也可点「上一张/下一张」或刷新页面查看')
}

async function reopen() {
  await api.reopenSheet(sheet.value.id)
  ElMessage.success('已撤销确认')
  await refreshStats()
  await loadSheet(sheet.value.id)
}

// 作废是业务判断，可能判断错；点错一次就让这张单永久既不入账也删不掉，
// 只能改库 —— 所以必须给一条回头的路。
async function restore() {
  await api.restoreSheet(sheet.value.id)
  ElMessage.success('已恢复为待核对')
  await refreshStats()
  await loadSheet(sheet.value.id)
  await loadQueue()
}

// 彻底删除：错拍、重复上传的单，光作废会一直占台账、占批次、占硬盘。
async function removeSheet() {
  let reason = ''
  try {
    const r = await ElMessageBox.prompt(
      '删除后不可恢复：单据、明细、原图一起删掉，只保留一条删除留痕。请填原因。',
      '彻底删除这张单据',
      { inputPlaceholder: '如：重复上传，与 #12 是同一张', type: 'warning', confirmButtonText: '删除', inputValidator: (v) => (v || '').trim().length > 0 || '请填原因' }
    )
    reason = r.value
  } catch {
    return
  }
  try {
    await ElMessageBox.confirm('确认删除？这一步不可撤销。', '再次确认', {
      type: 'error',
      confirmButtonText: '确认删除',
      cancelButtonText: '取消'
    })
  } catch {
    return
  }
  const r = await api.deleteSheet(sheet.value.id, reason)
  ElMessage.success(`已删除（含明细 ${r.snapshot_items} 行、文件 ${r.files_removed} 个）`)
  await refreshStats()
  router.push('/sheets')
}

// 维度补录：产品/件名/工序为空时，工时算得出来但归不到成本与工艺上。
// 已确认单据为了补一个件名去撤销确认，会抹掉复核人的确认时间戳，不值当。
const dimsVisible = ref(false)
const dimsReason = ref('')
const dimsRows = ref([])
const lackDims = computed(() =>
  (sheet.value?.items || []).filter(
    (it) => !(it.product_name || '').trim() || !(it.part_name || '').trim() || !((it.process || it.process_std || '').trim())
  )
)

function openDims() {
  dimsRows.value = lackDims.value.map((it) => ({
    id: it.id,
    seq: it.seq,
    product_name: it.product_name || '',
    part_name: it.part_name || '',
    process: it.process || it.process_std || '',
    lack: [
      !(it.product_name || '').trim() ? '产品' : '',
      !(it.part_name || '').trim() ? '件名' : '',
      !((it.process || it.process_std || '').trim()) ? '工序' : ''
    ].filter(Boolean).join('、')
  }))
  dimsReason.value = ''
  dimsVisible.value = true
}

async function submitDims() {
  if (!dimsReason.value.trim()) return ElMessage.warning('请填补录依据（如：按原图补录）')
  const items = dimsRows.value
    .filter((r) => (r.product_name || '').trim() || (r.part_name || '').trim() || (r.process || '').trim())
    .map((r) => ({ id: r.id, product_name: r.product_name, part_name: r.part_name, process: r.process }))
  if (!items.length) return ElMessage.warning('没有要补的内容')
  const r = await api.updateSheetDims(sheet.value.id, items, dimsReason.value)
  ElMessage.success(`已补录 ${r.changed.length} 项，工时金额未变`)
  if (r.warnings?.length) {
    ElMessageBox.alert(r.warnings.join('\n'), '补录完成，附带走查项', { type: 'warning' }).catch(() => {})
  }
  dimsVisible.value = false
  await loadSheet(sheet.value.id)
}

async function openAudit() {
  audit.value = await api.sheetAudit(sheet.value.id)
  showAudit.value = true
}

watch(showAudit, async (v) => {
  if (v && sheet.value) audit.value = await api.sheetAudit(sheet.value.id)
})

async function init(id) {
  await loadQueue()
  if (id) {
    await loadSheet(Number(id))
  } else if (queue.value.length) {
    router.replace(`/review/${queue.value[0].id}`)
  } else {
    sheet.value = null
  }
}

function onKey(e) {
  if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
    e.preventDefault()
    if (!readonly.value) verify(false)
  }
}

onMounted(() => {
  init(route.params.id)
  window.addEventListener('keydown', onKey)
})

onUnmounted(() => {
  window.removeEventListener('keydown', onKey)
  clearInterval(reocrTimer)
})

watch(() => route.params.id, (v) => init(v))
</script>

<style scoped>
.form-grid {
  display: grid;
  grid-template-columns: 76px 1fr 76px 1fr 76px 1fr;
  gap: 8px 10px;
  align-items: center;
}

.form-grid label { font-size: 12.5px; color: #6b7686; text-align: right; }

/* 留痕「字段」列：明细行的序号用浅色小字跟在字段名后面 */
.audit-target {
  margin-left: 6px;
  padding: 0 4px;
  font-size: 11.5px;
  color: #6b7686;
  background: #f2f5f9;
  border-radius: 3px;
}

/* 工时明细：单行表格，一条明细一行，行距压缩 */
.items-table { overflow-x: auto; }

.items-grid {
  display: grid;
  /* 检验员列给到 100px：这一格除了下拉还要塞一张签名缩略图（26px） */
  grid-template-columns: 24px 88px 68px 72px 110px 80px 100px minmax(56px, 1fr) 52px 52px;
  gap: 5px;
  align-items: center;
}

.items-table.wide .items-grid {
  grid-template-columns: 24px 128px 62px 72px 92px 72px 76px 110px 82px 104px 88px 52px 52px;
  min-width: 1102px;
}

.items-head {
  font-size: 12px;
  color: #6b7686;
  padding: 3px 4px 6px;
  border-bottom: 1px solid #e7ebf1;
}

.items-head .num { text-align: right; padding-right: 6px; }

.items-row {
  padding: 3px 4px;
  border-bottom: 1px solid #f2f5fa;
  border-radius: 4px;
  cursor: pointer;
  transition: background .12s;
}

.items-row:last-child { border-bottom: 0; }
.items-row:hover { background: #f8fbff; }
.items-row.active { background: #eff6ff; box-shadow: inset 3px 0 0 #3b82f6; }

.row-no {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 2px;
  font-size: 11.5px;
  color: #4a5568;
}

.flag-dot { width: 6px; height: 6px; border-radius: 50%; display: inline-block; }
.flag-dot.warn { background: #f0a020; }
.flag-dot.block { background: #e34b4b; }

.row-no .flag-dot { cursor: help; }

.num { text-align: right; font-variant-numeric: tabular-nums; }

.items-row .hours-cell { padding-right: 6px; }

.row-ops { display: flex; align-items: center; justify-content: flex-end; gap: 0; }

/* 压缩控件高度：默认 32px → 26px */
.items-row :deep(.el-input),
.items-row :deep(.el-select),
.items-row :deep(.el-input-number) { width: 100%; }

.items-row :deep(.el-input__wrapper),
.items-row :deep(.el-select__wrapper) {
  min-height: 26px;
  padding: 0 7px;
  box-shadow: 0 0 0 1px #e2e8f0 inset;
}

.items-row :deep(.el-input__inner) { height: 24px; font-size: 12.5px; }
.items-row :deep(.el-select__placeholder) { font-size: 12.5px; }
.items-row :deep(.el-input-number .el-input__inner) { text-align: right; }
.items-row :deep(.el-button) { padding: 2px 3px; height: 24px; margin-left: 0; }

.actions { display: flex; align-items: center; gap: 8px; margin-top: 14px; flex-wrap: wrap; }

/* 问题清单：把告警落到具体行、具体字段 */
.flag-panel {
  border: 1px solid #f5d9ab;
  background: #fffdf8;
  border-radius: 8px;
  padding: 7px 12px 8px;
  margin: -4px 0 12px;
}

.flag-panel.danger { border-color: #f3c9c9; background: #fffafa; }

.flag-panel-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12.5px;
  color: #6b7686;
  padding-bottom: 4px;
}

.flag-badge {
  font-size: 11.5px;
  border-radius: 4px;
  padding: 0 6px;
  line-height: 17px;
}

.flag-badge.danger { background: #fdecec; color: #c23934; }
.flag-badge.warn { background: #fef4e4; color: #a5681b; }

.flag-list { max-height: 176px; overflow-y: auto; }

.flag-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 3px 4px;
  font-size: 12.5px;
  border-radius: 4px;
  cursor: pointer;
}

.flag-item:hover { background: #f2f7ff; }

.flag-row {
  flex: none;
  min-width: 42px;
  text-align: center;
  font-size: 11.5px;
  color: #4a5568;
  background: #eef2f8;
  border-radius: 4px;
  padding: 0 6px;
  line-height: 18px;
}

.flag-tag {
  flex: none;
  font-size: 11.5px;
  border-radius: 4px;
  padding: 0 6px;
  line-height: 18px;
  font-weight: 500;
}

.flag-tag.block { background: #fdecec; color: #c23934; }
.flag-tag.warn { background: #fef4e4; color: #a5681b; }

.process-raw {
  font-size: 11px;
  line-height: 1.35;
  color: #8a93a3;
  margin-top: 2px;
  max-width: 120px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.flag-hint { color: #3c4453; }
/* 工时两行显示：上行是小数（参与计算），下行是单据上的 H:MM:SS 写法 */
.hours-hms {
  font-size: 10.5px;
  line-height: 1.3;
  color: #8a93a3;
  text-align: right;
  margin-top: 1px;
}.flag-field { color: #1f6fd0; font-size: 12px; }
.flag-goto { flex: none; color: #1f6fd0; font-size: 12px; opacity: .6; }
.flag-item:hover .flag-goto { opacity: 1; text-decoration: underline; }

/* 检验员格：下拉 + 签名裁图。签名是手写的，模型认不准，让人点开看一眼最快 */
.sign-row {
  display: flex;
  align-items: center;
  gap: 4px;
}
.sign-row :deep(.el-select) { flex: 1 1 auto; min-width: 0; width: 100%; }
.sign-crop {
  flex: none;
  width: 26px;
  height: 22px;
  border: 1px solid #d8dee9;
  border-radius: 3px;
  background: #fff;
  cursor: zoom-in;
}
.sign-crop:hover { border-color: #1f6fd0; }

/* 日期格：选择器 + 「原文 9.0」+ 那一格的裁图。手写「9.10」的 1 只有两像素高，
   模型读不出来，人点开看一眼就定了。缩略图放在下面一行——日期列只有 110px，
   和选择器并排会把「2026-09-10」挤掉 */
.date-hint {
  display: flex;
  align-items: center;
  gap: 3px;
  margin-top: 2px;
}
.date-crop { width: 30px; height: 18px; }
.date-hint .process-raw { margin-top: 0; max-width: none; }

/* 重新识别进行中的提示条 */
.reocr-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
  padding: 6px 10px;
  font-size: 12.5px;
  color: #1f6fd0;
  background: #eff6ff;
  border: 1px solid #cfe2fb;
  border-radius: 6px;
}

.reocr-bar .muted { color: #6b7686; }
.reocr-bar .spin { animation: rspin 1s linear infinite; }

@keyframes rspin { to { transform: rotate(360deg); } }

/* 重新识别确认弹窗 */
.reocr-body { font-size: 13px; color: #3c4453; }
.reocr-lead { margin: 0 0 8px; }
.reocr-keeps { margin-bottom: 8px; }
.reocr-keeps ul { margin: 6px 0 0; padding: 6px 10px; list-style: none; background: #f7f9fc; border-radius: 6px; }
.reocr-keeps li { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; padding: 2px 0; }
.keep-row { font-size: 11.5px; color: #4a5568; background: #eef2f8; border-radius: 4px; padding: 0 6px; line-height: 18px; }
.keep-item { font-size: 12.5px; color: #1f2b3d; background: #fff; border: 1px solid #e3e9f2; border-radius: 4px; padding: 0 6px; line-height: 18px; }
.reocr-note { margin: 4px 0 0; font-size: 12px; color: #6b7686; }
.reocr-warn { display: flex; align-items: center; gap: 6px; font-size: 12.5px; color: #a5681b; background: #fffdf8; border: 1px solid #f5d9ab; border-radius: 6px; padding: 6px 10px; }
</style>
