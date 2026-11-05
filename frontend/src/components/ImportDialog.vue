<template>
  <el-dialog
    :model-value="modelValue"
    :title="title"
    width="620px"
    top="8vh"
    append-to-body
    @update:model-value="emit('update:modelValue', $event)"
    @open="reset"
  >
    <el-tabs v-model="tab">
      <el-tab-pane label="上传文件" name="file">
        <el-upload
          drag
          :auto-upload="false"
          :show-file-list="false"
          accept=".xlsx,.xlsm,.csv,.txt"
          :on-change="onPick"
        >
          <el-icon class="el-icon--upload"><UploadFilled /></el-icon>
          <div class="el-upload__text">把 Excel / CSV 拖到这里，或<em>点击选择文件</em></div>
        </el-upload>
        <div class="hint">
          支持 .xlsx / .csv / .txt（旧版 .xls 请先另存为 .xlsx）。首行是表头时按列名自动对应，
          列顺序不必和模板一致；没有表头则按「{{ columns }}」的顺序对应。
        </div>
        <div v-if="file" class="picked">
          <el-icon><Document /></el-icon>
          <span class="picked-name">{{ file.name }}</span>
          <span class="muted">{{ (file.size / 1024).toFixed(1) }} KB</span>
          <el-button text size="small" @click="file = null">移除</el-button>
        </div>
      </el-tab-pane>

      <el-tab-pane label="粘贴文本" name="text">
        <el-input v-model="text" type="textarea" :rows="9" :placeholder="sample" />
        <div class="hint">{{ hint }}</div>
      </el-tab-pane>
    </el-tabs>

    <div class="foot-row">
      <el-radio-group v-model="mode" size="small">
        <el-radio-button value="append">仅新增（跳过重复）</el-radio-button>
        <el-radio-button value="replace">已存在则覆盖</el-radio-button>
      </el-radio-group>
      <a class="tpl" :href="`/api/admin/${kind}/template`" download>
        <el-icon><Download /></el-icon>下载模板
      </a>
    </div>

    <div v-if="result" class="result">
      <div class="result-row">
        <el-tag type="success" size="small">新增 {{ result.created }}</el-tag>
        <el-tag type="primary" size="small">更新 {{ result.updated }}</el-tag>
        <el-tag type="info" size="small">跳过 {{ result.skipped }}</el-tag>
        <span class="muted">{{ result.mapped_by }}</span>
      </div>
      <ul v-if="result.errors && result.errors.length" class="errors">
        <li v-for="(e, i) in result.errors" :key="i">{{ e }}</li>
      </ul>
    </div>

    <template #footer>
      <el-button @click="emit('update:modelValue', false)">关闭</el-button>
      <el-button type="primary" :loading="loading" @click="submit">开始导入</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  kind: { type: String, required: true },
  title: { type: String, default: '批量导入' },
  sample: { type: String, default: '' },
  hint: { type: String, default: '' },
  columns: { type: String, default: '' }
})
const emit = defineEmits(['update:modelValue', 'done'])

const tab = ref('file')
const text = ref('')
const file = ref(null)
const mode = ref('append')
const loading = ref(false)
const result = ref(null)

function reset() {
  tab.value = 'file'
  text.value = ''
  file.value = null
  mode.value = 'append'
  result.value = null
}

function onPick(uploadFile) {
  file.value = uploadFile.raw || uploadFile
  result.value = null
}

async function submit() {
  if (tab.value === 'file' && !file.value) return ElMessage.warning('请先选择文件')
  if (tab.value === 'text' && !text.value.trim()) return ElMessage.warning('请粘贴要导入的内容')

  loading.value = true
  try {
    const data =
      tab.value === 'file'
        ? await api.adminImportFile(props.kind, file.value, mode.value)
        : await api.adminImport(props.kind, text.value, mode.value)
    result.value = data
    if (data.created + data.updated > 0) {
      ElMessage.success(`新增 ${data.created} 条，更新 ${data.updated} 条`)
    } else {
      ElMessage.warning(`跳过 ${data.skipped} 条（字典里已存在），可切到「已存在则覆盖」再导入`)
    }
    emit('done')
  } catch {
    // 拦截器已提示具体原因
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.hint {
  margin-top: 8px;
  font-size: 12px;
  line-height: 1.6;
  color: #8a93a3;
}
.picked {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 10px;
  padding: 7px 10px;
  background: #f2f6ff;
  border: 1px solid #d8e2f8;
  border-radius: 6px;
  font-size: 13px;
}
.picked-name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.foot-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px solid #eef1f6;
}
.tpl {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12.5px;
  color: #2563eb;
  text-decoration: none;
}
.tpl:hover {
  text-decoration: underline;
}
.result {
  margin-top: 12px;
  padding: 10px 12px;
  background: #f7f9fc;
  border-radius: 6px;
}
.result-row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
}
.errors {
  margin: 8px 0 0;
  padding-left: 16px;
  font-size: 12px;
  color: #b45309;
  line-height: 1.6;
}
</style>
