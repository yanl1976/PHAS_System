<template>
  <el-dialog
    :model-value="modelValue"
    title="人员与角色管理"
    width="1000px"
    top="6vh"
    @update:model-value="close"
    @open="onOpen"
  >
    <div class="dict-toolbar">
      <el-input v-model="keyword" size="small" placeholder="姓名 / 工号 / 班组 / 别名" clearable style="width: 210px">
        <template #prefix><el-icon><Search /></el-icon></template>
      </el-input>
      <el-select v-model="roleFilter" size="small" placeholder="按角色筛选" clearable style="width: 150px">
        <el-option v-for="r in roles" :key="r.id" :value="r.id" :label="`${r.name}（${r.worker_count}）`" />
      </el-select>
      <el-button size="small" type="primary" @click="openForm()">
        <el-icon><Plus /></el-icon>新增人员
      </el-button>
      <el-button size="small" @click="importVisible = true">批量导入</el-button>
      <div class="spacer"></div>
      <span class="muted">共 {{ rows.length }} 人 · 启用 {{ rows.filter((r) => r.active).length }}</span>
    </div>

    <el-alert
      title="人员按角色进入不同识别名册：工作者→明细「工作者」列，检验员→「检验员」列，接交人→「接交人」列。手写姓名走封闭集匹配，同姓唯一时残字也能兜住（如「杰」→「杜文杰」）。"
      type="info"
      :closable="false"
      show-icon
      class="dict-tip"
    />

    <el-table :data="filtered" size="small" height="430" v-loading="loading" style="width: 100%">
      <el-table-column prop="name" label="姓名" width="100" fixed />
      <el-table-column prop="employee_no" label="工号" width="90">
        <template #default="{ row }">{{ row.employee_no || '—' }}</template>
      </el-table-column>
      <el-table-column prop="team" label="班组" width="90" />
      <el-table-column prop="default_process" label="工种" width="110">
        <template #default="{ row }">{{ row.default_process || '—' }}</template>
      </el-table-column>
      <el-table-column label="角色" min-width="200">
        <template #default="{ row }">
          <el-tag
            v-for="(n, i) in row.role_names"
            :key="n"
            size="small"
            :type="roleTagType(row.role_ids[i])"
            class="role-tag"
          >
            {{ n }}
          </el-tag>
          <span v-if="!row.role_names.length" class="muted">未分配</span>
        </template>
      </el-table-column>
      <el-table-column label="别名" min-width="140">
        <template #default="{ row }">
          <span class="alias-cell">{{ row.aliases || '—' }}</span>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="80">
        <template #default="{ row }">
          <el-tag v-if="row.active" size="small" type="success">启用</el-tag>
          <el-tag v-else size="small" type="info">停用</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="126" fixed="right">
        <template #default="{ row }">
          <el-button size="small" text type="primary" @click="openForm(row)">编辑</el-button>
          <el-button size="small" text type="danger" @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
      <template #empty>
        <div class="empty-tip">还没有人员，点「新增人员」或「批量导入」开始维护</div>
      </template>
    </el-table>

    <!-- 人员编辑 -->
    <el-dialog v-model="formVisible" :title="form.id ? '编辑人员' : '新增人员'" width="520px" append-to-body>
      <el-form :model="form" label-width="86px">
        <el-form-item label="姓名" required>
          <el-input v-model="form.name" placeholder="与单据上手写一致的姓名" />
        </el-form-item>
        <el-form-item label="工号">
          <el-input v-model="form.employee_no" placeholder="选填" />
        </el-form-item>
        <el-form-item label="班组">
          <el-input v-model="form.team" placeholder="如 2车间 / 质检" />
        </el-form-item>
        <el-form-item label="工种">
          <el-input v-model="form.default_process" placeholder="如 车工 / 铣工，用于人工核对时提示" />
        </el-form-item>
        <el-form-item label="角色">
          <el-select v-model="form.role_ids" multiple placeholder="选择角色" style="width: 100%">
            <el-option v-for="r in roles" :key="r.id" :value="r.id" :label="r.name">
              <span>{{ r.name }}</span>
              <span class="opt-hint">{{ kindText(r.kind) }}</span>
            </el-option>
          </el-select>
          <div class="field-hint">
            只勾「检验员」的人不会出现在工作者候选里；一人可兼多角色。
          </div>
        </el-form-item>
        <el-form-item label="识别别名">
          <el-input v-model="form.aliases" placeholder="逗号分隔，如 小马,马工" />
          <div class="field-hint">手写签名的常见变体写在这里，能显著提高匹配率。</div>
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="form.active" />
          <span class="field-hint" style="margin-left: 10px">停用后不参与识别匹配，历史数据保留</span>
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
      kind="workers"
      title="批量导入人员"
      hint="每行一人：姓名, 工号, 班组, 工种, 别名, 角色（多个角色用 / 分隔）"
      columns="姓名 / 工号 / 班组 / 工种 / 别名 / 角色"
      sample="张三, 001, 2车间, 车工, 小张, 工作者&#10;李四, 002, 质检, 检验, , 检验员"
      @done="onImported"
    />
  </el-dialog>
</template>

<script setup>
import { computed, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api'
import ImportDialog from './ImportDialog.vue'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  roles: { type: Array, default: () => [] }
})
const emit = defineEmits(['update:modelValue', 'changed'])

const rows = ref([])
const keyword = ref('')
const roleFilter = ref(null)
const loading = ref(false)
const formVisible = ref(false)
const saving = ref(false)
const importVisible = ref(false)
const form = ref({})

const filtered = computed(() => {
  const k = keyword.value.trim()
  return rows.value.filter((r) => {
    if (roleFilter.value && !r.role_ids.includes(roleFilter.value)) return false
    if (!k) return true
    return [r.name, r.employee_no, r.team, r.default_process, r.aliases].some(
      (v) => v && v.includes(k)
    )
  })
})

function close() {
  emit('update:modelValue', false)
}

function kindText(kind) {
  return { worker: '识别：工作者列', inspector: '识别：检验员列', receiver: '识别：接交人列', other: '仅归类' }[kind] || ''
}

function roleTagType(id) {
  const r = props.roles.find((x) => x.id === id)
  if (!r) return 'info'
  return { worker: 'primary', inspector: 'warning', receiver: 'success', other: 'info' }[r.kind] || 'info'
}

async function onOpen() {
  keyword.value = ''
  roleFilter.value = null
  await load()
}

async function load() {
  loading.value = true
  try {
    const r = await api.adminList('workers')
    rows.value = r.items
  } finally {
    loading.value = false
  }
}

function openForm(row) {
  form.value = row
    ? { ...row }
    : { name: '', employee_no: '', team: '2车间', default_process: '', aliases: '', active: true, role_ids: [] }
  formVisible.value = true
}

async function save() {
  if (!form.value.name?.trim()) return ElMessage.warning('请填写姓名')
  saving.value = true
  try {
    const payload = { ...form.value }
    delete payload.id
    delete payload.role_names
    delete payload.is_inspector
    if (form.value.id) await api.adminUpdate('workers', form.value.id, payload)
    else await api.adminCreate('workers', payload)
    ElMessage.success('已保存')
    formVisible.value = false
    await load()
    emit('changed')
  } finally {
    saving.value = false
  }
}

async function remove(row) {
  await ElMessageBox.confirm(
    `确认删除人员「${row.name}」？若已被工时明细引用将无法删除，可改为停用。`,
    '删除确认',
    { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
  )
  await api.adminDelete('workers', row.id)
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
.role-tag {
  margin-right: 4px;
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
.opt-hint {
  float: right;
  color: #9aa3b2;
  font-size: 11.5px;
}
</style>
