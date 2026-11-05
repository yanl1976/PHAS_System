<template>
  <div class="viewer">
    <div class="viewer-toolbar">
      <el-button-group size="small">
        <el-button @click="zoomOut"><el-icon><ZoomOut /></el-icon></el-button>
        <el-button @click="zoomFit">适应</el-button>
        <el-button @click="zoomIn"><el-icon><ZoomIn /></el-icon></el-button>
      </el-button-group>
      <span class="muted mono" style="font-size: 12px">{{ Math.round(scale * 100) }}%</span>
      <el-divider direction="vertical" />
      <el-button size="small" @click="rotate"><el-icon><RefreshRight /></el-icon>旋转</el-button>
      <el-checkbox v-model="localMask" size="small" :disabled="!maskSrc">显示红笔层</el-checkbox>
      <el-checkbox v-model="localBoxes" size="small">显示字段框</el-checkbox>
      <div class="spacer"></div>
      <el-tag v-if="activeLabel" size="small" type="primary">{{ activeLabel }}</el-tag>
    </div>

    <div ref="stage" class="viewer-stage">
      <div class="canvas" :style="canvasStyle">
        <img v-if="src" :src="src" :alt="alt" @load="onLoad" />
        <img v-if="localMask && maskSrc" :src="maskSrc" class="mask-layer" />
        <svg v-if="pageW && pageH" :viewBox="`0 0 ${pageW} ${pageH}`" preserveAspectRatio="none">
          <rect
            v-for="box in boxList"
            :key="box.key"
            :x="box.x"
            :y="box.y"
            :width="box.w"
            :height="box.h"
            :stroke="box.color"
            :stroke-width="Math.max(2, pageW / 420)"
            fill="transparent"
            rx="4"
            class="pickable"
            @click="$emit('pick', box.key)"
          />
        </svg>
      </div>
      <el-empty v-if="!src" description="无图片" :image-size="70" />
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'

const props = defineProps({
  src: { type: String, default: '' },
  maskSrc: { type: String, default: '' },
  pageW: { type: Number, default: 0 },
  pageH: { type: Number, default: 0 },
  boxes: { type: Object, default: () => ({}) },
  activeKey: { type: String, default: '' },
  labels: { type: Object, default: () => ({}) },
  alt: { type: String, default: '工时单' }
})

defineEmits(['pick'])

const stage = ref(null)
const zoom = ref(1)
const rotation = ref(0)
const fitScale = ref(1)
const localMask = ref(false)
const localBoxes = ref(true)

const FIELD_COLORS = {
  unit_hours: '#2f7be5',
  qualified_qty: '#2f9e66',
  process: '#b8860b',
  worker_raw: '#8e44ad',
  finish_date: '#d9822b'
}

const scale = computed(() => fitScale.value * zoom.value)

const canvasStyle = computed(() => ({
  width: props.pageW ? `${props.pageW * scale.value}px` : '100%',
  transform: `rotate(${rotation.value}deg)`
}))

const boxList = computed(() => {
  if (!localBoxes.value) return []
  return Object.entries(props.boxes || {})
    .filter(([, b]) => Array.isArray(b) && b.length === 4)
    .map(([key, b]) => ({
      key,
      x: b[0],
      y: b[1],
      w: Math.max(4, b[2] - b[0]),
      h: Math.max(4, b[3] - b[1]),
      color: key === props.activeKey ? '#e8484a' : FIELD_COLORS[key] || '#2f7be5'
    }))
})

const activeLabel = computed(() => (props.activeKey ? props.labels[props.activeKey] || props.activeKey : ''))

function computeFit() {
  if (!stage.value || !props.pageW) return
  const avail = stage.value.clientWidth - 24
  const availH = stage.value.clientHeight - 24
  const s = Math.min(avail / props.pageW, availH / props.pageH, 1.6)
  fitScale.value = Math.max(0.12, s)
}

function onLoad() {
  computeFit()
}

function zoomIn() {
  zoom.value = Math.min(4, +(zoom.value * 1.2).toFixed(3))
}

function zoomOut() {
  zoom.value = Math.max(0.2, +(zoom.value / 1.2).toFixed(3))
}

function zoomFit() {
  zoom.value = 1
  rotation.value = 0
  computeFit()
}

function rotate() {
  rotation.value = (rotation.value + 90) % 360
  if (rotation.value % 180 !== 0) {
    const w = props.pageW
    const h = props.pageH
    const avail = stage.value.clientWidth - 24
    const availH = stage.value.clientHeight - 24
    fitScale.value = Math.max(0.12, Math.min(avail / h, availH / w, 1.6))
  } else {
    computeFit()
  }
}

onMounted(() => {
  computeFit()
  window.addEventListener('resize', computeFit)
})
onUnmounted(() => window.removeEventListener('resize', computeFit))
watch(() => [props.src, props.pageW, props.pageH], computeFit)
</script>

<style scoped>
.mask-layer { position: absolute; inset: 0; width: 100%; opacity: .55; mix-blend-mode: multiply; }
.pickable { pointer-events: auto; cursor: pointer; }
</style>
