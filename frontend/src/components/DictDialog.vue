<template>
  <el-dialog
    :model-value="modelValue"
    :title="schema.title"
    width="min(860px, 94vw)"
    top="6vh"
    @update:model-value="close"
  >
    <div class="dict-toolbar">
      <el-input
        v-model="keyword"
        size="small"
        :placeholder="schema.searchHint"
        clearable
        style="width: 220px"
      >
        <template #prefix><el-icon><Search /></el-icon></template>
      </el-input>
      <el-button size="small" type="primary" @click="openForm()">
        <el-icon><Plus /></el-icon>新增
      </el-button>
      <el-button size="small" @click="importVisible = true">批量导入</el-button>
      <div class="spacer"></div>
      <span class="muted">
        共 {{ rows.length }} 条 · 启用 {{ rows.filter((r) => r.active !== false).length }}
      </span>
    </div>

    <el-alert
      v-if="schema.tip"
      :title="schema.tip"
      type="info"
      :closable="false"
      show-icon
      class="dict-tip"
    />

    <el-table :data="filtered" size="small" height="420" v-loading="loading" style="width: 100%">
      <el-table-column
        v-for="col in schema.columns"
        :key="col.prop"
        :prop="col.prop"
        :label="col.label"
        :width="col.width"
        :min-width="col.minWidth"
        show-overflow-tooltip
      >
        <template #default="{ row }">
          <span v-if="col.type === 'bool'">
            <el-tag v-if="row[col.prop]" size="small" type="success">启用</el-tag>
            <el-tag v-else size="small" type="info">停用</el-tag>
          </span>
          <span v-else-if="col.type === 'number'">{{ row[col.prop] ?? '—' }}</span>
          <span v-else-if="col.prop === 'aliases'" class="alias-cell">{{ row.aliases || '—' }}</span>
          <span v-else>{{ row[col.prop] || '—' }}</span>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="130" fixed="right">
        <template #default="{ row }">
          <el-button size="small" text type="primary" @click="openForm(row)">编辑</el-button>
          <el-button size="small" text type="danger" @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
      <template #empty>
        <div class="empty-tip">还没有数据，点「新增」或「批量导入」开始维护</div>
      </template>
    </el-table>

    <!-- 新增 / 编辑 -->
    <el-dialog v-model="formVisible" :title="form.id ? '编辑' : '新增'" width="460px" append-to-body>
      <el-form :model="form" label-width="92px" size="default">
        <el-form-item v-for="f in schema.fields" :key="f.prop" :label="f.label" :required="f.required">
          <el-input
            v-if="!f.type || f.type === 'text'"
            v-model="form[f.prop]"
            :placeholder="f.placeholder || ''"
          />
          <el-input
            v-else-if="f.type === 'textarea'"
            v-model="form[f.prop]"
            type="textarea"
            :rows="2"
            :placeholder="f.placeholder || ''"
          />
          <el-switch v-else-if="f.type === 'bool'" v-model="form[f.prop]" />
          <el-input-number v-else-if="f.type === 'number'" v-model="form[f.prop]" :min="0" />
          <div v-if="f.hint" class="field-hint">{{ f.hint }}</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="formVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>

    <!-- 批量导入：文件 / 粘贴两条路径共用一套解析 -->
    <ImportDialog
      v-model="importVisible"
      :kind="kind"
      :title="`批量导入${schema.title.replace('管理', '')}`"
      :sample="schema.importSample"
      :hint="schema.importHint"
      :columns="schema.importColumns || ''"
      @done="onImported"
    />
  </el-dialog>
</template>

<script setup>
import { computed, ref, nextTick, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api'
import ImportDialog from './ImportDialog.vue'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  kind: { type: String, required: true },
  schema: { type: Object, required: true }
})
const emit = defineEmits(['update:modelValue', 'changed'])

const rows = ref([])
const keyword = ref('')
const loading = ref(false)
const formVisible = ref(false)
const saving = ref(false)
const importVisible = ref(false)
const form = ref({})
const route = useRoute()

const filtered = computed(() => {
  const k = keyword.value.trim()
  if (!k) return rows.value
  return rows.value.filter((r) =>
    Object.values(r).some((v) => typeof v === 'string' && v.includes(k))
  )
})

function close() {
  emit('update:modelValue', false)
}

// 用 watch(immediate) 而不是 el-dialog 的 @open：从卡片切换管理项时 :key 变化会让组件
// 带着 modelValue=true 重建，此时 open 事件不会触发，列表就永远是空的。
watch(
  () => props.modelValue,
  (v) => {
    if (v) onOpen()
  },
  { immediate: true }
)

async function onOpen() {
  keyword.value = ''
  await load()
  // 深链：/admin?open=products&import=1 直接打开导入窗口
  // 要等外层弹窗的进场动画结束，否则内层弹窗会被吞掉
  if (route.query.import) {
    await nextTick()
    setTimeout(() => {
      importVisible.value = true
    }, 320)
  }
}

async function load() {
  loading.value = true
  try {
    const r = await api.adminList(props.kind)
    if (!r || !Array.isArray(r.items)) {
      // 接口异常时宁可报错，也不要静默显示空表
      ElMessage.error(`加载失败：${props.kind} 列表返回异常`)
      rows.value = []
      return
    }
    rows.value = r.items
  } finally {
    loading.value = false
  }
}

function blank() {
  const o = {}
  for (const f of props.schema.fields) {
    o[f.prop] = f.type === 'bool' ? true : f.type === 'number' ? 999 : ''
  }
  return o
}

function openForm(row) {
  form.value = row ? { ...row } : { ...blank(), ...(props.schema.defaults || {}) }
  formVisible.value = true
}

async function save() {
  const required = props.schema.fields.filter((f) => f.required)
  for (const f of required) {
    if (!String(form.value[f.prop] || '').trim()) return ElMessage.warning(`请填写${f.label}`)
  }
  saving.value = true
  try {
    const payload = { ...form.value }
    delete payload.id
    if (form.value.id) await api.adminUpdate(props.kind, form.value.id, payload)
    else await api.adminCreate(props.kind, payload)
    ElMessage.success('已保存')
    formVisible.value = false
    await load()
    emit('changed')
  } finally {
    saving.value = false
  }
}

async function remove(row) {
  const name = row.name || row.code || row.id
  await ElMessageBox.confirm(
    `确认删除「${name}」？删除后该值不再参与识别兜底，历史单据不受影响。`,
    '删除确认',
    { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
  )
  await api.adminDelete(props.kind, row.id)
  ElMessage.success('已删除')
  await load()
  emit('changed')
}

async function onImported() {
  await load()
  emit('changed')
}
</script>

<style scoped>
.dict-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}
.dict-tip {
  margin-bottom: 10px;
}
.alias-cell {
  color: #6b7280;
  font-size: 12px;
}
.empty-tip {
  padding: 26px 0;
  color: #9aa3b2;
  font-size: 13px;
}
.field-hint {
  font-size: 11.5px;
  color: #9aa3b2;
  line-height: 1.5;
  margin-top: 2px;
}
</style>
