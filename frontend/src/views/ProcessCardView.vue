<template>
  <div class="page pc-page">
    <div class="page-head pc-noprint">
      <span class="page-title">工艺卡</span>
      <span class="page-sub">
        工艺卡＝<b>加工路线单</b>：工序与定额预印，操作者与检验结果留白；
        只印<b>已发布</b>组合，编号按层级（01 → 01-01）。
      </span>
      <div class="spacer"></div>
      <el-button size="small" @click="router.push('/combos')">去工时组合</el-button>
      <el-button size="small" @click="load">刷新</el-button>
      <el-button size="small" :disabled="!cards.length" @click="openPdfBatch">
        下载 PDF{{ cards.length ? `（${pageTotal} 页）` : '' }}{{ mode === 'product' ? ' · 无报工码' : '' }}
      </el-button>
    </div>

    <div class="pc-toolbar pc-noprint">
      <el-radio-group v-model="mode" size="small" @change="onModeChange">
        <el-radio-button value="product">按产品</el-radio-button>
        <el-radio-button value="task">按生产任务</el-radio-button>
      </el-radio-group>

      <template v-if="mode === 'product'">
        <el-select
          v-model="product"
          size="small"
          filterable
          style="width: 210px"
          placeholder="选择产品"
          @change="onProductChange"
        >
          <el-option
            v-for="p in products"
            :key="p.product"
            :value="p.product"
            :label="p.product || '(未指定产品)'"
          >
            <span class="pc-opt-name">{{ p.product || '(未指定产品)' }}</span>
            <span class="pc-opt-meta">{{ p.released }} 已发 · {{ p.hours }}h</span>
          </el-option>
        </el-select>
      </template>
      <template v-else>
        <el-select
          v-model="taskId"
          size="small"
          filterable
          style="width: 320px"
          placeholder="选择生产任务"
          @change="onTaskChange"
        >
          <el-option v-for="t in tasks" :key="t.id" :value="t.id" :label="t.task_no">
            <span class="pc-opt-name">{{ t.task_no }}</span>
            <span class="pc-opt-meta">{{ t.released }} 张卡 · {{ t.qty }} 台</span>
          </el-option>
        </el-select>
        <el-tag v-if="curTask && curTask.title" size="small" type="info" effect="plain">
          {{ curTask.title }}
        </el-tag>
      </template>

      <el-divider direction="vertical" />
      <el-tag v-if="part" size="small" type="primary" effect="plain" closable @close="selectPart('')">
        只看 {{ part }}
      </el-tag>
      <el-checkbox v-model="withChildren" size="small" :disabled="!part" @change="load">
        连子件一起
      </el-checkbox>
      <div class="spacer"></div>
      <span class="muted pc-print-hint">
        {{ mode === 'task'
          ? '点一行看工序明细；预览与出纸的右上角都带二维码和单号'
          : '按产品印的是「工艺标准」：不带报工码、不产生单号。要扫码报工请切到「按生产任务」' }}
      </span>
    </div>

    <div class="pc-body" v-loading="loading">
      <!-- 左：产品 → 部件层级 / 任务 → 本单的卡，与右侧一一对应 -->
      <aside class="card pc-tree pc-noprint">
        <div class="section-title">
          {{ mode === 'task' ? '本单的卡' : '部件层级' }}
          <span class="muted">出卡 {{ summary.printed ?? 0 }} 张</span>
          <!-- 把「结构里有、但出不了卡」单独说清楚：否则人会以为漏印。
               文案要短：左栏只有 268px，写「另有 N 件未建工艺」会把标题挤成两行 -->
          <span
            v-if="summary.unbuilt"
            class="muted pc-unbuilt-hint"
            :title="`装配结构里还有 ${summary.unbuilt} 件没有已发布的工艺，所以不出卡`"
          >· {{ summary.unbuilt }} 件未建</span>
        </div>
        <el-input v-model="kw" size="small" placeholder="搜索件名 / 编号" clearable style="margin-bottom: 8px" />
        <div class="pc-tree-body">
          <div class="pc-tree-all" :class="{ active: !part }" @click="selectPart('')">
            <span class="pc-tree-code">{{ mode === 'task' ? '整单' : '全部' }}</span>
            <span class="pc-tree-name">{{ mode === 'task' ? (curTask?.task_no || '—') : (product || '—') }}</span>
            <span class="pc-tree-hours mono">{{ hours(summary.batch_hours) }}</span>
          </div>
          <div
            v-for="n in visibleNodes"
            :key="n.key"
            class="pc-tree-row"
            :class="{ active: part === n.part, lv0: n.level === 0, unbuilt: isUnbuilt(n) }"
            :style="{ paddingLeft: 6 + n.depth * 13 + 'px' }"
            @click="onNodeClick(n)"
          >
            <span class="pc-tree-code mono">{{ n.code || '—' }}</span>
            <span class="pc-tree-name">
              {{ n.part }}
              <!-- 结构里有、工艺没发布：显示出来是为了让「刚加的件去哪了」有答案，
                   但它不会是卡片，所以用灰标 —— 顺带区分「没编」和「编了没发」 -->
              <span
                v-if="isUnbuilt(n)"
                class="pc-tree-unbuilt"
                :title="unbuiltTip(n)"
              >{{ statusText(n.status) }}</span>
              <span v-if="n.sheet_count" class="pc-tree-report" title="已经有加工路线单录进来了">已报{{ n.sheet_count }}</span>
            </span>
            <span class="pc-tree-hours mono">{{ isUnbuilt(n) ? '—' : hours(n.own_hours) }}</span>
          </div>
          <el-empty v-if="!visibleNodes.length" description="没有已发布工艺" :image-size="54" />
        </div>
      </aside>

      <!-- 右：这批单子有哪些 → 选中那张的基本信息 + 工序明细 -->
      <div class="pc-main">
        <div class="pc-strip pc-noprint">
          <span class="sb-item"><span class="muted">卡片</span><b class="mono">{{ summary.printed ?? 0 }}</b></span>
          <span class="sb-item"><span class="muted">页数</span><b class="mono">{{ pageTotal }}</b></span>
          <span class="sb-item"><span class="muted">工序</span><b class="mono">{{ summary.step_count ?? 0 }}</b></span>
          <span
            class="sb-item"
            :title="mode === 'task' ? '本单件数 × 单件定额 + 准备工时' : '按单台口径的定额合计'"
          >
            <span class="muted">{{ mode === 'task' ? '本单定额' : '单台定额' }}</span>
            <b class="mono">{{ hours(summary.batch_hours) }}</b>
          </span>
          <span class="sb-item" title="本层 + 所有下级（按每台用量加权）">
            <span class="muted">{{ mode === 'task' ? '单台含子件' : '含子件' }}</span>
            <b class="mono">{{ hours(summary.subtotal_hours) }}</b>
          </span>
          <span class="sb-item" title="整个产品的整机工时合计">
            <span class="muted">产品单台</span><b class="mono">{{ hours(summary.product_hours) }}</b>
          </span>
          <span class="sb-sep"></span>
          <span
            v-if="summary.sheet_count"
            class="sb-item ok"
            title="这些件名已经有加工路线单录进来了：同样是「产品×件名」，说明活已经干过"
          >
            <span class="muted">已录单据</span>
            <b class="mono">{{ summary.sheet_count }} 张 / {{ hours(summary.sheet_hours) }}</b>
          </span>
          <span class="sb-item" :class="{ warn: summary.draft }" title="未发布的组合不会出现在工艺卡里">
            <span class="muted">未发布</span><b class="mono">{{ summary.draft ?? 0 }}</b>
          </span>
          <span
            v-if="summary.archived"
            class="sb-item"
            title="已停用（归档）的组合不出工艺卡；要印先在工时组合台账里启用"
          >
            <span class="muted">已停用</span><b class="mono">{{ summary.archived }}</b>
          </span>
          <span class="sb-item" :class="{ danger: summary.missing }" title="该有的卡印不出来：缺工艺或没发布">
            <span class="muted">缺卡</span><b class="mono">{{ summary.missing ?? 0 }}</b>
          </span>
          <div class="spacer"></div>
          <span class="sb-note muted">{{ printedAt }}</span>
        </div>

        <div class="pc-empty" v-if="!cards.length">
          <el-empty :image-size="72" :description="emptyHint">
            <el-button size="small" @click="router.push('/combos')">去工时组合发布</el-button>
          </el-empty>
        </div>

        <template v-else>
          <!-- 一、这一批有哪几张单：一屏能扫完，不用一张张翻纸 -->
          <section class="card pc-list">
            <div class="section-title">
              加工路线单
              <span class="muted">{{ cards.length }} 张</span>
              <div class="spacer"></div>
              <span class="muted pc-list-hint">点一行看工序明细</span>
            </div>
            <!-- 不能用 row-key：卡片数据里有 children（下级件），el-table 会当成
                 树形表格，把「刀具」折到「整机装配」底下藏起来 -->
            <el-table
              :data="cards"
              size="small"
              :row-class-name="rowCls"
              class="pc-table"
              :max-height="listMaxH"
              @row-click="pick"
              @row-dblclick="openPreview"
            >
              <el-table-column label="件号" min-width="96">
                <template #default="{ row }">
                  <span class="mono strong">{{ row.code }}</span>
                </template>
              </el-table-column>
              <el-table-column label="件名" min-width="108" show-overflow-tooltip>
                <template #default="{ row }">
                  <span class="pc-row-part">{{ row.part }}</span>
                  <span v-if="row.sheets.count" class="pc-tree-report" title="已经有加工路线单录进来了">
                    已报{{ row.sheets.count }}
                  </span>
                </template>
              </el-table-column>
              <el-table-column label="类别" min-width="116">
                <template #default="{ row }">
                  <!-- 类别 + 装在 挤在一个格里：必须 nowrap，否则「装在 01-02」会被折断成两行 -->
                  <span class="pc-row-cat">
                    <el-tag size="small" :type="row.level === 0 ? 'danger' : 'info'" effect="plain">
                      {{ row.level_name }}
                    </el-tag>
                    <span v-if="row.parent_code" class="muted pc-row-x" :title="`装在 ${row.parent_code}`">装在 {{ row.parent_code }}</span>
                  </span>
                </template>
              </el-table-column>
              <el-table-column label="版本" width="42">
                <template #default="{ row }"><span class="mono">v{{ row.version }}</span></template>
              </el-table-column>
              <el-table-column label="工序" width="42" align="right">
                <template #default="{ row }"><span class="mono">{{ row.step_count }}</span></template>
              </el-table-column>
              <el-table-column label="本批件数" width="72" align="right">
                <template #default="{ row }"><span class="mono">{{ row.print_qty }}</span></template>
              </el-table-column>
              <el-table-column label="单件" width="64" align="right">
                <template #default="{ row }"><span class="mono">{{ hours(row.own_hours) }}</span></template>
              </el-table-column>
              <el-table-column label="本批定额" width="76" align="right">
                <template #default="{ row }"><span class="mono strong">{{ hours(row.batch_hours) }}</span></template>
              </el-table-column>
              <el-table-column label="操作" min-width="72" align="right">
                <template #default="{ row }">
                  <el-button link type="primary" size="small" @click.stop="openPreview(row)">
                    预览打印
                  </el-button>
                </template>
              </el-table-column>
            </el-table>
          </section>

          <!-- 二、选中那张单：基本信息 + 工序明细 -->
          <section class="card pc-detail" v-if="cur">
            <div class="section-title">
              <span class="mono pc-detail-code">{{ cur.code }}</span>
              <span class="pc-detail-part">{{ cur.part }}</span>
              <el-tag size="small" :type="cur.level === 0 ? 'danger' : 'info'" effect="plain">
                {{ cur.level_name }}
              </el-tag>
              <span v-if="cur.parent_code" class="muted">
                装在 <b class="mono">{{ cur.parent_code }}</b> {{ cur.parent_part }}
              </span>
              <div class="spacer"></div>
              <el-button size="small" @click="openPdfOne(cur)">下载 PDF</el-button>
              <el-button size="small" type="primary" @click="openPreview(cur)">预览打印</el-button>
            </div>

            <dl class="pcv-info">
              <div><dt>产品</dt><dd>{{ cur.product || '—' }}</dd></div>
              <div><dt>版本</dt><dd class="mono">v{{ cur.version }}</dd></div>
              <div><dt>工序</dt><dd class="mono">{{ cur.step_count }} 道</dd></div>
              <div><dt>本批件数</dt><dd class="mono">{{ cur.print_qty }}</dd></div>
              <div><dt>每台用量</dt><dd class="mono">{{ cur.qty_per_unit }}</dd></div>
              <div><dt>件数口径</dt><dd>{{ qtyLabel(cur.qty_src) }}</dd></div>
              <div><dt>单件定额</dt><dd class="mono">{{ hours(cur.own_hours) }}</dd></div>
              <div><dt>准备工时</dt><dd class="mono">{{ hours(cur.setup_hours) }}</dd></div>
              <div><dt>本批定额</dt><dd class="mono strong">{{ hours(cur.batch_hours) }}</dd></div>
              <div><dt>含子件</dt><dd class="mono">{{ hours(cur.subtotal_hours) }}</dd></div>
              <div><dt>产品单台</dt><dd class="mono">{{ hours(cur.product_hours) }} <span class="muted">（占 {{ cur.share_pct }}%）</span></dd></div>
              <div><dt>下级</dt><dd>{{ cur.child_count ? `${cur.child_count} 件先做完` : '—' }}</dd></div>
              <div>
                <dt>已录加工路线单</dt>
                <dd>
                  <a
                    v-if="cur.sheets.ids?.length"
                    class="pc-link"
                    @click="openSheet(cur.sheets.ids[0])"
                  >
                    {{ cur.sheets.count }} 张 / {{ hours(cur.sheets.hours) }}
                  </a>
                  <span v-else-if="cur.sheets.count">
                    {{ cur.sheets.count }} 张 / {{ hours(cur.sheets.hours) }}
                    <span class="muted">（扫码报工 {{ cur.sheets.last_form_no }}）</span>
                  </span>
                  <span v-else class="muted">还没有</span>
                </dd>
              </div>
              <div><dt>发布</dt><dd>{{ cur.published_by || '—' }} <span class="muted">{{ (cur.published_at || '').slice(0, 10) }}</span></dd></div>
              <div v-if="cur.material"><dt>材料</dt><dd>{{ cur.material }}</dd></div>
              <div v-if="cur.remark" class="pcv-wide"><dt>备注</dt><dd>{{ cur.remark }}</dd></div>
            </dl>

            <div class="pc-steps-head">
              工序明细
              <span class="muted">{{ cur.steps.length }} 道 · 单件合计 {{ hours(cur.own_hours) }}</span>
            </div>
            <!-- 工序明细用原生表格：列少的时候 el-table 的 min-width 列会独占剩余宽度，
                 工序名和工时之间会拉开一大片空白；原生表格按内容分配更紧凑，
                 也方便在表尾加一行合计。
                 列口径对着《加工路线单》纸面走：序号/工序等级/数量/单件/准备/检验/备注，
                 纸上有的一定要在页面上看得到，否则拿页面核对完、印出来对不上。 -->
            <table class="pc-steps">
              <thead>
                <tr>
                  <th class="c" style="width: 6%">序</th>
                  <th style="width: 24%">工序等级</th>
                  <th class="r" style="width: 9%">数量</th>
                  <th class="r" style="width: 12%">单件定额</th>
                  <th class="r" style="width: 12%">准备工时</th>
                  <th v-if="hasCol('work_center')" style="width: 12%">工作中心 / 工种</th>
                  <th class="c" style="width: 9%">检验</th>
                  <th style="width: 16%">备注</th>
                  <th v-if="hasCol('sop')" style="width: 18%">作业要求</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(s, i) in cur.steps" :key="s.id">
                  <td class="c mono muted">{{ i + 1 }}</td>
                  <td>
                    <span :class="{ 'pc-key': s.is_key }">{{ s.process_name }}</span>
                    <span v-if="s.is_key" class="pc-mark" title="关键工序">★</span>
                    <span v-if="s.not_in_dict" class="pc-mark warn" title="疑似非工序，建议改掉">?</span>
                  </td>
                  <!-- 数量 = 本批件数：一张卡上每道工序都是同一件活，纸面这一格车间不填，
                       这里显示的是「这批要做几件」，乘上单件定额才是纸上的合计 -->
                  <td class="r mono muted">{{ cur.print_qty }}</td>
                  <td class="r mono">{{ hours(s.unit_hours) }}</td>
                  <td class="r mono">{{ Number(s.setup_hours) ? hours(s.setup_hours) : '—' }}</td>
                  <td v-if="hasCol('work_center')">{{ s.work_center || '—' }}</td>
                  <td class="c">
                    <span v-if="s.inspect_required" class="pc-insp" title="该道工序需检验，纸面印在备注格">需检</span>
                    <span v-else class="muted">—</span>
                  </td>
                  <td class="pc-note">{{ stepNote(s) || '—' }}</td>
                  <td v-if="hasCol('sop')">{{ s.sop || '—' }}</td>
                </tr>
              </tbody>
              <tfoot>
                <tr>
                  <td colspan="3" class="r muted">
                    合计（{{ cur.steps.length }} 道 · 本批 {{ cur.print_qty }} 件）
                  </td>
                  <td class="r mono strong">{{ hours(cur.own_hours) }}</td>
                  <td class="r mono">{{ Number(cur.setup_hours) ? hours(cur.setup_hours) : '—' }}</td>
                  <td v-if="hasCol('work_center')"></td>
                  <td></td>
                  <td></td>
                  <td v-if="hasCol('sop')"></td>
                </tr>
              </tfoot>
            </table>
          </section>
        </template>

        <div v-if="missing.length" class="pc-missing">
          <div class="section-title">这些件名不印卡</div>
          <div v-for="(m, i) in missing" :key="i" class="pc-missing-one">
            <span class="mono">{{ m.product || '（无产品）' }} / {{ m.part }}</span>
            <span class="muted">{{ m.reason }}</span>
          </div>
        </div>
      </div>
    </div>

    <!-- 预览打印：整张单子按 A4 横向比例缩放显示，确认无误再出纸 -->
    <el-dialog
      v-model="pv.open"
      :title="pv.card ? `加工路线单预览 · ${pv.card.code} ${pv.card.part}` : '加工路线单预览'"
      width="min(1320px, 94vw)"
      top="3vh"
      destroy-on-close
      class="pc-pv-dialog"
      @closed="clearPv"
    >
      <div class="pv-meta">
        <span class="mono">{{ pv.card?.code }}</span>
        <span>{{ pv.card?.part }}</span>
        <el-tag v-if="pv.card" size="small" :type="pv.card.level === 0 ? 'danger' : 'info'" effect="plain">
          {{ pv.card.level_name }}
        </el-tag>
        <span v-if="pv.card?.parent_code" class="muted">装在 {{ pv.card.parent_code }}</span>
        <span class="muted">v{{ pv.card?.version }} · {{ pv.card?.step_count }} 道工序 · 本批 {{ pv.card?.print_qty }} 件</span>
        <div class="spacer"></div>
        <!-- 预览与出纸是同一张图：预览里看到码，纸上就有码。
             按产品印的是标准工艺（没有生产任务），不建单也不印码 -->
        <span v-if="mode === 'task' && taskId" class="muted pv-qr-hint">
          右上角二维码与单号已生成本单，可直接扫码报工
        </span>
        <span v-else class="muted pv-qr-hint">按产品印标准工艺，不生成报工码</span>
        <span class="muted">A4 横向 1:1 出纸，共 {{ pv.card?.pages || 1 }} 页</span>
      </div>

      <div class="pv-body" v-loading="pv.loading">
        <div v-for="(u, i) in pv.urls" :key="i" class="pv-paper">
          <img :src="u" :alt="`${pv.card?.code} ${pv.card?.part} 第 ${i + 1} 页`" />
          <span v-if="(pv.card?.pages || 1) > 1" class="pv-page-no">第 {{ i + 1 }} / {{ pv.card.pages }} 页</span>
        </div>
        <el-empty v-if="!pv.loading && !pv.urls.length" :image-size="64" description="版式渲染失败，无法预览" />
      </div>

      <template #footer>
        <span class="pv-foot-hint muted">
          纸上的操作者 / 加工结果 / 检查员留白，由车间手写
        </span>
        <el-button @click="pv.open = false">关闭</el-button>
        <el-button :disabled="!pv.urls.length" @click="openPdfOne(pv.card)">下载 PDF</el-button>
        <el-button type="primary" :disabled="!pv.urls.length" @click="printPv">
          打印{{ pv.card ? `（${pv.card.pages || 1} 页）` : '' }}
        </el-button>
      </template>
    </el-dialog>

    <!-- 打印专用副本：打印时只留它，页面上其它东西一律不出现 -->
    <Teleport to="body">
      <div class="pc-print-only">
        <img v-for="(u, i) in printUrls" :key="i" :src="u" alt="" />
      </div>
    </Teleport>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, onUnmounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { api } from '../api'

const route = useRoute()
const router = useRouter()

const mode = ref('product') // product | task
const product = ref('')
const taskId = ref(null)
const part = ref('')
const withChildren = ref(false)
const kw = ref('')
const loading = ref(false)
const res = ref({})
const cur = ref(null) // 右侧正在看的那张单

const cards = computed(() => res.value.cards || [])
const tree = computed(() => res.value.tree || [])
const products = computed(() => res.value.products || [])
const tasks = computed(() => res.value.tasks || [])
const summary = computed(() => res.value.summary || {})
const curTask = computed(() => res.value.task || null)
const missing = computed(() => res.value.missing || [])
const printedAt = computed(() => res.value.printed_at || '')
const pageTotal = computed(() => summary.value.pages || 0)
// 清单最多占半屏：再长就把工序明细挤没了
const listMaxH = computed(() => (cards.value.length > 8 ? 320 : undefined))

// 左栏与右侧必须一一对应：把层级树拍平只为了加缩进与搜索，
// 不重新组织父子关系（否则「树上有、右边没有」这种自相矛盾又回来了）
const flatTree = computed(() => {
  const out = []
  const walk = (nodes, depth) => {
    for (const n of nodes || []) {
      out.push({ ...n, depth, key: `${n.code}-${n.combo_id ?? n.part}` })
      walk(n.children, depth + 1)
    }
  }
  walk(tree.value, 0)
  return out
})

const visibleNodes = computed(() => {
  const k = kw.value.trim()
  if (!k) return flatTree.value
  return flatTree.value.filter((n) => (n.part || '').includes(k) || (n.code || '').includes(k))
})

const emptyHint = computed(() => {
  if (mode.value === 'task') {
    return curTask.value
      ? `「${curTask.value.task_no}」还没有已发布的工艺组合，工艺卡只印已发布的`
      : '先选一个生产任务'
  }
  return product.value
    ? `「${product.value}」还没有已发布的工艺组合，工艺卡只印已发布的`
    : '还没有任何已发布的工艺组合'
})

// 工时显示：库里是 4 位小数（32.0 / 53.1667），纸上写 32h / 53.17h 就够
function hours(v) {
  const n = Number(v || 0)
  if (!n) return '0h'
  return `${Number(n.toFixed(2))}h`
}

function qtyLabel(src) {
  if (src === 'route') return '本单件名数量'
  if (src === 'task') return '投产台数 × 每台用量'
  return '每台用量'
}

function pick(row) {
  if (row) cur.value = row
}

// 选中行自己上色：不用 el-table 的 current-row，那套要 row-key，
// 而 row-key 会让带 children 的数据变成树形表
// 这张卡的所有工序都没有这一项时，整列不显示：空列白占宽度，工序名就没地方写了
function hasCol(key) {
  return (cur.value?.steps || []).some((s) => Boolean(String(s[key] || '').trim()))
}

// 备注列 = 纸面那一格会印什么：外协优先，其次是工序自己的备注。
// 「疑似非工序」是系统打的核对标记（工序名后面已有 ? 号），整条都不是人手写的，
// 不能当备注显示出去 —— 一段 30 字的系统提示会把 16% 宽的列撑成两行。
function stepNote(s) {
  const parts = []
  const raw = String(s.remark || '').trim()
  const hand = raw && !raw.startsWith('疑似非工序') ? raw : ''
  if ((s.op_type || '') === 'outsource') {
    parts.push('外协')
    // 备注自己已经写了「外协…」就不重复印一遍
    if (hand && !hand.startsWith('外协')) parts.push(hand)
  } else if (hand) {
    parts.push(hand)
  }
  return parts.join(' · ')
}

function rowCls({ row }) {
  return cur.value && row.combo_id === cur.value.combo_id ? 'pc-row-cur' : ''
}

// 占位件的三种情况要分开说：没编 / 编了没发 / 停用了。混成一句「未建」，
// 工艺员会去重编一个已经有草稿的件。
function isUnbuilt(n) {
  return n.has_route === false
}

function statusText(s) {
  return { none: '未建', draft: '草稿', archived: '停用' }[s] || '未建'
}

function unbuiltTip(n) {
  if (n.status === 'draft') return '工艺还是草稿，发布后才会出卡'
  if (n.status === 'archived') return '工艺已停用，重新发布后才会出卡'
  return '装配结构里有这件，但还没编工艺，不出卡'
}

// 占位件（结构里有、工艺没发布）点不进去：没有卡可看。直接说清楚该去哪编工艺，
// 比让人点开一个空白列表再自己猜要好。
function onNodeClick(n) {
  if (isUnbuilt(n)) {
    const why =
      n.status === 'draft'
        ? '工艺还是草稿、没发布'
        : n.status === 'archived'
          ? '工艺已停用'
          : '还没编工艺'
    ElMessage.info(`「${n.part}」已经挂进装配结构，但${why}，所以出不了卡；先到「工艺及工时编排」处理`)
    return
  }
  selectPart(n.part)
}

function selectPart(name) {
  part.value = name
  // 点左栏某一件 = 「就看这一件的卡」：默认不带子件，右栏只有这一张，
  // 打印出来也就是这一张（要打整套就把「连子件一起」勾上）
  withChildren.value = false
  load()
}

function onProductChange() {
  part.value = ''
  kw.value = ''
  withChildren.value = false
  load()
}

function onTaskChange() {
  part.value = ''
  kw.value = ''
  withChildren.value = false
  load()
}

function onModeChange() {
  part.value = ''
  kw.value = ''
  withChildren.value = false
  if (mode.value === 'task' && taskId.value == null && tasks.value.length) {
    taskId.value = tasks.value[0].id
  }
  load()
}

function syncQuery() {
  const q = {}
  if (mode.value !== 'product') q.mode = mode.value
  if (mode.value === 'product' && product.value) q.product = product.value
  if (mode.value === 'task' && taskId.value) q.task = String(taskId.value)
  if (part.value) q.part = part.value
  if (mode.value !== 'product' || part.value) q.children = withChildren.value ? '1' : '0'
  router.replace({ path: '/cards', query: q })
}

async function load() {
  loading.value = true
  try {
    const params = { part: part.value || undefined, include_children: withChildren.value }
    if (mode.value === 'task') {
      if (taskId.value == null) {
        // 直接开 #/cards?mode=task（分享链接）时地址里没有任务号：先空跑一次
        // 把任务清单拿回来，自动落到第一单。否则下拉是空的、左栏也一片空白，
        // 看着像「这一单没有卡」，其实是压根没选单。
        const boot = await api.cards({})
        res.value = { ...boot, cards: [], tree: [], summary: {} }
        if (!(boot.tasks || []).length) {
          cur.value = null
          return
        }
        taskId.value = boot.tasks[0].id
      }
      params.task_id = taskId.value
    } else if (product.value) {
      params.product = product.value
    }
    const d = await api.cards(params)
    res.value = d
    // 首屏没指定产品时，后端会挑一个最值得看的，把它写回下拉，
    // 否则下拉空着、右边却有卡，看着像筛选没生效
    if (mode.value === 'product' && !product.value && d.product) product.value = d.product
    if (mode.value === 'task' && taskId.value == null && d.task) taskId.value = d.task.id
    // 选中项跟着列表走：原来那张还在就保持，没了就落到第一张
    const keep = cur.value && (d.cards || []).find((c) => c.combo_id === cur.value.combo_id)
    cur.value = keep || (d.cards || [])[0] || null
    syncQuery()
  } catch (e) {
    ElMessage.error(e?.message || '加载工艺卡失败')
  } finally {
    loading.value = false
  }
}

function openSheet(id) {
  if (id) router.push(`/review/${id}`)
}

// ---------------------------------------------------------------- 预览打印

const pv = reactive({ open: false, card: null, urls: [], loading: false })
const printUrls = ref([])

function clearPv() {
  pv.urls.forEach((u) => URL.revokeObjectURL(u))
  pv.urls = []
  pv.card = null
}

async function openPreview(card) {
  if (!card) return
  clearPv()
  pv.card = card
  pv.open = true
  pv.loading = true
  const list = []
  try {
    for (let p = 1; p <= (card.pages || 1); p++) {
      // 200dpi：预览够清晰，出纸时 297mm 宽也还站得住（150dpi 的小字会发虚）
      const params = { combo_id: card.combo_id, page: p, dpi: 200 }
      if (mode.value === 'task' && taskId.value) params.task_id = taskId.value
      list.push(await api.cardPng(params))
    }
  } catch (e) {
    ElMessage.error(`「${card.part}」渲染失败：${e?.message || '未知错误'}`)
  } finally {
    pv.urls = list
    pv.loading = false
  }
}

function decoded(urls) {
  // 图片没解码完就 print，纸上会是空白：先等每一张 onload
  return Promise.all(
    urls.map(
      (u) =>
        new Promise((done) => {
          const im = new Image()
          im.onload = im.onerror = () => done()
          im.src = u
        }),
    ),
  )
}

async function printPv() {
  if (!pv.urls.length) return
  printUrls.value = [...pv.urls]
  await decoded(printUrls.value)
  await nextTick()
  document.body.classList.add('pc-printing')
  const done = () => {
    document.body.classList.remove('pc-printing')
    window.removeEventListener('afterprint', done)
  }
  window.addEventListener('afterprint', done)
  window.print()
  // 有的浏览器不发 afterprint，兜个底
  setTimeout(done, 1500)
}

function openPdfOne(card) {
  if (!card) return
  const params = { combo_id: String(card.combo_id) }
  if (mode.value === 'task' && taskId.value) params.task_id = String(taskId.value)
  window.open(api.cardPdfUrl(params), '_blank')
}

function openPdfBatch() {
  const params = {}
  if (mode.value === 'task' && taskId.value) params.task_id = String(taskId.value)
  else {
    if (product.value) params.product = product.value
    if (part.value) params.part = part.value
    params.include_children = withChildren.value ? '1' : '0'
  }
  window.open(api.cardPdfUrl(params), '_blank')
}

onMounted(() => {
  // 地址栏里有条件就先按它来：分享出去的链接必须打开就是同一套卡
  if (route.query.mode === 'task') mode.value = 'task'
  if (route.query.product) product.value = String(route.query.product)
  if (route.query.task) taskId.value = Number(route.query.task) || null
  // 兼容 task_id 这种更顺手的写法：从别处拼链接过来很容易写成 task_id，
  // 写错了不会报错，只会安静地退回「按产品」，人还以为是权限问题
  if (!taskId.value && route.query.task_id) {
    const id = Number(route.query.task_id) || null
    if (id) {
      taskId.value = id
      mode.value = 'task'
    }
  }
  if (route.query.part) part.value = String(route.query.part)
  if (route.query.children) withChildren.value = String(route.query.children) !== '0'
  load()
})

onUnmounted(clearPv)
</script>

<style scoped>
.pc-page { padding-bottom: 30px; }

.pc-toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  padding: 10px 14px;
  background: #fff;
  border: 1px solid #e7ebf1;
  border-radius: 10px;
  margin-bottom: 12px;
}
.pc-print-hint { font-size: 12px; }
.pc-opt-name { margin-right: 10px; }
.pc-opt-meta { color: #98a0ad; font-size: 12px; }

.pc-body {
  display: grid;
  grid-template-columns: 268px minmax(0, 1fr);
  gap: 12px;
  align-items: start;
}

.pc-tree { padding: 12px 12px 8px; position: sticky; top: 8px; }
.pc-tree .section-title { margin-bottom: 8px; }
.pc-tree-body { max-height: calc(100vh - 276px); overflow: auto; }

.pc-tree-all,
.pc-tree-row {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 6px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 13px;
}
.pc-tree-all { background: #f6f8fb; margin-bottom: 4px; }
.pc-tree-row:hover { background: #f2f6fd; }
.pc-tree-row.active { background: #e8f1fe; color: #1b62d4; }
.pc-tree-row.lv0 { font-weight: 600; }
.pc-tree-code {
  flex: 0 0 auto;
  min-width: 38px;
  font-size: 11px;
  color: #8b93a1;
}
.pc-tree-row.active .pc-tree-code { color: #1b62d4; }
.pc-tree-name { flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pc-tree-hours { flex: 0 0 auto; font-size: 11px; color: #98a0ad; }
.pc-tree-report {
  margin-left: 4px;
  padding: 0 4px;
  border-radius: 3px;
  background: #e8f6ec;
  color: #237a3f;
  font-size: 10px;
}

/* 结构里有、工艺还没建的件：留在层级里但明显"不是一张卡" */
.pc-tree-row.unbuilt { color: #8b93a1; }
.pc-tree-unbuilt {
  margin-left: 4px;
  padding: 0 4px;
  border-radius: 3px;
  background: #f0f2f6;
  color: #7d8694;
  font-size: 10px;
}
.pc-unbuilt-hint { color: #a06a1f; white-space: nowrap; }
/* 左栏只有 268px：标题允许省略号但不许折行，否则「部件层级」会被折断 */
.pc-tree .section-title { flex-wrap: nowrap; white-space: nowrap; min-width: 0; }
.pc-tree .section-title > :first-child { flex: 0 0 auto; }

.pc-main { min-width: 0; display: flex; flex-direction: column; gap: 12px; }

.pc-strip {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
  padding: 9px 14px;
  background: #fff;
  border: 1px solid #e7ebf1;
  border-radius: 10px;
  font-size: 12.5px;
}
.sb-item { display: inline-flex; align-items: baseline; gap: 5px; white-space: nowrap; }
.sb-item b { font-size: 14px; }
.sb-item.warn b { color: #b7791f; }
.sb-item.danger b { color: #c0392b; }
.sb-item.ok b { color: #237a3f; }
.sb-sep { width: 1px; height: 16px; background: #e7ebf1; }
.sb-note { font-size: 12px; }

.pc-empty { background: #fff; border: 1px solid #e7ebf1; border-radius: 10px; padding: 40px 0; }

/* ---- 一、清单 ---- */
.pc-list { padding: 12px 14px 6px; }
.pc-list .section-title { margin-bottom: 8px; }
.pc-list-hint { font-size: 12px; }
.pc-row-part { font-weight: 600; }
.pc-row-x { margin-left: 4px; font-size: 11px; }
:deep(.pc-table .el-table__row) { cursor: pointer; }
:deep(.pc-table .el-table__row:hover > td) { background: #f2f6fd; }
:deep(.pc-table .pc-row-cur > td) { background: #e8f1fe !important; }
:deep(.pc-table .pc-row-cur .pc-row-part) { color: #1b62d4; }
:deep(.pc-table th.el-table__cell) { background: #f8fafc; color: #5b6472; font-weight: 600; }
:deep(.pc-table .el-table__cell) { padding: 6px 0; }
/* 清单一律单行：列宽按内容自适应，放不下就省略号裁掉。
   element 默认 .cell 是 word-break: break-all，会把「装在 01-02」折成两行、把行高撑开，
   一张 100 行的单子翻起来像两倍长 —— 所以这里统一改成 nowrap + ellipsis。 */
:deep(.pc-table .cell) {
  /* 左右内边距从默认 12 收到 8：9 列一共省出 70 多像素，
     窄窗口下能少一次横向滚动 */
  padding: 0 8px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  word-break: keep-all;
}
:deep(.pc-table .pc-row-cat) {
  display: flex;
  align-items: center;
  gap: 4px;
  min-width: 0;
  white-space: nowrap;
}
:deep(.pc-table .pc-row-cat .el-tag) { flex: 0 0 auto; }
:deep(.pc-table .pc-row-cat .pc-row-x) {
  flex: 0 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ---- 二、详情 ---- */
.pc-detail { padding: 12px 14px 14px; }
.pc-detail .section-title { margin-bottom: 10px; flex-wrap: wrap; row-gap: 6px; }
.pc-detail-code { color: #1b62d4; font-size: 15px; }
.pc-detail-part { font-size: 15px; font-weight: 600; }

.pcv-info {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 1px;
  margin: 0 0 14px;
  background: #eef1f5;
  border: 1px solid #eef1f5;
  border-radius: 6px;
  overflow: hidden;
}
.pcv-info > div {
  display: flex;
  align-items: baseline;
  gap: 8px;
  padding: 7px 10px;
  background: #fff;
  font-size: 12.5px;
  min-width: 0;
}
.pcv-info dt { flex: 0 0 auto; color: #98a0ad; }
.pcv-info dd { flex: 1 1 auto; min-width: 0; margin: 0; color: #1f2430; overflow: hidden; text-overflow: ellipsis; }
.pcv-info .pcv-wide { grid-column: 1 / -1; }

.pc-steps-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 14px;
  font-weight: 600;
  margin: 2px 0 8px;
}
.pc-steps-head .muted { font-weight: 400; font-size: 12.5px; }

/* 工序明细表：列宽用百分比定死（fixed 布局），这样加列、少列都不会让
   某一列把空白全吃掉 */
.pc-steps {
  width: 100%;
  table-layout: fixed;
  border-collapse: collapse;
  font-size: 12.5px;
}
.pc-steps th,
.pc-steps td {
  border: 1px solid #eef1f5;
  padding: 5px 8px;
  text-align: left;
  vertical-align: top;
  white-space: normal;
  word-break: break-word;
}
.pc-steps td.mono { white-space: nowrap; }
.pc-note { color: #6b7280; }
/* 「需检」是布尔量，不是工序名的一部分：给个浅底小标签，扫一眼分得清 */
.pc-insp {
  display: inline-block;
  padding: 0 6px;
  border-radius: 8px;
  background: #fff6e6;
  color: #b7791f;
  font-size: 11.5px;
  line-height: 18px;
}
.pc-steps thead th {
  background: #f8fafc;
  color: #5b6472;
  font-weight: 600;
  white-space: nowrap;
}
.pc-steps tbody tr:nth-child(even) td { background: #fbfcfe; }
.pc-steps tfoot td { background: #f6f8fb; font-weight: 600; }
.pc-steps .c { text-align: center; }
.pc-steps .r { text-align: right; }
.pc-key { font-weight: 600; }
.pc-mark { margin-left: 4px; color: #c0392b; font-size: 12px; }
.pc-mark.warn { color: #b7791f; }
.pc-link { color: #1b62d4; cursor: pointer; }
.pc-link:hover { text-decoration: underline; }

.pc-missing {
  background: #fff;
  border: 1px solid #f0e2c8;
  border-radius: 10px;
  padding: 12px 14px;
}
.pc-missing .section-title { margin-bottom: 8px; color: #b7791f; }
.pc-missing-one { display: flex; gap: 10px; font-size: 12.5px; padding: 2px 0; }

/* ---- 预览弹窗 ---- */
.pv-meta {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  font-size: 12.5px;
  color: #5b6472;
  padding-bottom: 8px;
  border-bottom: 1px dashed #e7ebf1;
  margin-bottom: 10px;
}
.pv-body {
  max-height: calc(100vh - 258px);
  overflow: auto;
  padding: 2px 2px 4px;
  background: #f4f6f9;
  border-radius: 6px;
}
/* 整张纸按 A4 横向比例缩到弹窗里：先保证「全貌一眼看完」 */
.pv-paper {
  position: relative;
  width: min(100%, calc((100vh - 286px) * 297 / 210));
  aspect-ratio: 297 / 210;
  margin: 8px auto;
  background: #fff;
  box-shadow: 0 1px 8px rgba(23, 35, 61, 0.12);
}
.pv-paper img { display: block; width: 100%; height: 100%; object-fit: contain; }
.pv-page-no {
  position: absolute;
  right: 8px;
  bottom: 6px;
  padding: 1px 6px;
  border-radius: 3px;
  background: rgba(0, 0, 0, 0.45);
  color: #fff;
  font-size: 11px;
}
.pv-foot-hint { float: left; line-height: 32px; font-size: 12px; }
.pv-qr-hint { font-size: 12px; margin-right: 14px; }

.mono { font-variant-numeric: tabular-nums; }
.section-title { font-size: 14px; font-weight: 600; display: flex; align-items: baseline; gap: 8px; }
.spacer { flex: 1 1 auto; }
.muted { color: #98a0ad; }
.strong { font-weight: 600; }

@media (max-width: 1180px) {
  .pc-body { grid-template-columns: 1fr; }
  .pc-tree { position: static; }
  .pc-tree-body { max-height: 260px; }
  .pcv-info { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
</style>

<!-- 打印规则必须非 scoped：外壳的 .sidebar / .topbar / .content 都不在这个组件的
     作用域里，写在 scoped 里盖不住它们 -->
<style>
/* 打印副本平时不存在，只有点了「打印」才显形 */
.pc-print-only { display: none; }

@media print {
  @page { size: A4 landscape; margin: 0; }

  /* 只留打印副本：整页其它东西（含弹窗、侧栏、工具条）一律不出现在纸上 */
  body.pc-printing > *:not(.pc-print-only) { display: none !important; }
  body.pc-printing .pc-print-only { display: block !important; }

  /* 一张纸一页。高度取 209.5mm 而不是 210mm —— 差这半毫米，浏览器仍会把
     纸底算成溢出，每张后面白送一张空白页 */
  .pc-print-only img {
    display: block;
    width: 297mm;
    height: 209.5mm;
    object-fit: contain;
    break-after: page;
    page-break-after: always;
  }
  .pc-print-only img:last-child { break-after: auto; page-break-after: auto; }

  /* 没在打印单据时（例如直接 Ctrl+P），至少出张干净的页面：去掉通栏、侧栏与状态栏。
     外壳改成三段式之后（通栏 / 主体 / 状态栏），这三段都得点名，漏一段就会印出
     一条深色空栏。 */
  .sidebar,
  .topnav,
  .statusbar,
  .pc-noprint { display: none !important; }
  .app-shell { display: block !important; height: auto !important; overflow: visible !important; }
  .app-body { display: block !important; }
  .main { display: block !important; }
  .content { overflow: visible !important; height: auto !important; }
  .page { padding: 0 !important; max-width: none !important; }
  .pc-body { display: block !important; }
}
</style>
