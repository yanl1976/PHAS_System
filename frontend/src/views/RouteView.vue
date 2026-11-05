<template>
  <div class="page">
    <div class="page-head">
      <span class="page-title">工艺及工时编排</span>
      <span class="page-sub">
        路线 = 某个「产品 × 件名」的有序工序清单与工时定额。发布后作为单据核对的比对基准；要改点「升版修改」，旧版继续生效直到新版发布。
      </span>
      <div class="spacer"></div>
      <el-button size="small" @click="openCopiesDrawer()">
        本单副本<span v-if="copiesTotal">（{{ copiesTotal }}）</span>
      </el-button>
      <el-button size="small" @click="openPublishDrafts">批量发布草稿</el-button>
      <el-button size="small" @click="openCreate">新建路线</el-button>
      <el-button size="small" @click="loadAll">刷新</el-button>
    </div>

    <div class="summary-bar">
      <span class="sb-item">
        <span class="muted">总组合</span>
        <b class="mono">{{ coverage.total ?? 0 }}</b>
      </span>
      <span class="sb-item"><span class="muted">已发布</span><b class="mono">{{ coverage.covered_count ?? 0 }}</b></span>
      <span class="sb-item"><span class="muted">草稿</span><b class="mono">{{ coverage.draft_count ?? 0 }}</b></span>
      <span class="sb-item"><span class="muted">待建</span><b class="mono warn">{{ coverage.unbuilt_count ?? 0 }}</b></span>
      <span class="sb-item">
        <span class="muted">工时覆盖</span>
        <b class="mono">{{ coverage.coverage_pct ?? 0 }}%</b>
      </span>
      <span class="sb-item" title="这些工艺流程被多少个订单引用（一条路线可被多个任务共用）">
        <span class="muted">被订单引用</span>
        <b class="mono">{{ taskTotal }}</b>
      </span>
      <span class="sb-item" title="基线不仅要发布了，还得有订单真按它做过">
        <span class="muted">被订单验证</span>
        <b class="mono">{{ verifiedPct }}%</b>
      </span>
      <span class="sb-note muted">仅已发布路线参与单据核对</span>
      <div class="spacer"></div>
      <el-button size="small" @click="batchVisible = true">批量生成</el-button>
    </div>

    <div class="route-wrap">
      <!-- 左：产品 → 件名 树（覆盖看板已并入产品行） -->
      <div class="card tree-card">
        <div class="section-title">
          产品 / 件名
          <span class="muted">产品按工时价值 · 件名按装配层级</span>
        </div>
        <div class="row-flex tree-tools">
          <el-input
            v-model="kw"
            size="small"
            placeholder="搜索产品或件名"
            clearable
            style="width: 132px"
            @change="onFilterChange"
          />
          <el-checkbox v-model="onlyUnbuilt" size="small" @change="onFilterChange">只看未建</el-checkbox>
          <!-- 装配关系不在这里定：这里只负责「给这个件编工艺」。
               结构有它自己的窗口，两个页面打开的是同一份 -->
          <el-button size="small" text type="primary" @click="structVisible = true">结构</el-button>
          <el-tag v-if="focusProduct !== null" size="small" type="primary" effect="plain" closable @close="onFilterChange">
            {{ focusLabel }}
          </el-tag>
        </div>
        <div class="tree-body" v-loading="treeLoading">
          <!-- 每个产品一个组件：展开/折叠只重渲染被点的那一个。
               写在一个 render 里时，点一下要把 400 多行全部重新 diff。 -->
          <TreeProduct
            v-for="p in tree"
            :key="p.name"
            :product="p"
            :parts="p.parts"
            :collapsed="collapsedNodes"
            :open="!!expanded[p.name]"
            :active-part="currentPart"
            :active-product="currentProduct"
            @toggle-product="toggleProduct"
            @toggle-node="toggleNode"
            @select="select"
            @task="openTaskDrawer"
            @copies="openCopiesDrawer"
            @unbuilt="focusUnbuilt"
          />
          <el-empty v-if="!tree.length" description="没有匹配的组合" :image-size="60" />
        </div>
      </div>

      <!-- 右：路线编辑 -->
      <div class="card editor-card">
        <div class="section-title">
          {{ currentProduct || '未选择' }} <span v-if="currentPart">/ {{ currentPart }}</span>
          <el-tag v-if="route" size="small" :type="statusType(route.status)" effect="plain">
            {{ statusText(route.status) }} v{{ route.version }}
          </el-tag>
          <div class="spacer"></div>
          <el-select
            v-if="versions.length > 1"
            v-model="activeVersionId"
            size="small"
            style="width: 150px"
            @change="switchVersion"
          >
            <el-option v-for="v in versions" :key="v.id" :value="v.id" :label="`v${v.version} ${statusText(v.status)}`" />
          </el-select>
          <el-button size="small" type="primary" :disabled="!route || route.status !== 'draft'" @click="save">保存</el-button>
          <el-button size="small" :disabled="!route || route.status !== 'draft'" @click="publish(false)">发布</el-button>
          <el-button size="small" :disabled="!route || route.status !== 'released'" @click="publish(true)">升版修改</el-button>
          <el-button
            size="small"
            :disabled="!route || route.status !== 'released' || (route.ref_count || 0) > 0"
            :title="(route && route.ref_count) ? `正被 ${route.ref_count} 个在制任务引用，撤回会让它们失去核对基准` : '撤回发布，回到草稿'"
            @click="reopen"
          >
            撤回发布
          </el-button>
          <el-button
            size="small"
            :disabled="!route || route.status !== 'released' || (route.ref_count || 0) > 0"
            :title="(route && route.ref_count) ? `正被 ${route.ref_count} 个在制任务引用，归档会让它们指向失效基准` : '归档'"
            @click="archive"
          >
            归档
          </el-button>
          <el-button size="small" type="success" :disabled="!route || route.status !== 'archived'" @click="restore('draft')">撤销归档</el-button>
          <el-button size="small" :disabled="!route || route.status === 'released'" @click="remove">删除</el-button>
        </div>

        <el-empty v-if="!route" description="从左侧选择一个产品与件名，或点「新建路线」" :image-size="90" />

        <template v-else>
          <!-- 状态说明放在最上面：路线能不能改、能不能被订单引用，
               全看这一行的状态，藏在右上角的小标签里太容易看漏 -->
          <div class="state-tip" :class="`state-${route.status}`">
            <template v-if="route.status === 'draft'">
              <b>草稿</b>
              <span>可以自由修改；发布后才会成为核对基准，也才能被生产任务引用。</span>
            </template>
            <template v-else-if="route.status === 'released'">
              <b>已发布 v{{ route.version }}</b>
              <span>
                不可直接修改 —— 它是全厂核对基准，改了历史实报就说不清按哪版做的。
                要改点「升版修改」生成新草稿，旧版继续生效直到新版发布。
                <template v-if="route.ref_count">
                  正被 <b>{{ route.ref_count }}</b> 个在制任务引用，所以不能撤回或归档。
                </template>
                <template v-if="route.published_by">
                  （{{ route.published_by }} 发布于 {{ (route.published_at || '').slice(0, 16).replace('T', ' ') }}）
                </template>
              </span>
            </template>
            <template v-else>
              <b>已归档 v{{ route.version }}</b>
              <span>不再参与单据核对；可点「撤销归档」恢复为草稿或已发布。</span>
            </template>
          </div>

          <!-- 升版影响：这条基准被换过版、还有谁没认，编排页自己也要说清楚，
               不能只靠任务页标红——改标准的人通常就在这一页 -->
          <div v-if="route.notice || route.pending_count" class="impact-tip">
            <span class="impact-badge">升版影响</span>
            <span v-if="route.notice" class="impact-text">
              最近一次：v{{ route.notice.from_version }} → v{{ route.notice.to_version }}
              <template v-if="route.notice.actor"> · {{ route.notice.actor }}</template>
              · {{ fmtTime(route.notice.at) }}
              <template v-if="route.notice.unit_delta">
                · 单件 {{ route.notice.unit_delta > 0 ? '+' : '' }}{{ route.notice.unit_delta }}h
              </template>
            </span>
            <span v-if="route.pending_count" class="impact-pending">
              {{ route.pending_count }} 个引用订单尚未确认
            </span>
            <div class="spacer"></div>
            <el-button size="small" text type="primary" @click="openRefsDrawer">看是哪几单</el-button>
          </div>

          <div class="meta-row">
            <el-input v-model="form.title" size="small" placeholder="路线名称" style="width: 220px" :disabled="!isDraft" />
            <span class="meta-label">标准批量</span>
            <el-input-number v-model="form.batch_qty" size="small" :min="0" :step="10" :controls="false" :disabled="!isDraft" style="width: 92px" />
            <span class="meta-hint">填了才分摊准备工时，0 = 纯单件</span>
            <span class="meta-label">宽放率</span>
            <el-input-number v-model="form.allowance_rate" size="small" :min="0" :step="0.01" :precision="3" :controls="false" :disabled="!isDraft" style="width: 82px" />
            <el-select v-model="form.completeness" size="small" :disabled="!isDraft" style="width: 120px">
              <el-option value="partial" label="片段（仅已知工序）" />
              <el-option value="full" label="完整" />
            </el-select>
          </div>

          <div class="row-flex step-tools">
            <el-button size="small" :disabled="!isDraft" @click="addStep">加一行</el-button>
            <el-button size="small" :disabled="!isDraft" @click="fromHistory">从历史生成</el-button>
            <el-button size="small" :disabled="!isDraft" @click="openCopy">引用其他路线</el-button>
            <el-button size="small" :disabled="!isDraft" @click="reorderBySeq">按工序号排序</el-button>
            <div class="spacer"></div>
            <span class="muted">点行首 ↑ ↓ 可调整顺序（工序号一并互换）</span>
          </div>

          <el-table
            :data="form.steps"
            size="small"
            class="step-table"
            row-key="uid"
            v-loading="loading"
          >
            <el-table-column label="排序" width="66" align="center">
              <template #default="{ $index }">
                <div class="ord-wrap">
                  <el-button
                    link
                    class="ord-btn"
                    :disabled="!isDraft || $index === 0"
                    title="上移一行（工序号自动互换）"
                    @click="moveStep($index, -1)"
                  >
                    <el-icon><ArrowUp /></el-icon>
                  </el-button>
                  <el-button
                    link
                    class="ord-btn"
                    :disabled="!isDraft || $index === form.steps.length - 1"
                    title="下移一行（工序号自动互换）"
                    @click="moveStep($index, 1)"
                  >
                    <el-icon><ArrowDown /></el-icon>
                  </el-button>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="工序号" width="76" align="center">
              <template #default="{ row }">
                <el-input-number v-model="row.seq" size="small" :min="0" :step="10" :controls="false" :disabled="!isDraft" style="width: 66px" />
              </template>
            </el-table-column>
            <el-table-column label="工序" min-width="150">
              <template #default="{ row }">
                <el-select
                  v-model="row.process_code"
                  size="small"
                  filterable
                  placeholder="选工序"
                  class="proc-cell"
                  style="width: 100%"
                  :disabled="!isDraft"
                  @change="onProcessChange(row)"
                >
                  <el-option v-for="p in processOptions" :key="p.code" :value="p.code" :label="p.name" />
                </el-select>
                <div v-if="row.not_in_dict" class="cell-sub warn" :title="row.remark">
                  疑似非工序，请确认
                </div>
              </template>
            </el-table-column>
            <el-table-column label="工种" width="96">
              <template #default="{ row }">
                <el-input v-model="row.category" size="small" :disabled="!isDraft" />
              </template>
            </el-table-column>
            <el-table-column label="设备/工位" width="116">
              <template #default="{ row }">
                <el-input v-model="row.work_center" size="small" :disabled="!isDraft" />
              </template>
            </el-table-column>
            <el-table-column label="准备工时" width="100" align="right">
              <template #default="{ row }">
                <el-input-number v-model="row.setup_hours" size="small" :min="0" :step="0.1" :precision="3" :controls="false" :disabled="!isDraft" style="width: 88px" />
              </template>
            </el-table-column>
            <el-table-column label="单件工时" width="130" align="right">
              <template #default="{ row }">
                <el-input-number v-model="row.unit_hours" size="small" :min="0" :step="0.25" :precision="4" :controls="false" :disabled="!isDraft" style="width: 96px" />
                <div class="cell-sub">{{ hms(row.unit_hours) }}</div>
              </template>
            </el-table-column>
            <el-table-column label="来源" width="94">
              <template #default="{ row }">
                <el-tag size="small" :type="srcType(row.hours_src)" effect="plain">{{ srcText(row.hours_src) }}</el-tag>
                <div v-if="row.samples" class="cell-sub" :class="{ warn: row.samples < 3 }">{{ row.samples }} 样本</div>
              </template>
            </el-table-column>
            <el-table-column label="关键" width="58" align="center">
              <template #default="{ row }">
                <el-checkbox v-model="row.is_key" :disabled="!isDraft" />
              </template>
            </el-table-column>
            <el-table-column label="需检验" width="66" align="center">
              <template #default="{ row }">
                <el-checkbox v-model="row.inspect_required" :disabled="!isDraft" />
              </template>
            </el-table-column>
            <el-table-column label="备注" min-width="130">
              <template #default="{ row }">
                <el-input v-model="row.remark" size="small" :disabled="!isDraft" />
              </template>
            </el-table-column>
            <el-table-column width="48" align="center">
              <template #default="{ $index }">
                <el-button link type="danger" size="small" :disabled="!isDraft" @click="form.steps.splice($index, 1)">删</el-button>
              </template>
            </el-table-column>
          </el-table>

          <div class="sum-bar">
            <span class="muted">工序数 <b class="mono">{{ form.steps.length }}</b></span>
            <span class="muted">单件合计 <b class="mono">{{ unitSum.toFixed(4) }}</b> h</span>
            <span class="muted">准备合计 <b class="mono">{{ setupSum.toFixed(3) }}</b> h</span>
            <el-divider direction="vertical" />
            <span v-if="form.batch_qty > 0">
              批量 {{ form.batch_qty }} → 单件含准备
              <b class="mono">{{ perUnit.toFixed(4) }}</b> h
              <span class="muted">（含 H:MM:SS {{ hms(perUnit) }}）</span>
            </span>
            <span v-else class="muted">未填标准批量 → 按纯单件计（与现行核算口径一致）</span>
          </div>
        </template>
      </div>
    </div>

    <!-- 本单副本清单：副本不参与基线清单（kind=task），必须有独立入口，
         否则拆出去就再也找不到，工艺等于被藏起来 -->
    <el-drawer
      v-model="copiesVisible"
      size="1080px"
      :close-on-click-modal="false"
      destroy-on-close
      class="copy-drawer"
    >
      <template #header>
        <div class="copy-head">
          <b>本单副本</b>
          <span class="muted">
            只属于某个订单的工艺，与基线并列管理。与现行基线一致时可直接「合并回引用」；
            确实更好时「进阶为基线」，它会成为全厂新版标准。
          </span>
        </div>
      </template>

      <div class="row-flex copy-tools">
        <el-input
          v-model="copiesKw"
          size="small"
          placeholder="搜产品 / 件名"
          clearable
          style="width: 180px"
          @change="loadCopies"
        />
        <el-checkbox v-model="copiesOnlyDiff" size="small">只看与基线不一致</el-checkbox>
        <el-tag v-if="copiesFilter" size="small" type="primary" effect="plain" closable @close="clearCopiesFilter">
          {{ copiesFilter.product || '（未指定产品）' }} / {{ copiesFilter.part }}
        </el-tag>
        <div class="spacer"></div>
        <span class="muted">
          {{ copiesList.length }} 份
          <template v-if="copiesIdentical">
            · <span class="ok">{{ copiesIdentical }} 份与基线一致，可合并</span>
          </template>
        </span>
        <el-button size="small" @click="loadCopies">刷新</el-button>
      </div>

      <el-table :data="copiesRows" size="small" v-loading="copiesLoading" max-height="calc(100vh - 226px)">
        <el-table-column label="产品 / 件名" min-width="190" show-overflow-tooltip>
          <template #default="{ row }">
            <div class="mono">{{ row.product }}</div>
            <div class="cell-sub">{{ row.part || '—' }}</div>
          </template>
        </el-table-column>
        <el-table-column label="所属订单" min-width="160" show-overflow-tooltip>
          <template #default="{ row }">
            <div v-if="row.task_no" class="mono">{{ row.task_no }}</div>
            <div v-else class="warn">无归属任务</div>
            <div class="cell-sub">{{ row.task_title || '' }}</div>
          </template>
        </el-table-column>
        <el-table-column label="工序" width="66" align="right" prop="step_count" />
        <el-table-column label="单件" width="82" align="right">
          <template #default="{ row }"><span class="mono">{{ row.unit_hours }}</span></template>
        </el-table-column>
        <el-table-column label="来源基线" width="88" align="center">
          <template #default="{ row }">
            <span v-if="row.source_version" class="mono">v{{ row.source_version }}</span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="现行基线" width="88" align="center">
          <template #default="{ row }">
            <span v-if="row.baseline_version" class="mono">v{{ row.baseline_version }}</span>
            <span v-else class="warn">未发布</span>
          </template>
        </el-table-column>
        <el-table-column label="较基线" width="96" align="right">
          <template #default="{ row }">
            <el-tag v-if="row.identical_to_baseline" size="small" type="info" effect="plain">一致</el-tag>
            <span v-else class="mono" :class="row.delta_total > 0 ? 'warn' : 'ok'">
              {{ row.delta_total > 0 ? '+' : '' }}{{ row.delta_total }}h
            </span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="236" fixed="right">
          <template #default="{ row }">
            <div class="op-cell">
              <el-button size="small" text type="primary" :disabled="!row.task_route_id" @click="editCopiesRow(row)">
                编辑
              </el-button>
              <el-button size="small" text type="success" :disabled="!row.task_route_id" @click="promoteCopiesRow(row)">
                {{ row.identical_to_baseline ? '合并回引用' : '进阶为基线' }}
              </el-button>
              <el-button size="small" text :disabled="!row.task_id" @click="gotoTask(row)">去任务</el-button>
            </div>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!copiesRows.length && !copiesLoading" description="没有符合条件的本单副本" :image-size="70" />
    </el-drawer>

    <!-- 批量发布草稿：只有已发布的路线才能被任务引用 -->
    <el-dialog v-model="pdVisible" title="批量发布草稿基线" width="580px" :close-on-click-modal="false">
      <div class="hint-box">
        只有<b>已发布</b>的工艺路线才能被生产任务引用。这里只处理「从来没有发布过」的草稿：
        已有生效版本的组合一律跳过 —— 那属于升版，必须逐条确认。
        没有工序的草稿也跳过（空路线当基准会让核对把所有工序判成「多做的」）。
      </div>
      <div v-if="pdPreview" class="pd-box">
        <div class="pd-sum">
          将发布 <b class="ok">{{ pdPreview.published_count }}</b> 条
          <span v-if="pdPreview.skipped_count" class="muted">· 跳过 {{ pdPreview.skipped_count }} 条</span>
        </div>
        <div v-if="pdPreview.items.length" class="pd-list">
          <div v-for="x in pdPreview.items" :key="x.id" class="pd-row">
            <span class="mono">{{ x.product }} / {{ x.part }}</span>
            <span class="muted">v{{ x.version }} · {{ x.step_count }} 道</span>
          </div>
        </div>
        <div v-if="pdPreview.skipped.length" class="pd-skipped">
          <div class="muted" style="margin-bottom: 4px">跳过明细</div>
          <div v-for="x in pdPreview.skipped.slice(0, 8)" :key="x.id" class="pd-row">
            <span class="mono">{{ x.product }} / {{ x.part }}</span>
            <span class="muted">{{ x.reason }}</span>
          </div>
        </div>
      </div>
      <template #footer>
        <span class="muted" style="margin-right: auto; font-size: 12px">发布后立即可被任务引用</span>
        <el-button size="small" :loading="pdBusy" @click="runPublishDrafts(true)">预演</el-button>
        <el-button
          size="small"
          type="primary"
          :loading="pdBusy"
          :disabled="!pdPreview || !pdPreview.published_count || pdPreview.dry_run === false"
          @click="runPublishDrafts(false)"
        >
          确认发布
        </el-button>
      </template>
    </el-dialog>

    <!-- 新建路线 -->
    <el-dialog v-model="createVisible" title="新建工艺路线" width="440px">
      <el-form label-width="90px" size="small">
        <el-form-item label="产品" required>
          <el-select v-model="newRoute.product" filterable allow-create placeholder="选或输入产品" style="width: 100%">
            <el-option v-for="p in productNames" :key="p" :value="p" :label="p" />
          </el-select>
        </el-form-item>
        <el-form-item label="件名" required>
          <el-select v-model="newRoute.part" filterable allow-create placeholder="选或输入件名" style="width: 100%">
            <el-option v-for="p in partNames" :key="p" :value="p" :label="p" />
          </el-select>
        </el-form-item>
        <el-form-item label="生成工序">
          <el-checkbox v-model="newRoute.generate">按历史工时自动生成工序行</el-checkbox>
          <div class="muted" style="font-size: 12px; margin-top: 2px">
            勾上会按「真单据顺序 → 工艺默认序 → 样本数」三档推断顺序，再人工微调。
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="doCreate">创建草稿</el-button>
      </template>
    </el-dialog>

    <!-- 批量生成 -->
    <el-dialog v-model="batchVisible" title="按价值批量生成路线草稿" width="480px">
      <el-form label-width="96px" size="small">
        <el-form-item label="生成条数">
          <el-input-number v-model="batchLimit" :min="1" :max="500" :step="10" />
          <div class="muted" style="font-size: 12px; margin-top: 2px">
            按「历史累计工时」从高到低取。已有的组合会跳过，不会重复建。
          </div>
        </el-form-item>
        <el-form-item label="生成后">
          <el-radio-group v-model="batchPublish">
            <el-radio :value="false">保持草稿（可再改，逐条确认后发布）</el-radio>
            <el-radio :value="true">直接发布</el-radio>
          </el-radio-group>
          <div class="muted" style="font-size: 12px; margin-top: 2px">
            直接发布后作为核对基准；要改就点「升版修改」，旧版继续生效直到新版发布。
          </div>
        </el-form-item>
      </el-form>
      <div v-if="batchPreview" class="batch-preview">
        <div class="muted">将生成 {{ batchPreview.created_count }} 条，跳过 {{ batchPreview.skipped_count }} 条</div>
        <div class="batch-list">
          <div v-for="x in batchPreview.created.slice(0, 8)" :key="x.product + x.part" class="batch-item">
            {{ x.product }} / {{ x.part }} · {{ x.step_count }} 道工序
          </div>
          <div v-if="batchPreview.created.length > 8" class="muted">…… 还有 {{ batchPreview.created.length - 8 }} 条</div>
        </div>
      </div>
      <template #footer>
        <el-button @click="batchVisible = false">取消</el-button>
        <el-button @click="doPreview">预览</el-button>
        <el-button type="primary" :loading="batchBusy" @click="doBatchGenerate">生成</el-button>
      </template>
    </el-dialog>

    <!-- 引用其他路线 -->
    <el-dialog v-model="copyVisible" title="引用其他路线的工序行" width="560px">
      <el-select v-model="copySourceId" filterable placeholder="选一条已发布路线" style="width: 100%">
        <el-option
          v-for="r in publishedRoutes"
          :key="r.id"
          :value="r.id"
          :label="`${r.product} / ${r.part}  (${r.step_count} 道)`"
        />
      </el-select>
      <div class="muted" style="font-size: 12px; margin-top: 8px">
        只把工序行拷进来，工时可再单独调整；原路线的版本与发布状态不受影响。
      </div>
      <template #footer>
        <el-button @click="copyVisible = false">取消</el-button>
        <el-button type="primary" @click="doCopy">引用</el-button>
      </template>
    </el-dialog>
    <!-- 订单引用抽屉：这个零件被哪些订单用着、谁在引用谁在改 -->
    <el-drawer v-model="taskVisible" title="引用该工艺的订单" size="640px" destroy-on-close>
      <div v-if="!taskList.length" class="muted" style="padding: 12px">
        还没有订单引用这个件名的工艺。到「生产任务」里建任务时可以把这条路线挂上去。
      </div>
      <template v-else>
        <div class="task-picker">
          <div
            v-for="t in taskList"
            :key="t.id"
            class="task-pick"
            :class="{ active: activeTaskId === t.id }"
            @click="selectTask(t.id)"
          >
            <div class="task-pick-no">{{ t.task_no }}</div>
            <div class="muted task-pick-meta">
              {{ t.title || '—' }} · 批次 {{ t.batch_no || '—' }} · 单量 {{ t.qty }} · 实报 {{ t.actual_hours }}h
              <el-tag v-if="t.origin === 'copy'" size="small" type="warning" effect="plain">本单副本</el-tag>
              <el-tag v-else size="small" type="success" effect="plain">引用基线</el-tag>
              <el-tag v-if="t.confirmed === false" size="small" type="danger" effect="dark">升版待确认</el-tag>
            </div>
          </div>
        </div>

        <template v-if="taskDetail">
          <div class="task-block">
            <div class="task-grid">
              <div><span class="muted">任务号</span><b>{{ taskDetail.task_no }}</b></div>
              <div><span class="muted">标题</span><b>{{ taskDetail.title || '—' }}</b></div>
              <div><span class="muted">订单数量</span><b>{{ taskDetail.qty || '—' }}</b></div>
              <div><span class="muted">路线 / 工序</span><b>{{ taskDetail.summary.route_count }} 条 / {{ taskDetail.summary.step_count }} 道</b></div>
              <div><span class="muted">单件工时</span><b>{{ taskDetail.summary.unit_hours }}h</b></div>
              <div><span class="muted">实报工时</span><b>{{ taskDetail.summary.actual_hours }}h</b></div>
            </div>
          </div>

          <!-- 这单引用的标准被换过版、它还没认：在引用清单里也要红字说明 -->
          <div v-if="(taskDetail.notices || []).length" class="task-notice">
            <div class="task-notice-title">
              基线升版待确认 {{ taskDetail.notices.length }} 条
            </div>
            <div v-for="n in taskDetail.notices" :key="n.task_route_id" class="task-notice-row">
              <b>{{ n.part || '—' }}</b>
              <span>{{ n.message }}</span>
            </div>
            <div class="task-notice-foot">
              到「生产任务」打开这单，可逐条选择「采用新版」或「保留现版」。
            </div>
          </div>

          <div class="task-block">
            <div class="task-block-title">
              这个订单的工艺路线
              <span class="task-sub">{{ (taskDetail.routes || []).length }} 条</span>
            </div>
            <el-table :data="taskDetail.routes || []" size="small" max-height="260">
              <el-table-column label="件名" min-width="120" show-overflow-tooltip prop="part" />
              <el-table-column label="来源" width="92" align="center">
                <template #default="{ row }">
                  <el-tag v-if="row.origin === 'copy'" size="small" type="warning" effect="plain">副本</el-tag>
                  <el-tag v-else size="small" type="success" effect="plain">引用</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="工序" width="64" align="right" prop="step_count" />
              <el-table-column label="单件" width="78" align="right" prop="unit_hours" />
              <el-table-column label="实报" width="78" align="right" prop="actual_hours" />
            </el-table>
          </div>

          <div class="task-block">
            <div class="task-block-title">归属单据<span class="task-sub">{{ (taskDetail.sheets || []).length }} 张</span></div>
            <el-table :data="taskDetail.sheets || []" size="small" max-height="200">
              <el-table-column label="#" width="66">
                <template #default="{ row }">
                  <el-link type="primary" :underline="false" @click="$router.push(`/review/${row.id}`)">#{{ row.id }}</el-link>
                </template>
              </el-table-column>
              <el-table-column label="明细" width="64" align="right" prop="item_count" />
              <el-table-column label="工时" width="84" align="right" prop="total_hours" />
              <el-table-column label="确认" min-width="120">
                <template #default="{ row }">{{ row.verify_user || '—' }}</template>
              </el-table-column>
            </el-table>
          </div>

          <div class="task-total muted">
            要到任务里加/减工艺路线、拆副本或结案，去「生产任务」页。
          </div>
        </template>
      </template>
    </el-drawer>

    <!-- 副本编辑：复用任务侧的就地编辑器，避免两套工序编辑逻辑分叉 -->
    <RouteEditDialog
      v-model="copiesEditVisible"
      :task-id="copiesEditTaskId"
      :tr="copiesEditTr"
      @saved="onCopiesSaved"
    />

    <!-- 产品结构：和「工时组合 → 产品结构」是同一个组件、同一份数据。
         编排页面不自己维护装配关系，只提供入口，避免两处各有一套结构。
         窗口按原尺寸缩 30%（1040×560 → 728×392）：行尾那 6 个导航键在最右边，
         窗口越宽，鼠标从左边的件名挪到右边按键的路越远。 -->
    <el-dialog
      v-model="structVisible"
      title="产品结构 · 总装 → 部件 → 子件"
      width="min(730px, 92vw)"
      top="7vh"
      destroy-on-close
    >
      <StructurePanel
        v-if="structVisible"
        :product="focusProduct || ''"
        :focus-part="currentPart || ''"
        @changed="loadTree"
      />
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api'
import { store } from '../store'
import RouteEditDialog from '../components/RouteEditDialog.vue'
import TreeProduct from '../components/TreeProduct.vue'
import StructurePanel from '../components/StructurePanel.vue'

const router = useRouter()

const tree = ref([])
const treeLoading = ref(false)
const coverage = ref({})
const kw = ref('')
const onlyUnbuilt = ref(false)
const expanded = ref({})

// 任务来源抽屉
const taskVisible = ref(false)
const taskList = ref([])
const activeTaskId = ref(null)
const taskDetail = ref(null)

// 摘要里的「任务」与「被任务验证」按工时加权，不是简单平均数：
// 一个只占 0.1% 工时的小件名建了基线，不该把整厂的验证率拉高。
const taskTotal = computed(() => tree.value.reduce((s, p) => s + (p.task_count || 0), 0))
const verifiedPct = computed(() => {
  const total = tree.value.reduce((s, p) => s + (p.value || 0), 0)
  if (!total) return 0
  const verified = tree.value.reduce((s, p) => s + (p.value || 0) * ((p.verified_pct || 0) / 100), 0)
  return Math.round((verified / total) * 1000) / 10
})

async function openTaskDrawer(p, x) {
  taskList.value = x ? x.tasks || [] : p.parts.flatMap((it) => it.tasks || [])
  taskDetail.value = null
  activeTaskId.value = null
  taskVisible.value = true
  if (taskList.value.length) await selectTask(taskList.value[0].id)
}

// 「看是哪几单」：直接用路线详情里的引用清单打开抽屉，
// 不必绕回树里按件名找 —— 从提醒点进来的人要的是马上定位。
async function openRefsDrawer() {
  const refs = route.value?.ref_tasks || []
  if (!refs.length) return ElMessage.info('这条路线还没有订单引用')
  // 待确认的排前面：点「看是哪几单」的人多半就是来处理提醒的
  const sorted = [...refs].sort(
    (a, b) => Number(a.confirmed === false ? 0 : 1) - Number(b.confirmed === false ? 0 : 1)
  )
  taskList.value = sorted.map((r) => ({
    id: r.task_id,
    task_no: r.task_no,
    title: r.title,
    origin: r.origin,
    confirmed: r.confirmed
  }))
  taskDetail.value = null
  activeTaskId.value = null
  taskVisible.value = true
  await selectTask(taskList.value[0].id)
}

function fmtTime(s) {
  if (!s) return ''
  return String(s).replace('T', ' ').slice(0, 16)
}

async function selectTask(id) {
  activeTaskId.value = id
  try {
    taskDetail.value = await api.task(id)
  } catch {
    taskDetail.value = null
  }
}
// 点「待建 N」时把视图收窄到某个产品；null 表示看全部。
// 用 null 而不是空串：「未指定产品」的真实产品名就是空串，
// 空串没法区分「聚焦到它」和「没有聚焦」。
const focusProduct = ref(null)
// 产品结构弹窗：编排页只提供入口，结构本身不在这一页编辑
const structVisible = ref(false)

// 与后端一致的虚拟产品名：历史工时里没写产品的组合归到它下面
const UNSPECIFIED = '（未指定产品）'

const route = ref(null)
// 本页的 route 变量已被「当前路线」占用，路由实例另起名
const rt = useRoute()
const versions = ref([])
const activeVersionId = ref(null)
const loading = ref(false)
const currentProduct = ref('')
const currentPart = ref('')

const form = ref({ title: '', batch_qty: 0, allowance_rate: 0, completeness: 'partial', steps: [] })

const createVisible = ref(false)
const creating = ref(false)
const newRoute = ref({ product: '', part: '', generate: true })

const copyVisible = ref(false)
const copySourceId = ref(null)

const batchVisible = ref(false)
const batchLimit = ref(50)
const batchPublish = ref(false)
const batchPreview = ref(null)
const batchBusy = ref(false)

// ------------------------------------------------------------------ 本单副本
// 副本挂在任务上（kind='task'），不在基线树里，所以需要独立清单。
// 这里的每个动作都要带 task_id + task_route_id —— 副本没有独立生命周期，
// 它的一切操作都发生在任务上下文里。
const copiesVisible = ref(false)
const copiesLoading = ref(false)
const copiesList = ref([])
const copiesKw = ref('')
const copiesOnlyDiff = ref(false)
const copiesFilter = ref(null)
const copiesIdentical = ref(0)

const copiesRows = computed(() => {
  let rows = copiesList.value
  if (copiesFilter.value) {
    rows = rows.filter(
      (r) => r.part === copiesFilter.value.part && (r.product || '') === (copiesFilter.value.product || '')
    )
  }
  if (copiesOnlyDiff.value) rows = rows.filter((r) => !r.identical_to_baseline)
  return rows
})

// 树里已带每份副本的计数，这里汇总一次就能在按钮上显示，不用等抽屉打开
const copiesTotal = computed(() => tree.value.reduce((s, p) => s + (p.copy_count || 0), 0))

async function loadCopies() {
  copiesLoading.value = true
  try {
    const d = await api.routeCopies({ q: copiesKw.value, limit: 500 })
    copiesList.value = d.items || []
    copiesIdentical.value = d.identical_count || 0
  } finally {
    copiesLoading.value = false
  }
}

async function openCopiesDrawer(part, product) {
  copiesFilter.value = part ? { part, product: product || '' } : null
  copiesVisible.value = true
  await loadCopies()
}

function clearCopiesFilter() {
  copiesFilter.value = null
}

// 抽屉里的副本改用任务级编辑弹窗：副本的就地修改只在这里发生
const copiesEditVisible = ref(false)
const copiesEditTaskId = ref(null)
const copiesEditTr = ref(null)

function editCopiesRow(row) {
  if (!row.task_route_id || !row.task_id) {
    ElMessage.warning('这份副本已经没有归属任务，无法编辑；请先在任务里重新挂上')
    return
  }
  copiesEditTaskId.value = row.task_id
  copiesEditTr.value = {
    id: row.task_route_id,
    route_id: row.route_id,
    origin: 'copy',
    ref_count: 0,
    baseline_id: row.baseline_id,
    baseline_version: row.baseline_version,
    product: row.product,
    part: row.part
  }
  copiesEditVisible.value = true
}

async function onCopiesSaved() {
  await Promise.all([loadCopies(), loadTree(), loadCoverage()])
}

async function promoteCopiesRow(row) {
  if (!row.task_route_id || !row.task_id) {
    ElMessage.warning('这份副本没有归属任务，无法进阶')
    return
  }
  const merged = row.identical_to_baseline
  try {
    await ElMessageBox.confirm(
      merged
        ? `这份副本与现行基线 v${row.baseline_version || '?'} 完全一致，合并后任务改回「引用基线」，副本会被删除。确定？`
        : `进阶会把这份副本发布成「${row.product} / ${row.part}」的新版基线（旧版归档），` +
          '所有引用旧版的在制订单一起切到新版，副本随后删除。确定进阶？',
      merged ? '合并回引用' : '进阶为基线',
      { type: 'warning', confirmButtonText: merged ? '合并' : '确定进阶' }
    )
  } catch {
    return
  }
  try {
    const r = await api.promoteTaskRoute(row.task_id, row.task_route_id)
    ElMessage.success(r.message || '已处理')
    await Promise.all([loadCopies(), loadTree(), loadCoverage(), loadPublished()])
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '进阶失败')
  }
}

function gotoTask(row) {
  copiesVisible.value = false
  router.push({ path: '/tasks', query: { task: row.task_id } })
}

// ------------------------------------------------------------------ 批量发布草稿
const pdVisible = ref(false)
const pdPreview = ref(null)
const pdBusy = ref(false)

function openPublishDrafts() {
  pdPreview.value = null
  pdVisible.value = true
  runPublishDrafts(true)
}

async function runPublishDrafts(dry) {
  pdBusy.value = true
  try {
    const d = await api.publishDrafts({ dry_run: dry, limit: 500, user: 'operator' })
    pdPreview.value = d
    if (!dry) {
      ElMessage.success(`已发布 ${d.published_count} 条草稿基线`)
      await Promise.all([loadTree(), loadCoverage(), loadPublished()])
      pdVisible.value = false
    }
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '批量发布失败')
  } finally {
    pdBusy.value = false
  }
}

let uid = 0
const nextUid = () => ++uid

const isDraft = computed(() => route.value?.status === 'draft')

const processOptions = computed(() =>
  (store.dicts.processes || []).map((p) => ({ code: p.code, name: p.name, category: p.category }))
)

// 新建路线对话框的下拉候选。
// 不能直接用 tree：树可能被「只看未建」或产品聚焦过滤，
// 那样新建时就只能选到当前可见的那几个名字。
const knownProducts = ref([])
const knownParts = ref([])

const productNames = computed(() => knownProducts.value.filter((n) => n !== UNSPECIFIED))
const partNames = computed(() => knownParts.value)

// 聚焦时的展示名：「未指定产品」的真实名是空串，要用展示名
const focusLabel = computed(() => {
  if (focusProduct.value === null) return ''
  return focusProduct.value || UNSPECIFIED
})

const publishedRoutes = ref([])

const unitSum = computed(() => form.value.steps.reduce((s, r) => s + (Number(r.unit_hours) || 0), 0))
const setupSum = computed(() => form.value.steps.reduce((s, r) => s + (Number(r.setup_hours) || 0), 0))
const perUnit = computed(() => {
  const batch = Number(form.value.batch_qty) || 0
  return unitSum.value + (batch > 0 ? setupSum.value / batch : 0)
})

function hms(hours) {
  const v = Number(hours)
  if (!v) return ''
  const neg = v < 0
  const a = Math.abs(v)
  let hh = Math.floor(a)
  let mm = Math.round((a - hh) * 60)
  if (mm === 60) { hh += 1; mm = 0 }
  return `${neg ? '-' : ''}${hh}:${String(mm).padStart(2, '0')}:00`
}

function statusText(s) {
  return { draft: '草稿', released: '已发布', archived: '已归档' }[s] || s
}
function statusType(s) {
  return { draft: 'warning', released: 'success', archived: 'info' }[s] || 'info'
}
function srcText(s) {
  return { history: '历史', sheet: '单据', manual: '手工' }[s] || s
}
function srcType(s) {
  return { history: 'success', sheet: 'primary', manual: 'info' }[s] || 'info'
}

function toggleProduct(name) {
  expanded.value[name] = !expanded.value[name]
}

// ------------------------------------------------------------------ 装配层级
// 件名不是平级的一串：总装下面挂部件，部件下面挂子件。左栏用缩进 + 竖线
// 表达这层关系，并允许按部件收起 —— 一百多件的产品全摊开是没法找东西的。
// 折叠状态只放在界面上，不落库：它是「此刻想看多细」，不是数据。
const collapsedNodes = ref({})

const nodeKey = (pname, part) => `${pname}::${part}`

function toggleNode(pname, part) {
  const k = nodeKey(pname, part)
  collapsedNodes.value[k] = !collapsedNodes.value[k]
}

// 折叠状态被整体清空（搜索、只看未建、重载）时，换一个新对象就行：
// 子组件收到新的 collapsed，会各自重算自己的行。
function resetCollapsed() {
  collapsedNodes.value = {}
}

// ------------------------------------------------------------------ 加载
async function loadTree() {
  treeLoading.value = true
  try {
    const d = await api.routeTree({
      q: kw.value,
      only_unbuilt: onlyUnbuilt.value,
      // null 表示不限产品，这个参数不能传（传空串会被当成「未指定产品」）
      product: focusProduct.value === null ? undefined : focusProduct.value
    })
    tree.value = d.items || []
    // 搜索/只看未建时把折叠全部展开：否则命中的件藏在收起的部件里，
    // 人看到的是「没结果」，其实是自己把它收起来了
    if (kw.value || onlyUnbuilt.value) {
      resetCollapsed()
      // 命中的产品也一并展开 —— 产品默认收起是为了「不筛选时别铺满屏」，
      // 已经筛出结果了还收着，人只会以为没搜到
      for (const p of tree.value) expanded.value[p.name] = true
    }
    // 新建路线的候选名单要累积而不是覆盖：树被过滤后，
    // 候选名单若也跟着变小，就只能选到当前可见的那几个名字
    const ps = new Set(knownProducts.value)
    const rs = new Set(knownParts.value)
    for (const p of tree.value) {
      ps.add(p.name)
      for (const x of p.parts) rs.add(x.part)
    }
    knownProducts.value = [...ps]
    knownParts.value = [...rs]
    // 产品默认全部收起：110 多个产品全摊开要滚好几屏，
    // 想找的那一个反而不在视口里。展开状态只记用户自己点过的，
    // 不再「偷偷」替人展开前几个 —— 那种默认展开还会随排序变化，
    // 每次刷新落在不同的产品上，看着像树在自己乱动。
    // 树重载后，当前组合的版本列表也要跟着更新（升版/归档会改状态）
    if (currentProduct.value && currentPart.value) {
      const pn = tree.value.find((p) => p.name === (currentProduct.value || UNSPECIFIED))
      const part = pn?.parts.find((x) => x.part === currentPart.value)
      if (part) versions.value = part.versions || []
    }
  } finally {
    treeLoading.value = false
  }
}

async function loadAll() {
  await Promise.all([loadTree(), loadCoverage(), loadPublished()])
}

async function loadCoverage() {
  const d = await api.routeCoverage(200)
  coverage.value = d
}

async function loadPublished() {
  const d = await api.routes({ status: '', limit: 2000 })
  publishedRoutes.value = d.items || []
}

async function select(product, partNode) {
  // 「（未指定产品）」是展示用的虚拟名，真实产品名是空的
  currentProduct.value = product === UNSPECIFIED ? '' : product
  currentPart.value = partNode.part
  versions.value = partNode.versions || []
  if (partNode.route_id) {
    await openRoute(partNode.route_id)
    activeVersionId.value = partNode.route_id
  } else {
    route.value = null
    form.value = { title: '', batch_qty: 0, allowance_rate: 0, completeness: 'partial', steps: [] }
    activeVersionId.value = null
    ElMessage.info('该组合还没建路线，可点「新建路线」')
  }
}

async function openRoute(id) {
  loading.value = true
  try {
    const d = await api.route(id)
    route.value = d
    form.value = {
      title: d.title || '',
      batch_qty: d.batch_qty || 0,
      allowance_rate: d.allowance_rate || 0,
      completeness: d.completeness || 'partial',
      steps: (d.steps || []).map((s) => ({ ...s, uid: nextUid() }))
    }
  } finally {
    loading.value = false
  }
}

async function switchVersion(id) {
  await openRoute(id)
}

// ------------------------------------------------------------------ 工序行
function addStep() {
  form.value.steps.push({
    uid: nextUid(),
    seq: (form.value.steps.length + 1) * 10,
    process_code: '',
    process_name: '',
    op_type: 'self',
    category: '',
    work_center: '',
    setup_hours: 0,
    unit_hours: 0,
    hours_src: 'manual',
    samples: 0,
    is_key: false,
    inspect_required: false,
    remark: ''
  })
}

function onProcessChange(row) {
  const p = processOptions.value.find((x) => x.code === row.process_code)
  if (p) {
    row.process_name = p.name
    if (!row.category) row.category = p.category || ''
  }
}

async function fromHistory() {
  if (!currentProduct.value || !currentPart.value) return ElMessage.warning('先选中产品与件名')
  const d = await api.routeSuggest(currentProduct.value, currentPart.value)
  if (!d.steps.length) {
    ElMessage.info('这个组合没有历史工时数据，可手工加行')
    return
  }
  form.value.steps = d.steps.map((s) => ({
    seq: s.seq,
    process_code: s.process_code,
    process_name: s.process_name,
    op_type: s.op_type,
    category: s.category,
    work_center: s.work_center,
    setup_hours: s.setup_hours,
    unit_hours: s.unit_hours,
    hours_src: s.hours_src,
    samples: s.samples,
    is_key: s.is_key,
    inspect_required: s.inspect_required,
    remark: s.remark,
    not_in_dict: s.not_in_dict,
    uid: nextUid()
  }))
  ElMessage.success(`已按历史生成 ${d.steps.length} 道工序，请核对后保存`)
}

function reorderBySeq() {
  form.value.steps.sort((a, b) => (Number(a.seq) || 0) - (Number(b.seq) || 0))
  ElMessage.success('已按工序号排序')
}

function openCopy() {
  copyVisible.value = true
  copySourceId.value = null
}

async function doCopy() {
  if (!copySourceId.value) return ElMessage.warning('先选一条路线')
  const d = await api.route(copySourceId.value)
  form.value.steps = (d.steps || []).map((s) => ({ ...s, uid: nextUid(), hours_src: 'manual' }))
  copyVisible.value = false
  ElMessage.success(`已引用 ${d.steps.length} 道工序`)
}

// ------------------------------------------------------------------ 批量生成
async function doPreview() {
  const d = await api.batchGenerateRoutes({
    limit: batchLimit.value,
    auto_publish: false,
    dry_run: true
  })
  batchPreview.value = d
}

async function doBatchGenerate() {
  batchBusy.value = true
  try {
    const d = await api.batchGenerateRoutes({
      limit: batchLimit.value,
      auto_publish: batchPublish.value,
      dry_run: false,
      created_by: 'operator'
    })
    ElMessage.success(`已生成 ${d.created_count} 条路线，跳过 ${d.skipped_count} 条`)
    batchVisible.value = false
    batchPreview.value = null
    await loadAll()
  } finally {
    batchBusy.value = false
  }
}

// 用户手动改筛选时，退出「聚焦某个产品」的状态。
// 否则点了「待建 N」之后再去搜索，会被产品过滤挡住搜不到东西。
async function onFilterChange() {
  focusProduct.value = null
  await loadTree()
}

// 点产品行上的「待建 N」→ 只看这个产品还没建路线的件名。
// 再次点击同一个产品则取消筛选。
// 后端按真实产品名过滤（未指定产品的真实名是空串），展开用的 key 是展示名。
async function focusUnbuilt(p) {
  if (focusProduct.value === p.raw_name && onlyUnbuilt.value) {
    focusProduct.value = null
    onlyUnbuilt.value = false
    await loadTree()
    return
  }
  focusProduct.value = p.raw_name
  onlyUnbuilt.value = true
  expanded.value[p.name] = true
  await loadTree()
}

// 上下移动排序（替代原来的拖拽，避免误拖）
function moveStep(index, delta) {
  if (!isDraft.value) return
  const list = form.value.steps
  const to = index + delta
  if (to < 0 || to >= list.length) return
  const row = list[index]
  const other = list[to]
  // 工序号跟着互换，保持「工序号顺序 = 显示顺序」
  const sa = row.seq
  row.seq = other.seq
  other.seq = sa
  list.splice(index, 1)
  list.splice(to, 0, row)
  // 互换后工序号若不再严格递增（同号或倒挂），整列重排成 10/20/30
  const mono = list.every((r, i) => i === 0 || Number(list[i - 1].seq) < Number(r.seq))
  if (!mono) list.forEach((r, i) => { r.seq = (i + 1) * 10 })
}

// ------------------------------------------------------------------ 写入
function openCreate() {
  newRoute.value = { product: currentProduct.value || '', part: currentPart.value || '', generate: true }
  createVisible.value = true
}

async function doCreate() {
  if (!newRoute.value.product || !newRoute.value.part) {
    return ElMessage.warning('产品与件名都要填')
  }
  if (newRoute.value.product === UNSPECIFIED) {
    return ElMessage.warning('这一组还没归属到具体产品，请先在上方输入产品名')
  }
  creating.value = true
  try {
    const d = await api.createRoute({
      product: newRoute.value.product,
      part: newRoute.value.part,
      generate: newRoute.value.generate,
      created_by: 'operator'
    })
    createVisible.value = false
    ElMessage.success('已创建草稿')
    await loadTree()
    await openRoute(d.id)
    currentProduct.value = d.product
    currentPart.value = d.part
  } finally {
    creating.value = false
  }
}

async function save() {
  if (!route.value) return
  const d = await api.updateRoute(route.value.id, {
    title: form.value.title,
    batch_qty: form.value.batch_qty,
    allowance_rate: form.value.allowance_rate,
    completeness: form.value.completeness,
    steps: form.value.steps.map(({ uid, ...rest }) => rest)
  })
  route.value = d
  form.value.steps = (d.steps || []).map((s) => ({ ...s, uid: nextUid() }))
  ElMessage.success('已保存')
  await loadTree()
  await loadCoverage()
}

async function publish(newVersion) {
  if (!route.value) return

  // 发布/升版前先说清影响面：这次动作会换掉全厂核对基准，
  // 引用它的订单不会自动认可新版，要它们自己确认。
  if (!newVersion) {
    const refs = route.value.ref_tasks || []
    if (refs.length) {
      try {
        await ElMessageBox.confirm(
          `发布后这条工艺成为该产品×件名的核对基准，正在使用它的 ${refs.length} 个订单：\n` +
            refs.slice(0, 6).map((t) => `· ${t.task_no}（${t.part || '—'}）`).join('\n') +
            (refs.length > 6 ? `\n… 另有 ${refs.length - 6} 单` : '') +
            '\n\n发布不会自动替这些订单确认新版，它们会亮起「升版待确认」。确定发布？',
          '发布为核对基准',
          { type: 'warning', confirmButtonText: '确定发布' }
        )
      } catch {
        return
      }
    }
  }

  const d = await api.publishRoute(route.value.id, newVersion)
  if (newVersion) {
    ElMessage.success('已升版：生成新版草稿，旧版继续作为基准')
  } else if ((d.affected_tasks || []).length) {
    // 有旧基准被顶掉：把「谁还在旧版上」当场列出来
    ElMessageBox.alert(
      `已发布 v${d.version}（上一版 v${d.previous_version ?? '—'} 归档）。\n` +
        `以下 ${d.affected_count} 个在制订单仍引用旧版，已标记「升版待确认」，需要它们各自确认：\n` +
        d.affected_tasks.slice(0, 8).map((t) => `· ${t.task_no}（${t.part || '—'}）`).join('\n') +
        (d.affected_count > 8 ? `\n… 另有 ${d.affected_count - 8} 单` : '') +
        '\n\n已确认单据需重校验后，告警才会按新基准更新。',
      '升版已生效',
      { type: 'warning', confirmButtonText: '知道了' }
    ).catch(() => {})
  } else {
    ElMessage.success(
      '已发布为核对基准；已确认单据的告警要到「核对工作台」重校验后才会按新基准更新'
    )
  }
  await loadAll()
  // 升版会返回**新记录**（新 id），必须打开新版本而不是原地刷新旧版
  await openRoute(d.id)
  activeVersionId.value = d.id
  // 树里的版本列表也要跟着更新，否则版本下拉还停在旧版
  await loadTree()
  currentProduct.value = d.product
  currentPart.value = d.part
  expanded.value[d.product || UNSPECIFIED] = true
}

// 撤回发布：让已发布的原稿能就地改。会提示该组合暂时没有核对基准。
async function reopen() {
  if (!route.value) return
  await ElMessageBox.confirm(
    '撤回后这条路线回到草稿，改完需重新发布。撤回期间该产品×件名没有核对基准，单据核对不再按它比对。',
    '撤回发布',
    { type: 'warning', confirmButtonText: '撤回', cancelButtonText: '取消' }
  )
  await api.reopenRoute(route.value.id)
  ElMessage.success('已撤回为草稿，可修改后重新发布')
  await loadAll()
  await openRoute(route.value.id)
}

// 撤销归档：误点归档的救援入口
async function restore(to = 'draft') {
  if (!route.value) return
  const label = to === 'released' ? '已发布' : '草稿'
  await ElMessageBox.confirm(`把这条归档版本恢复为${label}？`, '撤销归档', {
    type: 'info',
    confirmButtonText: '恢复'
  })
  const d = await api.restoreRoute(route.value.id, to)
  ElMessage.success(`已恢复为${label}`)
  await loadAll()
  await openRoute(d.id)
  await loadTree()
}

async function archive() {
  if (!route.value) return
  await ElMessageBox.confirm('归档后这条路线退出当前基准，可随时恢复。确定归档？', '归档', {
    type: 'warning'
  })
  await api.archiveRoute(route.value.id)
  ElMessage.success('已归档，可在版本下拉里恢复')
  await loadAll()
  await openRoute(route.value.id)
}

async function remove() {
  if (!route.value) return
  const what = route.value.status === 'archived' ? '这条归档版本' : '这条草稿'
  await ElMessageBox.confirm(`删除后不可恢复，确定删除${what}？`, '删除', { type: 'warning' })
  await api.deleteRoute(route.value.id)
  ElMessage.success('已删除')
  route.value = null
  form.value = { title: '', batch_qty: 0, allowance_rate: 0, completeness: 'partial', steps: [] }
  await loadAll()
}

// 从「工时组合」台账点「建线」跳过来：?product=&part=
// 直接定位到那个组合并展开，省得再在 117 个产品里找
async function jumpFromQuery() {
  const product = String(rt.query.product || '')
  const part = String(rt.query.part || '')
  if (!part) return
  const pname = product || UNSPECIFIED
  kw.value = part
  await loadTree()
  const node = tree.value.find((p) => p.name === pname)
  if (!node) return
  expanded.value[pname] = true
  const partNode = node.parts.find((x) => x.part === part)
  if (partNode) {
    await select(pname, partNode)
  } else {
    // 还没建线：把右侧清空，让人点新建
    currentProduct.value = product
    currentPart.value = part
    route.value = null
  }
}

onMounted(async () => {
  await loadAll()
  await jumpFromQuery()
})
</script>

<style scoped>
/* 顶部一行摘要：替代原先 6 张 KPI 卡，高度从 ~100px 压到 34px */
.summary-bar {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 7px 14px;
  margin-bottom: 12px;
  background: #fff;
  border: 1px solid #e7ebf1;
  border-radius: 8px;
  font-size: 12.5px;
}

.sb-item { display: inline-flex; align-items: baseline; gap: 5px; }
.sb-item b { font-size: 14px; font-weight: 600; font-variant-numeric: tabular-nums; }
.sb-item b.warn { color: #d9822b; }
.sb-note { font-size: 11.5px; }

.route-wrap {
  display: grid;
  grid-template-columns: minmax(300px, 340px) minmax(0, 1fr);
  gap: 14px;
  align-items: start;
}

.tree-card { padding: 14px 14px 10px; }

.tree-tools { margin-bottom: 8px; gap: 8px; }

/* 树占满可视区：页面去掉两块卡后，树的可用高度明显变大了。
   scrollbar-gutter: stable 让滚动条槽位常驻：否则产品展开时行宽被挤窄 15px，
   件名会跟着左右窜，看起来像整块在漂。 */
.tree-body {
  max-height: calc(100vh - 276px);
  overflow: auto;
  scrollbar-gutter: stable;
}

.task-picker {
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-height: 190px;
  overflow: auto;
  margin-bottom: 14px;
}
.task-pick {
  border: 1px solid #e7ebf1;
  border-radius: 8px;
  padding: 7px 10px;
  cursor: pointer;
  background: #fbfcfe;
}
.task-pick.active { border-color: #1f6fd0; background: #f1f6fd; }
.task-pick-no { font-size: 12.5px; font-weight: 600; color: #2b3444; }
.task-pick-meta { font-size: 11.5px; margin-top: 2px; }

.task-block { margin-bottom: 16px; }
.task-block-title {
  font-size: 12.5px;
  font-weight: 600;
  color: #2b3444;
  margin-bottom: 8px;
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.task-sub { font-size: 11.5px; font-weight: 400; color: #8a93a3; }
.task-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 6px 14px;
  font-size: 12px;
}
.task-grid .muted { margin-right: 6px; }
.task-hint {
  font-size: 12px;
  color: #8a5a00;
  background: #fff8e8;
  border: 1px solid #f3e0b5;
  border-radius: 6px;
  padding: 7px 10px;
}
.task-same {
  font-size: 12px;
  color: #3b6d11;
  background: #f2f8ea;
  border: 1px solid #d6e8bd;
  border-radius: 6px;
  padding: 7px 10px;
}
.task-diff {
  margin: 0;
  padding-left: 18px;
  font-size: 12px;
  line-height: 1.75;
  color: #444b58;
}
.task-total { margin-top: 6px; font-size: 11.5px; }

.editor-card { min-height: 420px; }

.batch-preview {
  margin-top: 8px;
  padding: 10px 12px;
  background: #f7f9fc;
  border: 1px solid #eaeef4;
  border-radius: 8px;
  font-size: 12.5px;
}

.batch-list { margin-top: 6px; max-height: 160px; overflow: auto; }

.batch-item {
  padding: 2px 0;
  color: #4b5565;
  border-bottom: 1px dashed #eaeef4;
}
.batch-item:last-child { border-bottom: none; }

.meta-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}

.meta-label { font-size: 12.5px; color: #7a8290; }
.meta-hint { font-size: 11.5px; color: #a4acb8; }

.step-tools { margin-bottom: 8px; }

.step-table :deep(.cell) { padding: 4px 6px; }

.cell-sub {
  font-size: 11px;
  color: #9aa3b2;
  line-height: 1.3;
  font-variant-numeric: tabular-nums;
}
.cell-sub.warn { color: #d9822b; }

/* 上下移按钮：一格内并排居中，不撑高行 */
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

/* 工序名加粗加黑，一眼看清每行是什么工序 */
.step-table :deep(.proc-cell .el-input__inner),
.step-table :deep(.proc-cell input),
.step-table :deep(.proc-cell .el-select__selected-item:not(.is-transparent)),
.step-table :deep(.proc-cell .el-select__placeholder:not(.is-transparent)) {
  font-weight: 700;
  color: #11151b;
  -webkit-text-fill-color: #11151b;
}

.sum-bar {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
  margin-top: 12px;
  padding: 10px 12px;
  background: #f7f9fc;
  border: 1px solid #eaeef4;
  border-radius: 8px;
  font-size: 12.5px;
}

/* 路线状态说明：能不能改、能不能被引用全看这一行 */
.state-tip {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 12.5px;
  line-height: 1.7;
  border-radius: 6px;
  padding: 8px 12px;
  margin-bottom: 10px;
}
.state-tip b { font-weight: 600; white-space: nowrap; }
.state-draft { background: #fdf6e8; border: 1px solid #f2e2bf; color: #8a6212; }
.state-draft b { color: #7a5306; }
.state-released { background: #eef7ef; border: 1px solid #cfe8d4; color: #2f6b3c; }
.state-released b { color: #1f5b2c; }
.state-archived { background: #f4f5f7; border: 1px solid #e3e6eb; color: #5a6474; }

/* 升版影响条：改动过基准之后，这条路线本身也要把话说清楚 */
.impact-tip {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  font-size: 12.5px;
  line-height: 1.7;
  border-radius: 6px;
  padding: 6px 12px;
  margin-bottom: 10px;
  background: #fdf3f2;
  border: 1px solid #f0c2bc;
  color: #7a4a42;
}
.impact-badge {
  background: #c0392b;
  color: #fff;
  border-radius: 3px;
  padding: 1px 6px;
  font-size: 11.5px;
  font-weight: 600;
  white-space: nowrap;
}
.impact-pending { color: #a52a1c; font-weight: 600; }

/* 引用订单抽屉里的升版提醒 */
.task-notice {
  margin-bottom: 14px;
  padding: 8px 12px;
  border: 1px solid #f0c2bc;
  background: #fdf3f2;
  border-radius: 6px;
  font-size: 12.5px;
  line-height: 1.7;
}
.task-notice-title { font-weight: 600; color: #a52a1c; margin-bottom: 4px; }
.task-notice-row { display: flex; gap: 6px; color: #7a4a42; }
.task-notice-row b { color: #1f2329; white-space: nowrap; }
.task-notice-foot { margin-top: 4px; font-size: 11.5px; color: #9c7c76; }

/* 树干上的副本标记：橙色，与紫色的「引用单数」一眼分开 */

/* 副本抽屉 */
.copy-head { display: flex; flex-direction: column; gap: 3px; }
.copy-head b { font-size: 15px; color: #1f2329; }
.copy-head .muted { font-size: 12px; line-height: 1.6; }
.copy-tools { gap: 10px; margin-bottom: 10px; align-items: center; }
.op-cell { display: flex; align-items: center; gap: 2px; }
.op-cell :deep(.el-button + .el-button) { margin-left: 0; }

.pd-box {
  border: 1px solid #eaeef4;
  border-radius: 8px;
  padding: 10px 12px;
  background: #fafbfd;
}
.pd-sum { font-size: 13px; margin-bottom: 8px; }
.pd-list { max-height: 220px; overflow: auto; }
.pd-row {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  font-size: 12px;
  padding: 3px 0;
  border-bottom: 1px dashed #eef1f6;
}
.pd-row:last-child { border-bottom: none; }
.pd-skipped { margin-top: 8px; padding-top: 8px; border-top: 1px solid #eaeef4; }
.ok { color: #1f8b4c; }
.warn { color: #c0392b; }
</style>
