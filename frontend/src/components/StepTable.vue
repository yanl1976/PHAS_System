<template>
  <div class="step-table-wrap">
    <div v-if="showTools" class="row-flex tools">
      <!-- 各弹窗自己的入口（从历史生成、引用其他路线）插在最前面，
           通用工具排在后面，两个弹窗的工具条就自动长得一样 -->
      <slot name="tools" />
      <el-button size="small" :disabled="readonly" @click="addStep">加一行</el-button>
      <el-button size="small" :disabled="readonly" @click="sortBySeq">按工序号排序</el-button>
      <el-button size="small" :disabled="readonly" @click="renumber">重排工序号（10/20/30…）</el-button>
      <div class="spacer"></div>
      <span class="muted">{{ steps.length }} 道工序</span>
    </div>

    <el-table :data="steps" size="small" row-key="uid" max-height="420" class="step-table">
      <el-table-column label="排序" width="64" align="center">
        <template #default="{ $index }">
          <div class="ord-wrap">
            <el-button
              link
              class="ord-btn"
              :disabled="readonly || $index === 0"
              title="上移一行（工序号自动互换）"
              @click="moveStep($index, -1)"
            >
              <el-icon><ArrowUp /></el-icon>
            </el-button>
            <el-button
              link
              class="ord-btn"
              :disabled="readonly || $index === steps.length - 1"
              title="下移一行（工序号自动互换）"
              @click="moveStep($index, 1)"
            >
              <el-icon><ArrowDown /></el-icon>
            </el-button>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="工序号" width="86" align="center">
        <template #default="{ row }">
          <el-input-number
            v-model="row.seq"
            size="small"
            :min="0"
            :step="10"
            :controls="false"
            :disabled="readonly"
            style="width: 70px"
          />
        </template>
      </el-table-column>
      <el-table-column label="工序" min-width="150">
        <template #default="{ row }">
          <el-select
            v-model="row.process_code"
            size="small"
            filterable
            allow-create
            default-first-option
            placeholder="选或输入工序"
            class="proc-cell"
            style="width: 100%"
            :disabled="readonly"
            @change="onProcessChange(row)"
          >
            <el-option v-for="p in processOptions" :key="p.code" :value="p.code" :label="p.name" />
          </el-select>
        </template>
      </el-table-column>
      <el-table-column label="工种" width="96">
        <template #default="{ row }">
          <el-input v-model="row.category" size="small" :disabled="readonly" />
        </template>
      </el-table-column>
      <el-table-column label="设备 / 工位" width="120">
        <template #default="{ row }">
          <el-input v-model="row.work_center" size="small" :disabled="readonly" />
        </template>
      </el-table-column>
      <el-table-column label="准备工时" width="106" align="right">
        <template #default="{ row }">
          <el-input-number
            v-model="row.setup_hours"
            size="small"
            :min="0"
            :step="0.1"
            :precision="3"
            :controls="false"
            :disabled="readonly"
            style="width: 100%"
          />
        </template>
      </el-table-column>
      <el-table-column label="单件工时" width="124" align="right">
        <template #default="{ row }">
          <el-input-number
            v-model="row.unit_hours"
            size="small"
            :min="0"
            :step="0.25"
            :precision="4"
            :controls="false"
            :disabled="readonly"
            style="width: 100%"
          />
        </template>
      </el-table-column>
      <el-table-column label="关键" width="56" align="center">
        <template #default="{ row }">
          <el-checkbox v-model="row.is_key" :disabled="readonly" />
        </template>
      </el-table-column>
      <el-table-column label="需检验" width="66" align="center">
        <template #default="{ row }">
          <el-checkbox v-model="row.inspect_required" :disabled="readonly" />
        </template>
      </el-table-column>
      <el-table-column label="备注" min-width="120">
        <template #default="{ row }">
          <el-input v-model="row.remark" size="small" :disabled="readonly" />
        </template>
      </el-table-column>
      <el-table-column width="50" align="center">
        <template #default="{ $index }">
          <el-button link type="danger" size="small" :disabled="readonly" @click="steps.splice($index, 1)">
            删
          </el-button>
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>

<script setup>
/**
 * 工序明细表：一道工序一行的可编辑表格。
 *
 * 为什么单独拆出来：这张表在「生产任务 → 编辑工序」和「结构 → 编制工序」两个
 * 弹窗里长得必须一模一样（同样的列、同样的上下移、同样的重排）。
 * 以前只有任务弹窗一家，复制一份给结构弹窗就等于给以后埋一个「改一处忘一处」。
 *
 * 直接改 props.steps 里的行对象，不发 v-model：
 * 这些行是父组件 reactive 数组里的同一批对象，行内编辑本来就是就地改；
 * 换成 emit 一份新数组反而要深拷贝、丢 uid 引用、还得处理选中态。
 * 增/删行也是同理（父组件的数组本身就是响应式的）。
 */
import { ArrowUp, ArrowDown } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'

const props = defineProps({
  steps: { type: Array, required: true },
  processOptions: { type: Array, default: () => [] },
  // 只读：已发布/已归档的路线不许就地改（改了历史单据的比对基准就没了）
  readonly: { type: Boolean, default: false },
  showTools: { type: Boolean, default: true }
})

let uid = 0
function nextUid() {
  uid += 1
  return `r${uid}`
}

function addStep() {
  const last = props.steps[props.steps.length - 1]
  props.steps.push({
    uid: nextUid(),
    seq: last ? (Number(last.seq) || 0) + 10 : 10,
    process_code: '',
    process_name: '',
    op_type: 'self',
    category: '',
    work_center: '',
    setup_hours: 0,
    unit_hours: 0,
    is_key: false,
    inspect_required: false,
    remark: ''
  })
}

function sortBySeq() {
  props.steps.sort((a, b) => (Number(a.seq) || 0) - (Number(b.seq) || 0))
  ElMessage.success('已按工序号排序')
}

// 上下移动；工序号跟着互换，保证「工序号顺序 = 显示顺序」
function moveStep(index, delta) {
  const list = props.steps
  const to = index + delta
  if (to < 0 || to >= list.length) return
  const sa = list[index].seq
  list[index].seq = list[to].seq
  list[to].seq = sa
  const [row] = list.splice(index, 1)
  list.splice(to, 0, row)
  const mono = list.every((r, i) => i === 0 || Number(list[i - 1].seq) < Number(r.seq))
  if (!mono) list.forEach((r, i) => { r.seq = (i + 1) * 10 })
}

function renumber() {
  props.steps.forEach((r, i) => {
    r.seq = (i + 1) * 10
  })
}

function onProcessChange(row) {
  const p = props.processOptions.find((x) => x.code === row.process_code)
  if (p) {
    row.process_name = p.name
    if (!row.category && p.category) row.category = p.category
  } else {
    row.process_name = row.process_code
  }
}
</script>

<style scoped>
.tools { margin-bottom: 8px; }
.step-table :deep(.cell) { padding: 4px 6px; }

/* 排序按钮 + 工序名加粗加黑，与编排页保持一致 */
.ord-wrap {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
}
.ord-btn {
  padding: 0;
  margin: 0 !important;
  width: 22px;
  height: 22px;
  color: #5b6675;
}
.ord-btn :deep(.el-icon) { font-size: 15px; }
.ord-btn:not(.is-disabled):hover {
  color: #1f6fd0;
  background: #eef4fd;
  border-radius: 4px;
}
.ord-btn.is-disabled { opacity: .3; }

.step-table :deep(.proc-cell .el-input__inner),
.step-table :deep(.proc-cell input),
.step-table :deep(.proc-cell .el-select__selected-item:not(.is-transparent)),
.step-table :deep(.proc-cell .el-select__placeholder:not(.is-transparent)) {
  font-weight: 700;
  color: #11151b;
  -webkit-text-fill-color: #11151b;
}
</style>
