<script setup>
/**
 * 左栏里的一个产品：标题 + 覆盖率 + 件名树。
 *
 * 为什么单独拆成组件：
 *
 * 以前这一整块写在 `RouteView` 的模板里，十个产品共用一个 render 函数。
 * 展开/折叠一个部件时，Vue 会把**整棵树**重新 diff 一遍 —— 现场 481 个件、
 * 展开 393 行，一次点击就是 60~77ms 的渲染任务（老机器上 4 倍更慢），
 * 手感就是「点一下要顿一下」。
 *
 * 拆开之后，`collapsed` 里只有它自己的 key 被读，点击只让这一个产品重渲染，
 * 其它产品连 render 都不进。代价是每行数据的计算搬进来了，收益是常数级。
 *
 * 另外：**这一层不许出现任何 Element Plus 组件**。
 * 每行挂一个 `el-tag`，393 行就是 393 个组件实例；展开一个 100 行的产品要当场
 * 建 100 个。`<component :is="'ArrowDown'">` 更贵 —— 每行都要走一次字符串解析
 * 和全局组件查找。这些标签本身只是带颜色的文字和一个三角，用 span + CSS 就够，
 * 组件化在这里纯是负担。
 */
import { computed } from 'vue'

const props = defineProps({
  product: { type: Object, required: true },
  parts: { type: Array, default: () => [] },
  collapsed: { type: Object, default: () => ({}) },
  open: { type: Boolean, default: false },
  activePart: { type: String, default: '' },
  activeProduct: { type: String, default: '' }
})

const emit = defineEmits(['toggle-product', 'toggle-node', 'select', 'task', 'copies', 'unbuilt'])

const nodeKey = (part) => `${props.product.name}::${part}`

// 状态标签的文案与配色在这里一次算完。模板里用四个 v-if 分支去挑 el-tag
// 的 type，等于让每个节点都过一遍组件解析，没必要。
const STATE_TEXT = { draft: '草稿', archived: '归档', none: '未建' }

function decorate(x) {
  const state = x.state || 'none'
  const parts = []
  if (Number(x.qty_per_unit) !== 1 && x.qty_per_unit != null) parts.push(`×${x.qty_per_unit}`)
  if (x.plan_qty != null) parts.push(`计划 ${x.plan_qty}`)
  return {
    x,
    state,
    stateText: state === 'released' ? `v${x.version}` : STATE_TEXT[state] || '未建',
    qtyText: parts.join(' '),
    metaText: state !== 'none' ? `${x.step_count} 道 · ${x.unit_hours}h` : ''
  }
}

// 把后端的「结构序平铺列表」还原成可渲染的层级行：
// 后端已按 总装 → 部件 → 其子件 排好序，这里只算缩进与折叠，
// 不在前端重排 —— 排序规则只能有一处，否则两边会慢慢跑偏。
const rows = computed(() => {
  const list = props.parts || []
  const names = new Set(list.map((x) => x.part))
  const kids = new Map()
  for (const x of list) {
    // 父件被筛掉（搜索/只看未建）时把它当顶层显示，否则整枝会消失
    const key = x.parent && names.has(x.parent) ? x.parent : ''
    if (!kids.has(key)) kids.set(key, [])
    kids.get(key).push(x)
  }
  const out = []
  const walk = (parent, depth) => {
    for (const x of kids.get(parent) || []) {
      const open = !props.collapsed[nodeKey(x.part)]
      out.push({ ...decorate(x), depth, hasChildren: (kids.get(x.part) || []).length > 0, open })
      if (open) walk(x.part, depth + 1)
    }
  }
  walk('', 0)
  return out
})
</script>

<template>
  <div class="tree-product">
    <div class="tree-product-head" @click="emit('toggle-product', product.name)">
      <span class="tree-arrow" :class="{ open }"></span>
      <span class="tree-name">{{ product.name }}</span>
      <span v-if="product.unspecified" class="tstate s-none">待补产品</span>
      <span class="tree-badge">{{ product.part_count }}</span>
    </div>
    <div class="tree-product-meta">
      <div class="bar-track tree-bar">
        <div class="bar-fill" :style="{ width: Math.min(100, product.coverage_pct) + '%' }"></div>
      </div>
      <span class="tree-counts">
        <span v-if="product.released_count" class="ok">已发 {{ product.released_count }}</span>
        <span v-if="product.draft_count" class="warn">草 {{ product.draft_count }}</span>
        <span v-if="product.archived_count" class="muted">归 {{ product.archived_count }}</span>
        <button
          v-if="product.unbuilt_count"
          class="tree-link"
          title="只看该产品未建的件名"
          @click.stop="emit('unbuilt', product)"
        >
          待建 {{ product.unbuilt_count }}
        </button>
        <button
          v-if="product.task_count"
          class="tree-link task-link"
          title="该产品下的工艺被多少个订单引用"
          @click.stop="emit('task', product, null)"
        >
          引用 {{ product.task_count }}
        </button>
        <span v-if="product.pending_count" class="pending-chip" title="该产品下还有引用没确认升版">
          待确认 {{ product.pending_count }}
        </span>
        <span
          v-if="!product.released_count && !product.draft_count && !product.archived_count && !product.unbuilt_count && !product.task_count"
          class="muted"
        >—</span>
      </span>
    </div>
    <!-- 用 v-if 不用 v-show：折叠的产品不该留在 DOM 里。全部 541 行常驻时，
         任何一次展开/折叠都要让浏览器对 5000+ 节点重算样式与布局。 -->
    <div v-if="open" class="tree-parts">
      <div
        v-for="row in rows"
        :key="row.x.part"
        class="tree-part"
        :class="[
          { active: activePart === row.x.part && activeProduct === product.name },
          `lvl${Math.min(row.depth, 2)}`
        ]"
        :style="row.depth ? { '--d': row.depth } : null"
        @click="emit('select', product.name, row.x)"
      >
        <!-- 缩进即层级：一根定宽块用重复渐变画出每级竖线。
             一层一个 span 的话，393 行要几千个空节点，全部参与布局。 -->
        <span v-if="row.depth" class="tree-guides"></span>
        <button
          v-if="row.hasChildren"
          class="tree-caret"
          :class="{ open: row.open }"
          :title="row.open ? '收起下级件' : '展开下级件'"
          @click.stop="emit('toggle-node', product.name, row.x.part)"
        ></button>
        <span v-else class="tree-caret ghost"></span>
        <span
          class="tree-name"
          :title="row.x.parent
            ? `装在「${row.x.parent}」· 结构第 ${(row.x.level ?? row.depth) + 1} 层`
            : '整机（顶层）'"
        >{{ row.x.part }}</span>
        <span class="tstate" :class="`s-${row.state}`">{{ row.stateText }}</span>
        <!-- 结构数量：装一台用几件、这一批做几件。
             用量=1 是绝大多数，那一节就不显示了，否则每行都挂个 ×1 太吵 -->
        <span
          v-if="row.qtyText"
          class="tree-qty"
          :title="`装一台上级用 ${row.x.qty_per_unit} 件 · 本批计划 ${row.x.plan_qty ?? '—'} 件（在「结构」里改）`"
        >{{ row.qtyText }}</span>
        <span v-if="row.metaText" class="tree-part-meta muted">{{ row.metaText }}</span>
        <button
          v-if="row.x.task_count"
          class="tree-link task-chip"
          :class="{ 'task-chip-warn': row.x.pending_count }"
          :title="row.x.pending_count
            ? `这个零件有 ${row.x.pending_count} 个引用还没确认升版，点开逐条处理`
            : '这个零件被哪些订单引用、引用还是副本'"
          @click.stop="emit('task', product, row.x)"
        >
          {{ row.x.task_count }} 单<span v-if="row.x.pending_count"> · 待确认 {{ row.x.pending_count }}</span>
        </button>
        <button
          v-if="row.x.copy_count"
          class="tree-link copy-chip"
          title="该件名有本单副本：只属于某个订单，与基线并列管理"
          @click.stop="emit('copies', row.x.part, product.raw_name)"
        >
          副本 {{ row.x.copy_count }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.tree-product {
  margin-bottom: 2px;
  padding-bottom: 4px;
  border-bottom: 1px solid #f2f4f8;
}
.tree-product:last-child { border-bottom: none; }

.tree-product-head {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 4px 2px;
  cursor: pointer;
  font-size: 13px;
  border-radius: 5px;
}
.tree-product-head:hover { background: #f2f5f9; }

/* 产品行第二行：覆盖率进度条 + 各类件数 */
.tree-product-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 4px 4px 22px;
}

.tree-bar { flex: 0 0 54px; height: 6px; }

.tree-counts {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  color: #9aa3b2;
}
.tree-counts .ok { color: #3b6d11; }
.tree-counts .warn { color: #d9822b; }

/* 「待建 N」可点：点了就只看该产品未建的件名 */
.tree-link {
  border: none;
  background: none;
  padding: 0;
  font: inherit;
  font-size: 11px;
  color: #1f6fd0;
  cursor: pointer;
  text-decoration: underline dotted;
}
.tree-link:hover { color: #14539f; }

/* 任务标记：产品行给数量、件名行给单数，都用同一个克制的小链接 */
.task-link { color: #7a5cc4; }
.task-chip { margin-left: auto; font-size: 10.5px; }
/* 引用里有人没确认升版：树上也要红起来，别等点进任务页才发现 */
.task-chip-warn { color: #c0392b; font-weight: 600; }
.task-chip-warn:hover { color: #a52a1c; }
.pending-chip {
  color: #c0392b;
  background: #fdf3f2;
  border: 1px solid #f0c2bc;
  border-radius: 10px;
  padding: 0 6px;
  font-size: 10.5px;
  line-height: 15px;
}

.tree-arrow,
.tree-caret::before {
  flex: 0 0 auto;
  width: 0;
  height: 0;
  border-left: 4.5px solid currentColor;
  border-top: 3.5px solid transparent;
  border-bottom: 3.5px solid transparent;
}
/* 产品行的三角是 span 本身，不是伪元素，必须改成 inline-block 才吃宽高 */
.tree-arrow { display: inline-block; align-self: center; }
/* 件名行的三角是伪元素，得显式给它一个 content 才会生成 */
.tree-caret::before { content: ''; }
/* 展开态：三角朝下。用边框画，不引图标组件 —— 每行一次全局组件解析太贵 */
.tree-arrow.open,
.tree-caret.open::before {
  border-left: 3.5px solid transparent;
  border-right: 3.5px solid transparent;
  border-top: 4.5px solid currentColor;
  border-bottom: 0;
}

.tree-name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 状态标签：视觉上等价 el-tag(size=small, effect=plain)，
   但没有组件实例。393 行 × 1 个 el-tag 是这一层最重的一笔开销。 */
.tstate {
  flex: 0 0 auto;
  font-size: 11px;
  line-height: 16px;
  padding: 0 5px;
  border-radius: 4px;
  border: 1px solid;
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}
.s-released { color: #3b6d11; background: #f0f9eb; border-color: #d5ecc0; }
.s-draft    { color: #b8762c; background: #fdf6ec; border-color: #f5dab1; }
.s-archived { color: #85898f; background: #f4f4f5; border-color: #e2e3e6; }
.s-none     { color: #85898f; background: #f4f4f5; border-color: #e2e3e6; }

/* 件名行的规模提示：「3 道 · 1.5h」 */
.tree-part-meta {
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  margin-left: auto;
  padding-left: 6px;
  white-space: nowrap;
}

/* 结构数量：用量 ×N、计划 N 件。颜色压淡，别和工时数字抢注意力 */
.tree-qty {
  font-size: 10.5px;
  color: #8a93a3;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
  padding-left: 2px;
}
.tree-part.active .tree-qty { color: #5b7fae; }

.tree-badge {
  font-size: 11px;
  color: #9aa3b2;
  background: #f0f3f7;
  border-radius: 8px;
  padding: 0 6px;
}

.tree-parts { padding-left: 8px; }

/* 层级竖线：一块定宽区域用重复渐变画出每级一根，宽度由 --d（层级）决定。
   以前是「一层一个 span」，393 行 × 平均 2 层 = 近千个空节点参与布局。 */
.tree-guides {
  align-self: stretch;
  flex: 0 0 auto;
  width: calc(var(--d) * 12px);
  margin-right: 1px;
  background-image: repeating-linear-gradient(to right, #e6eaf1 0 1px, transparent 1px 12px);
}

.tree-caret {
  flex: 0 0 14px;
  width: 14px;
  height: 14px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  border: none;
  background: none;
  color: #9aa3b2;
  cursor: pointer;
  border-radius: 3px;
}
.tree-caret:hover { color: #1f6fd0; background: #e8f1fd; }
/* 没有下级的行占一个等宽空位，件名左边界才对得齐 */
.tree-caret.ghost { cursor: default; background: none; }
.tree-caret.ghost::before { display: none; }

.tree-part {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 6px;
  cursor: pointer;
  font-size: 12.5px;
  color: #4b5565;
  border-radius: 5px;
}
.tree-part:hover { background: #f2f5f9; }
.tree-part.active { background: #e8f1fd; color: #1f6fd0; }

/* 部件（第 1 层）是分组的头，颜色压深一点；再往下的子件用常规灰 */
.tree-part.lvl1 .tree-name { font-weight: 600; color: #2b3444; }
.tree-part.lvl1.active .tree-name { color: #1f6fd0; }
.tree-part.lvl2 .tree-name { color: #5b6675; }

.copy-chip { color: #c26a12; font-size: 10.5px; }
.copy-chip:hover { color: #a3540a; }
</style>
