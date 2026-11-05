<template>
  <div class="page">
    <div class="page-head">
      <span class="page-title">导入与识别</span>
      <span class="page-sub">批量选择工时单照片，系统自动预处理并识别，结果进入核对队列</span>
      <div class="spacer"></div>
      <el-button :loading="loading" @click="refresh"><el-icon><Refresh /></el-icon>刷新</el-button>
    </div>

    <div class="kpi-row">
      <div class="kpi">
        <div class="kpi-label">本批图片</div>
        <div class="kpi-value">{{ images.length }}</div>
      </div>
      <div class="kpi ok">
        <div class="kpi-label">识别完成</div>
        <div class="kpi-value">{{ countBy('done') }}</div>
      </div>
      <div class="kpi warn">
        <div class="kpi-label">排队/识别中</div>
        <div class="kpi-value">{{ countBy('pending') + countBy('processing') }}</div>
      </div>
      <div class="kpi danger">
        <div class="kpi-label">识别失败</div>
        <div class="kpi-value">{{ countBy('failed') }}</div>
      </div>
      <div class="kpi accent">
        <div class="kpi-label">待核对单据</div>
        <div class="kpi-value">{{ store.stats?.counts?.recognized ?? 0 }}</div>
      </div>
    </div>

    <div class="card">
      <div class="row-flex" style="margin-bottom: 12px">
        <span class="section-title" style="margin: 0">导入批次</span>
        <el-select v-model="batchId" placeholder="选择批次" style="width: 240px" @change="loadImages">
          <el-option v-for="b in batches" :key="b.id" :value="b.id"
            :label="`${b.name}（${b.done}/${b.images}）`" />
        </el-select>
        <el-button @click="createBatch"><el-icon><FolderAdd /></el-icon>新建批次</el-button>
        <el-button :disabled="!batchId" @click="retryFailed" :loading="retrying">重试失败项</el-button>
        <div class="spacer"></div>
        <el-tag v-if="periodLocked" type="info" size="small">核算期已锁定，无法导入</el-tag>
      </div>

      <div
        class="dropzone"
        :class="{ active: dragging, disabled: periodLocked }"
        @dragover.prevent="dragging = true"
        @dragleave.prevent="dragging = false"
        @drop.prevent="onDrop"
        @click="pickFiles"
      >
        <input ref="fileInput" type="file" multiple accept="image/*" style="display: none" @change="onPick" />
        <el-icon style="font-size: 30px; color: #93a1b5"><UploadFilled /></el-icon>
        <div class="drop-title">拖拽图片到此处，或点击选择文件</div>
        <div class="drop-sub">支持 JPG / PNG / BMP / WEBP / TIF，可一次多选；上传后自动排队识别</div>
      </div>

      <el-progress v-if="uploading" :percentage="uploadPct" :stroke-width="6" style="margin-top: 12px" />
    </div>

    <div class="card">
      <div class="row-flex" style="margin-bottom: 12px">
        <span class="section-title" style="margin: 0">识别结果</span>
        <span class="muted" style="font-size: 12.5px">点击卡片进入核对工作台</span>
        <div class="spacer"></div>
        <el-button size="small" type="primary" :disabled="!images.length" @click="$router.push('/review')">
          进入核对工作台<el-icon><ArrowRight /></el-icon>
        </el-button>
      </div>

      <div v-if="!images.length" style="padding: 10px 0">
        <el-empty description="暂无图片，请先上传" :image-size="80" />
      </div>

      <div v-else class="grid">
        <div v-for="img in images" :key="img.id" class="thumb-card" @click="openSheet(img)">
          <div class="thumb-img">
            <img v-if="img.thumb_url" :src="img.thumb_url" :alt="img.filename" />
            <div v-else class="thumb-placeholder">
              <el-icon><Picture /></el-icon>
            </div>
            <div class="thumb-status" :class="img.status">{{ statusText(img.status) }}</div>
          </div>
          <div class="thumb-meta">
            <div class="thumb-name" :title="img.filename">{{ img.filename }}</div>
            <div class="thumb-sub">
              <span v-if="img.status === 'failed'" class="err">{{ img.error?.slice(0, 40) }}</span>
              <span v-else-if="img.sheet_id">单据 #{{ img.sheet_id }}</span>
              <span v-else class="muted">等待识别</span>
            </div>
          </div>
          <div v-if="img.status === 'failed'" class="thumb-actions">
            <el-button size="small" text type="danger" @click.stop="retryImg(img)">重试</el-button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { api } from '../api'
import { refreshStats, store } from '../store'

const router = useRouter()
const batches = ref([])
const batchId = ref(null)
const images = ref([])
const dragging = ref(false)
const uploading = ref(false)
const uploadPct = ref(0)
const loading = ref(false)
const retrying = ref(false)
const fileInput = ref(null)
let timer = null

const periodLocked = computed(() => store.periods.find((p) => p.ym === store.ym)?.status === 'locked')

function countBy(status) {
  return images.value.filter((i) => i.status === status).length
}

function statusText(s) {
  return { pending: '排队', processing: '识别中', done: '已完成', failed: '失败' }[s] || s
}

async function loadBatches() {
  const period = store.periods.find((p) => p.ym === store.ym)
  if (!period) return
  batches.value = await api.batches(period.id)
  if (!batches.value.length) {
    await createBatch(true)
  } else if (!batchId.value) {
    batchId.value = batches.value[0].id
  }
  await loadImages()
}

async function createBatch(silent = false) {
  const period = store.periods.find((p) => p.ym === store.ym)
  if (!period) return
  const r = await api.createBatch({ period_id: period.id })
  if (!silent) ElMessage.success('批次已创建')
  await loadBatches()
  batchId.value = r.id
}

async function loadImages() {
  if (!batchId.value) return
  loading.value = true
  try {
    images.value = await api.batchImages(batchId.value)
  } finally {
    loading.value = false
  }
  schedulePoll()
}

function schedulePoll() {
  const busy = images.value.some((i) => i.status === 'pending' || i.status === 'processing')
  if (timer) {
    clearTimeout(timer)
    timer = null
  }
  if (busy) {
    timer = setTimeout(async () => {
      await loadImages()
      await refreshStats()
    }, 2000)
  }
}

async function refresh() {
  await loadBatches()
  await refreshStats()
}

function pickFiles() {
  if (periodLocked.value) return ElMessage.warning('核算期已锁定，无法导入')
  fileInput.value?.click()
}

function onPick(e) {
  const files = Array.from(e.target.files || [])
  e.target.value = ''
  if (files.length) upload(files)
}

function onDrop(e) {
  dragging.value = false
  if (periodLocked.value) return ElMessage.warning('核算期已锁定，无法导入')
  const files = Array.from(e.dataTransfer.files || []).filter((f) => f.type.startsWith('image/'))
  if (files.length) upload(files)
}

async function upload(files) {
  if (!batchId.value) await createBatch(true)
  uploading.value = true
  uploadPct.value = 10
  try {
    const r = await api.upload(batchId.value, files)
    const ok = r.results.filter((x) => x.ok).length
    const dup = r.results.filter((x) => x.duplicate).length
    const bad = r.results.filter((x) => !x.ok && !x.duplicate).length
    uploadPct.value = 100
    ElMessage.success(`已上传 ${ok} 张${dup ? `，跳过重复 ${dup} 张` : ''}${bad ? `，失败 ${bad} 张` : ''}`)
    await loadImages()
    await refreshStats()
  } finally {
    uploading.value = false
    setTimeout(() => (uploadPct.value = 0), 800)
  }
}

async function retryFailed() {
  retrying.value = true
  try {
    const r = await api.retryFailed(batchId.value)
    ElMessage.success(`已重新入队 ${r.count} 张`)
    await loadImages()
  } finally {
    retrying.value = false
  }
}

async function retryImg(img) {
  const r = await api.reocrImage(img.id)
  await loadImages()
  // 已核对过的单据会带着人工修改一起重跑（不然选好的人、改过的工时会被覆盖），
  // 这里回显一句，避免让人以为「怎么又跑了一遍」
  if (r?.kept_fields) ElMessage.success(`已重新识别，保留 ${r.kept_fields} 处人工修改`)
  else ElMessage.success('已重新识别')
}

function openSheet(img) {
  if (img.sheet_id) router.push(`/review/${img.sheet_id}`)
  else if (img.status === 'failed') retryImg(img)
  else ElMessage.info('识别中，请稍候')
}

onMounted(async () => {
  await loadBatches()
})

onUnmounted(() => timer && clearTimeout(timer))
</script>

<style scoped>
.dropzone {
  border: 1.5px dashed #c8d2e0;
  border-radius: 10px;
  padding: 26px;
  text-align: center;
  cursor: pointer;
  transition: all .18s;
  background: #fbfcfe;
}

.dropzone:hover, .dropzone.active { border-color: #3b82f6; background: #f2f7ff; }
.dropzone.disabled { cursor: not-allowed; opacity: .55; }

.drop-title { margin-top: 8px; font-size: 14px; font-weight: 500; }
.drop-sub { margin-top: 4px; font-size: 12.5px; color: #8b93a1; }

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(178px, 1fr));
  gap: 12px;
}

.thumb-card {
  border: 1px solid #e7ebf1;
  border-radius: 9px;
  overflow: hidden;
  cursor: pointer;
  background: #fff;
  transition: box-shadow .16s, border-color .16s;
  position: relative;
}

.thumb-card:hover { border-color: #a9c7f0; box-shadow: 0 3px 14px rgba(31, 111, 208, .12); }

.thumb-img {
  height: 132px;
  background: #f4f6f9;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  position: relative;
}

.thumb-img img { width: 100%; height: 100%; object-fit: contain; }

.thumb-placeholder { color: #b7c0cd; font-size: 26px; }

.thumb-status {
  position: absolute;
  top: 6px; left: 6px;
  font-size: 11.5px;
  padding: 1px 7px;
  border-radius: 20px;
  background: #eef1f6;
  color: #5b6675;
}

.thumb-status.done { background: #e8f7ee; color: #26714a; }
.thumb-status.failed { background: #fdecec; color: #a32d2d; }
.thumb-status.processing { background: #fff4e2; color: #91591a; }

.thumb-meta { padding: 8px 9px 9px; }
.thumb-name { font-size: 12.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.thumb-sub { font-size: 11.5px; margin-top: 3px; color: #6b7686; }
.thumb-sub .err { color: #c0392b; }
.thumb-actions { position: absolute; right: 6px; bottom: 6px; }
</style>
