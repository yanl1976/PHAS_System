<template>
  <div class="page">
    <div class="page-head">
      <span class="page-title">生产任务</span>
      <span class="page-sub">
        一个订单 = 一个任务。任务下挂多条工艺路线，路线默认<b>引用</b>已有基线（多张任务共用一份，改标准一起变），只有确实要为本单改工艺时才拆副本
      </span>
      <div class="spacer"></div>
      <el-button size="small" type="primary" @click="openCreate">
        <el-icon><Plus /></el-icon>新建任务
      </el-button>
      <el-button size="small" @click="rebuild" :loading="rebuilding">按单据重建</el-button>
      <el-button size="small" @click="reload">刷新</el-button>
    </div>

    <!-- 业务待办：规则产生的缺口必须有人看见，否则规则等于没定 -->
    <div v-if="todos.items.length" class="todo-bar">
      <span class="todo-label">待办</span>
      <button
        v-for="t in todos.items"
        :key="t.key"
        class="todo-item"
        :class="`todo-${t.level}`"
        :title="t.desc"
        @click="$router.push(t.link)"
      >
        {{ t.title }} <b class="mono">{{ t.count }}</b>
      </button>
    </div>

    <div class="summary-bar">
      <span class="sb-item"><span class="muted">任务</span><b class="mono">{{ summary.task_count ?? 0 }}</b></span>
      <span class="sb-sep"></span>
      <span class="sb-item"><span class="muted">在制</span><b class="mono">{{ summary.open ?? 0 }}</b></span>
      <span class="sb-item"><span class="muted">已挂路线</span><b class="mono">{{ summary.with_route ?? 0 }}</b></span>
      <span class="sb-item" :class="{ warn: summary.no_route }">
        <span class="muted">待补路线</span><b class="mono">{{ summary.no_route ?? 0 }}</b>
      </span>
      <span
        class="sb-item"
        :class="{ warn: summary.stale_tasks }"
        title="这些任务引用的工艺基线已经升版，或引用指向了非现行版本，还没人确认"
      >
        <span class="muted">升版待确认</span><b class="mono">{{ summary.stale_tasks ?? 0 }}</b>
      </span>
      <span class="sb-sep"></span>
      <span class="sb-item"><span class="muted">实报工时</span><b class="mono">{{ (summary.actual_hours || 0).toFixed(2) }}</b></span>
      <span
        class="sb-item"
        :class="{ warn: summary.pending_hours }"
        title="待核对单据还没入账。汇总页只认已确认，所以这部分单独显示，不混进实报"
      >
        <span class="muted">待核对</span><b class="mono">{{ (summary.pending_hours || 0).toFixed(2) }}</b>
      </span>
      <div class="spacer"></div>
      <span class="sb-note muted">实报 = 已确认单据的工时合计；待核对单独列</span>
    </div>

    <div class="card">
      <div class="row-flex tools">
        <el-input
          v-model="kw"
          size="small"
          placeholder="搜索任务号 / 标题 / 客户 / 件名"
          clearable
          style="width: 250px"
          @keyup.enter="reload"
          @clear="reload"
        />
        <el-select v-model="state" size="small" style="width: 118px" @change="reload">
          <el-option value="" label="全部状态" />
          <el-option value="open" label="在制" />
          <el-option value="closed" label="已结案" />
          <el-option value="draft" label="草稿" />
        </el-select>
        <el-checkbox v-model="onlyNoRoute" size="small" @change="reload">只看待补路线</el-checkbox>
        <el-checkbox v-model="onlyNoSheet" size="small" @change="reload">只看无单据</el-checkbox>
        <el-button size="small" type="primary" @click="reload">查询</el-button>
        <div class="spacer"></div>
        <span class="muted">共 {{ total }} 条</span>
      </div>

      <el-table
        :data="rows"
        size="small"
        v-loading="loading"
        style="width: 100%"
        height="calc(100vh - 326px)"
      >
        <el-table-column label="任务号" min-width="200" show-overflow-tooltip>
          <template #default="{ row }">
            <div class="no-cell">
              <span class="task-no" @click="open(row)">{{ row.task_no }}</span>
              <!-- 工艺基线升过版、这单还没人认：任务号旁边就必须看得见 -->
              <el-tag
                v-if="row.notice_count"
                size="small"
                type="danger"
                effect="dark"
                class="notice-chip"
                :title="row.notice_msg"
              >
                升版待确认 {{ row.notice_count }}
              </el-tag>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="订单 / 标题" min-width="150" show-overflow-tooltip>
          <template #default="{ row }">
            <span>{{ row.title || '—' }}</span>
            <div v-if="row.customer" class="muted" style="font-size: 11.5px">{{ row.customer }}</div>
          </template>
        </el-table-column>
        <el-table-column label="产品" min-width="140" show-overflow-tooltip>
          <template #default="{ row }">
            <span v-if="row.product">{{ row.product }}</span>
            <el-tag v-else size="small" type="danger" effect="plain">未认领</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="工艺路线" align="right" width="130">
          <template #default="{ row }">
            <span v-if="row.route_count" class="mono">{{ row.route_count }} 条 · {{ row.step_count }} 道</span>
            <el-tag v-else size="small" type="warning" effect="plain">待补</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="单件工时" align="right" width="96">
          <template #default="{ row }">
            <span class="mono">{{ row.route_hours ? row.route_hours.toFixed(2) : '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="实报" align="right" width="118">
          <template #default="{ row }">
            <span class="mono">{{ (row.actual_hours || 0).toFixed(2) }}</span>
            <el-tooltip
              v-if="row.pending_hours"
              :content="`另有 ${row.pending_count} 张待核对单据 ${row.pending_hours} 小时未入账`"
              placement="top"
            >
              <span class="pending-hint mono">+{{ row.pending_hours.toFixed(2) }}</span>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column label="单据" align="center" width="86">
          <template #default="{ row }">
            <span class="mono">{{ row.sheet_count || 0 }}</span>
            <span v-if="row.pending_count" class="pending-hint" :title="`${row.pending_count} 张待核对`">+{{ row.pending_count }}</span>
          </template>
        </el-table-column>
        <el-table-column label="交期" width="104">
          <template #default="{ row }">{{ row.due_date || '—' }}</template>
        </el-table-column>
        <el-table-column label="状态" width="86" align="center">
          <template #default="{ row }">
            <el-tag size="small" :type="statusType(row.status)" effect="plain">{{ statusText(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="版本" width="76" align="center">
          <template #default="{ row }">
            <!-- 版本号 = 口径改过几次。v1 就是建单原样，没有 hover 提示 -->
            <span
              class="ver-chip mono"
              :class="{ revised: row.version > 1 }"
              :title="versionTip(row)"
            >
              v{{ row.version || 1 }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="132" fixed="right">
          <template #default="{ row }">
            <el-button size="small" text type="primary" @click="open(row)">详情</el-button>
            <el-button size="small" text @click="openEditTask(row)">编辑</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <!-- 新建任务 -->
    <el-dialog v-model="createVisible" title="新建生产任务" width="660px">
      <el-form :model="form" label-width="86px" size="small">
        <div class="form-grid">
          <el-form-item label="任务号">
            <el-input v-model="form.task_no" placeholder="留空自动生成：产品代号-批次-期间" />
          </el-form-item>
          <el-form-item label="期间">
            <el-select v-model="form.period_id" placeholder="选择期间" clearable style="width: 100%">
              <el-option v-for="p in periods" :key="p.id" :value="p.id" :label="`${p.ym}${p.status === 'locked' ? '（已锁定）' : ''}`" />
            </el-select>
          </el-form-item>
          <el-form-item label="产品">
            <el-input v-model="form.product" placeholder="产品名或代号，字典认不出会进待认领" />
          </el-form-item>
          <el-form-item label="批次">
            <el-input v-model="form.batch_no" placeholder="如 26-1" />
          </el-form-item>
          <el-form-item label="数量">
            <el-input-number v-model="form.qty" :min="0" :precision="0" controls-position="right" style="width: 100%" />
          </el-form-item>
          <el-form-item label="交期">
            <el-date-picker v-model="form.due_date" type="date" value-format="YYYY-MM-DD" placeholder="选择日期" style="width: 100%" />
          </el-form-item>
          <el-form-item label="客户">
            <el-input v-model="form.customer" placeholder="选填" />
          </el-form-item>
          <el-form-item label="标题">
            <el-input v-model="form.title" placeholder="选填，默认按产品+批次生成" />
          </el-form-item>
        </div>
        <el-form-item label="备注">
          <el-input v-model="form.remark" type="textarea" :rows="2" placeholder="选填" />
        </el-form-item>
      </el-form>
      <div class="hint-box">
        任务建好后可以一键把已有工艺路线挂进来 —— 引用而不是复制，同一份标准工艺被多个订单共用。
      </div>
      <template #footer>
        <el-button size="small" @click="createVisible = false">取消</el-button>
        <el-button size="small" type="primary" :loading="creating" @click="submitCreate">创建</el-button>
      </template>
    </el-dialog>

    <!-- 任务详情：只允许右上角关闭，点空白/按 ESC 都不关，避免做到一半误触清空 -->
    <el-drawer
      v-model="drawer"
      size="1140px"
      destroy-on-close
      class="task-drawer"
      :close-on-click-modal="false"
      :close-on-press-escape="false"
    >
      <template #header>
        <div class="tk-head">
          <div class="tk-head-line">
            <span class="tk-no">{{ detail ? detail.task_no : '任务详情' }}</span>
            <span v-if="detail && detail.title" class="tk-name">{{ detail.title }}</span>
            <el-tag v-if="detail" size="small" :type="statusType(detail.status)" effect="plain">
              {{ statusText(detail.status) }}
            </el-tag>
            <!-- 版本号：点开看改过什么，改口径必须有人认得出「这单现在第几版」 -->
            <button
              v-if="detail"
              class="ver-chip mono"
              :class="{ revised: (detail.version || 1) > 1 }"
              :title="versionTip(detail)"
              @click="openRevisions(detail)"
            >
              v{{ detail.version || 1 }}
            </button>
            <span class="spacer"></span>
            <el-button v-if="detail" size="small" type="primary" plain @click="openEditTask(detail)">
              编辑基本信息
            </el-button>
            <el-button v-if="detail && detail.revisions && detail.revisions.length" size="small" text @click="openRevisions(detail)">
              修订史 {{ detail.revisions.length }}
            </el-button>
            <span v-if="detail" class="tk-src">{{ sourceText(detail.source) }}</span>
          </div>

          <!-- 表头即「新建任务」时填写的基本信息，标签与值一律左右排列 -->
          <div v-if="detail" class="task-grid">
            <div><span class="k">产品</span><b class="v">{{ detail.product || '未认领' }}</b></div>
            <div><span class="k">订单数量</span><b class="v mono">{{ detail.qty || '—' }}</b></div>
            <div><span class="k">客户</span><b class="v">{{ detail.customer || '—' }}</b></div>
            <div><span class="k">批次</span><b class="v">{{ detail.batch_no || '—' }}</b></div>
            <div><span class="k">期间</span><b class="v mono">{{ detail.period_ym || '—' }}</b></div>
            <div><span class="k">交期</span><b class="v mono">{{ detail.due_date || '—' }}</b></div>
            <div>
              <span class="k">路线 / 工序</span>
              <b class="v mono">{{ detail.summary.route_count }} 条 / {{ detail.summary.step_count }} 道</b>
            </div>
            <div><span class="k">单件工时</span><b class="v mono">{{ detail.summary.unit_hours }}h</b></div>
            <div><span class="k">实报工时</span><b class="v mono">{{ detail.summary.actual_hours }}h</b></div>
            <div>
              <span class="k">待核对</span>
              <b class="v mono" :class="{ 'pending-hint': detail.summary.pending_hours }">
                {{ detail.summary.pending_count || 0 }} 张 / {{ detail.summary.pending_hours || 0 }}h
              </b>
            </div>
            <div><span class="k">本单副本</span><b class="v mono">{{ detail.summary.copy_count }} 条</b></div>
            <div><span class="k">归属单据</span><b class="v mono">{{ detail.sheet_count }} 张</b></div>
            <div>
              <span class="k">状态</span>
              <el-select v-model="detail.status" size="small" style="width: 104px" @change="changeStatus">
                <el-option value="draft" label="草稿" />
                <el-option value="open" label="在制" />
                <el-option value="closed" label="已结案" />
                <el-option value="cancelled" label="已取消" />
              </el-select>
            </div>
          </div>
          <!-- 升版提醒：放在表头正下方，进来第一眼就要看到，不能藏在路线表某个格子里 -->
          <div v-if="detail && detail.notices && detail.notices.length" class="tk-notice">
            <div class="tk-notice-head">
              <span class="tk-notice-badge">基线已升版</span>
              <b>本单有 {{ detail.notices.length }} 条引用待确认</b>
              <span class="tk-notice-hint">
                未确认前，这张单的定额到底按哪一版核对是说不清的
              </span>
              <div class="spacer"></div>
              <el-button size="small" :loading="acking" @click="ackAll('keep')">全部保留现版</el-button>
              <el-button size="small" type="primary" :loading="acking" @click="ackAll('adopt')">
                全部采用新版
              </el-button>
            </div>
            <div v-for="n in detail.notices" :key="n.task_route_id" class="tk-notice-row">
              <span class="tk-notice-part">{{ n.part || '—' }}</span>
              <el-tag size="small" type="danger" effect="plain">{{ n.label }}</el-tag>
              <span class="tk-notice-msg">{{ n.message }}</span>
              <div class="spacer"></div>
              <el-button size="small" text type="primary" @click="ackOne(n, 'adopt')">采用新版</el-button>
              <el-button size="small" text @click="ackOne(n, 'keep')">保留现版</el-button>
            </div>
            <div class="tk-notice-foot">
              「采用新版」= 本单改用现行工艺；「保留现版」= 按本单当初开工的那一版固化为副本，
              从此与基线脱钩，不再被后续升版影响。
            </div>
          </div>
          <div v-if="taskLocked && detail" class="tk-locked">
            <b>{{ detail.status === 'closed' ? '已结案' : '已取消' }}</b>
            <span>工艺路线已锁定：不能再增删改，也不能拆副本。如需调整，请先把状态改回「在制」。</span>
          </div>
          <div v-if="detail && detail.remark" class="tk-remark">备注：{{ detail.remark }}</div>
        </div>
      </template>

      <div v-if="!detail" class="muted">加载中…</div>
      <template v-else>
        <div class="block-head">
          <span class="block-title">工艺路线</span>
          <span class="muted">引用 = 与标准同步；副本 = 只影响本单</span>
          <div class="spacer"></div>
          <el-button size="small" type="primary" :disabled="taskLocked" @click="openPicker">从已有路线添加</el-button>
        </div>
        <el-table
          :data="detail.routes.filter((r) => r.status === 'active')"
          size="small"
          max-height="300"
          :row-class-name="routeRowClass"
        >
          <el-table-column label="件名" min-width="190" show-overflow-tooltip>
            <template #default="{ row }">
              <span>{{ row.part || '—' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="来源" min-width="108" align="center">
            <template #default="{ row }">
              <el-tag v-if="row.origin === 'baseline'" size="small" type="success" effect="plain">引用基线</el-tag>
              <el-tag v-else-if="row.origin === 'copy'" size="small" type="warning" effect="plain">本单副本</el-tag>
              <el-tag v-else size="small" type="info" effect="plain">手建</el-tag>
              <div v-if="row.origin === 'baseline' && row.ref_count > 1" class="cell-sub">
                {{ row.ref_count }} 单共用
              </div>
              <div v-if="row.stale" class="cell-warn">{{ row.stale.label }}</div>
            </template>
          </el-table-column>
          <el-table-column label="工序" width="82" align="right">
            <template #default="{ row }"><span class="mono">{{ row.step_count }}</span></template>
          </el-table-column>
          <el-table-column label="单件" min-width="108" align="right">
            <template #default="{ row }"><span class="mono">{{ row.unit_hours }}</span></template>
          </el-table-column>
          <el-table-column label="实报" min-width="108" align="right">
            <template #default="{ row }"><span class="mono">{{ row.actual_hours }}</span></template>
          </el-table-column>
          <el-table-column label="引用 / 现行" width="128" align="center">
            <template #default="{ row }">
              <span class="mono">v{{ row.route_version ?? '—' }}</span>
              <span
                v-if="row.stale && row.stale.current_version && row.stale.current_version !== row.route_version"
                class="ver-arrow mono"
              >
                → v{{ row.stale.current_version }}
              </span>
              <div v-if="!row.has_baseline" class="cell-sub">
                <el-tag size="small" type="warning" effect="plain">未建</el-tag>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="差异" width="84" align="center">
            <template #default="{ row }">
              <span v-if="!row.diff" class="muted">—</span>
              <span v-else-if="row.diff.identical" class="ok">一致</span>
              <span v-else class="warn">{{ row.diff.delta_total > 0 ? '+' : '' }}{{ row.diff.delta_total }}h</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="288" fixed="right">
            <template #default="{ row }">
              <div class="op-cell">
                <el-button size="small" text type="primary" :disabled="taskLocked" @click="openEdit(row)">编辑</el-button>
                <el-button size="small" text type="primary" @click="showRoute(row)">查看</el-button>
                <el-button
                  v-if="row.origin === 'copy'"
                  size="small"
                  text
                  type="success"
                  :disabled="taskLocked"
                  @click="promoteRoute(row)"
                >
                  进阶
                </el-button>
                <el-button v-else size="small" text :disabled="taskLocked" @click="forkRoute(row)">拆副本</el-button>
                <el-button size="small" text type="danger" :disabled="taskLocked" @click="removeRoute(row)">移除</el-button>
              </div>
            </template>
          </el-table-column>
        </el-table>

        <div class="block-head">
          <span class="block-title">单据</span>
          <span class="muted">{{ detail.sheets.length }} 张</span>
        </div>
        <el-table :data="detail.sheets" size="small" max-height="200">
          <el-table-column label="#" width="76">
            <template #default="{ row }">
              <el-link type="primary" :underline="false" @click="$router.push(`/review/${row.id}`)">#{{ row.id }}</el-link>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="96">
            <template #default="{ row }">{{ row.status === 'verified' ? '已确认' : row.status }}</template>
          </el-table-column>
          <el-table-column label="明细" width="80" align="right">
            <template #default="{ row }">{{ row.item_count }}</template>
          </el-table-column>
          <el-table-column label="工时" width="100" align="right">
            <template #default="{ row }"><span class="mono">{{ row.total_hours }}</span></template>
          </el-table-column>
          <el-table-column label="确认人 / 时间" min-width="190">
            <template #default="{ row }">{{ row.verify_user || '—' }} {{ fmtTime(row.verify_at) }}</template>
          </el-table-column>
          <el-table-column label="归属" width="88" align="center">
            <template #default="{ row }">
              <!-- 在这个任务里发现某张单不该在这，就地挪走，不必回台账找 -->
              <el-button size="small" text type="primary" @click="openSheetMove(row)">改挂</el-button>
            </template>
          </el-table-column>
        </el-table>

        <div class="hint-box" style="margin-top: 12px">
          任务下的路线是引用关系：改基线的定额，所有引用它的任务立即同步；如果这张单的工艺确实跟标准不一样，先「拆副本」再改，两边从此互不影响。
        </div>
      </template>
    </el-drawer>

    <!-- 编辑任务基本信息：口径字段改了要升版并留痕，不跳页 -->
    <TaskEditDialog v-model="taskEditVisible" :task="taskEditTarget" @saved="onTaskSaved" />

    <!-- 修订史：谁在什么时候把这单的什么改了 -->
    <el-dialog v-model="revisionsVisible" width="840px" class="revision-dialog">
      <template #header>
        <div class="rv-head">
          <span class="rv-title">修订历史</span>
          <span v-if="revisionsOf" class="rv-no mono">{{ revisionsOf.task_no }}</span>
          <el-tag size="small" effect="plain">当前 v{{ revisionsOf?.version || 1 }}</el-tag>
        </div>
      </template>
      <div v-if="!revisionRows.length" class="muted">
        这个任务自建单以来没有改过口径字段，仍是 v1。
      </div>
      <el-table v-else :data="revisionRows" size="small" max-height="420">
        <el-table-column label="版本" width="72" align="center">
          <template #default="{ row }">
            <span class="ver-chip mono revised">v{{ row.version }}</span>
          </template>
        </el-table-column>
        <el-table-column label="时间" width="160">
          <template #default="{ row }"><span class="mono">{{ fmtTime(row.created_at) }}</span></template>
        </el-table-column>
        <el-table-column label="操作人" width="96" prop="user" />
        <el-table-column label="改动" min-width="300">
          <template #default="{ row }">
            <div v-for="(c, i) in row.changes" :key="i" class="rv-change">
              <span class="rv-field">{{ c.label }}</span>
              <span class="rv-old">{{ c.old || '—' }}</span>
              <span class="rv-arrow">→</span>
              <span class="rv-new">{{ c.new || '—' }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="说明" min-width="150" show-overflow-tooltip>
          <template #default="{ row }">
            <span v-if="row.note">{{ row.note }}</span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
      </el-table>
      <div class="hint-box" style="margin-top: 10px">
        v1 是建单时的原样。任务号、订单名、客户、数量、下单日期、交期、核算期间每改一次升一版；
        改状态与备注不升版（它们是流程动作与说明，不是口径）。
      </div>
      <template #footer>
        <el-button size="small" @click="revisionsVisible = false">关闭</el-button>
      </template>
    </el-dialog>

    <!-- 编辑工序：抽屉内弹窗，不跳页，保存后回到抽屉 -->
    <RouteEditDialog v-model="editVisible" :task-id="detail?.id || null" :tr="editRow" @saved="onRouteSaved" />

    <!-- 单据改挂：任务侧的第二入口，发现归错就地挪走 -->
    <SheetTaskDialog
      v-model="sheetMoveVisible"
      :sheet-ids="sheetMoveRow ? [sheetMoveRow.id] : []"
      :current="detail ? { id: detail.id, task_no: detail.task_no } : null"
      :sheet-period="detail?.period_ym || ''"
      @done="onSheetMoved"
    />

    <!-- 选已有路线 -->
    <el-dialog v-model="pickerVisible" title="从已有工艺路线添加" width="820px">
      <div class="row-flex tools" style="margin-bottom: 8px">
        <el-input v-model="pickerKw" size="small" placeholder="搜索产品 / 件名" clearable style="width: 220px" @keyup.enter="loadCandidates" @clear="loadCandidates" />
        <el-button size="small" @click="loadCandidates">查询</el-button>
        <div class="spacer"></div>
        <span class="muted">已选 {{ picked.length }} 条</span>
      </div>
      <!-- 只有已发布路线能被引用；这里把「还有多少个组合只有草稿」讲清楚，
           否则用户会以为系统漏了数据 -->
      <div v-if="candidateHint" class="picker-hint">{{ candidateHint }}</div>
      <el-table :data="candidates" size="small" max-height="420" @selection-change="onPick">
        <el-table-column type="selection" width="46" :selectable="(row) => !row.in_task" />
        <el-table-column label="产品" min-width="150" show-overflow-tooltip prop="product" />
        <el-table-column label="件名" min-width="130" show-overflow-tooltip prop="part" />
        <el-table-column label="工序" width="70" align="right" prop="step_count" />
        <el-table-column label="单件" width="84" align="right">
          <template #default="{ row }"><span class="mono">{{ row.unit_hours }}</span></template>
        </el-table-column>
        <el-table-column label="版本" width="96" align="center">
          <template #default="{ row }">
            <span class="mono">v{{ row.version }}</span>
            <span class="muted"> {{ row.status === 'released' ? '已发布' : '草稿' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="已被引用" width="92" align="right">
          <template #default="{ row }"><span class="mono">{{ row.ref_count }}</span></template>
        </el-table-column>
        <el-table-column label="" width="86" align="center">
          <template #default="{ row }">
            <el-tag v-if="row.in_task" size="small" type="info" effect="plain">已在任务</el-tag>
          </template>
        </el-table-column>
      </el-table>
      <template #footer>
        <el-button size="small" @click="pickerVisible = false">取消</el-button>
        <el-button size="small" type="primary" :disabled="!picked.length" :loading="adding" @click="submitPick">
          添加到任务（{{ picked.length }}）
        </el-button>
      </template>
    </el-dialog>

    <!-- 路线工序 -->
    <el-dialog v-model="routeVisible" :title="routeTitle" width="720px">
      <div v-if="!routeSteps.length" class="muted">这条路线还没有工序。</div>
      <el-table v-else :data="routeSteps" size="small" max-height="420">
        <el-table-column prop="seq" label="#" width="50" />
        <el-table-column prop="process_name" label="工序" min-width="120" />
        <el-table-column prop="category" label="类别" width="100" />
        <el-table-column label="单件" width="86" align="right">
          <template #default="{ row }"><span class="mono">{{ row.unit_hours }}</span></template>
        </el-table-column>
        <el-table-column label="准备" width="80" align="right">
          <template #default="{ row }"><span class="mono">{{ row.setup_hours }}</span></template>
        </el-table-column>
        <el-table-column prop="hours_src" label="工时来源" width="110" />
      </el-table>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api'
import RouteEditDialog from '../components/RouteEditDialog.vue'
import TaskEditDialog from '../components/TaskEditDialog.vue'
import SheetTaskDialog from '../components/SheetTaskDialog.vue'

const route = useRoute()
const rows = ref([])
const detail = ref(null)
// 已结案/已取消：活已经干完或作废，再改它引用的工艺，
// 「当初按哪版做的」就再也答不上来了
const taskLocked = computed(() => ['closed', 'cancelled'].includes(detail.value?.status))
// 业务待办：规则产生的缺口（未发布工艺被引用、任务无路线、可合并副本…）
const todos = ref({ total: 0, item_count: 0, items: [] })
const total = ref(0)
const summary = ref({})
const loading = ref(false)
const rebuilding = ref(false)
const kw = ref('')
const state = ref('')
const onlyNoRoute = ref(false)
const onlyNoSheet = ref(false)

const drawer = ref(false)

const editVisible = ref(false)
const editRow = ref(null)
// 单据改挂：归集是自动的，归错了从任务侧直接挪走
const sheetMoveVisible = ref(false)
const sheetMoveRow = ref(null)

function openSheetMove(row) {
  sheetMoveRow.value = row
  sheetMoveVisible.value = true
}

async function onSheetMoved() {
  // 单据数、实报工时都是任务上的物化字段，挪走之后必须重新取详情
  if (detail.value) detail.value = await api.task(detail.value.id)
  await reload()
}

const periods = ref([])
const createVisible = ref(false)
const creating = ref(false)
const form = ref(emptyForm())

const pickerVisible = ref(false)
const pickerKw = ref('')
const candidates = ref([])
const candidateHint = ref('')
const picked = ref([])
const adding = ref(false)

const routeVisible = ref(false)
const routeTitle = ref('')
const routeSteps = ref([])

function emptyForm() {
  return {
    task_no: '',
    title: '',
    customer: '',
    product: '',
    batch_no: '',
    period_id: null,
    qty: 0,
    due_date: '',
    remark: ''
  }
}

async function reload() {
  loading.value = true
  try {
    const d = await api.tasks({
      q: kw.value,
      status: state.value,
      only_no_route: onlyNoRoute.value,
      only_no_sheet: onlyNoSheet.value,
      size: 500
    })
    rows.value = d.items || []
    total.value = d.total || 0
    summary.value = d.summary || {}
    // 待办跟主列表一起刷新：分开刷会出现「列表变了待办没变」的错觉
    loadTodos()
  } finally {
    loading.value = false
  }
}

async function loadTodos() {
  try {
    todos.value = await api.taskTodos()
  } catch {
    todos.value = { total: 0, item_count: 0, items: [] }
  }
}

async function open(row) {
  drawer.value = true
  detail.value = null
  detail.value = await api.task(row.id)
}

// ---------------------------------------------------------------- 升版提醒
// 提醒不能只挂在页面角标上：任何「保存/提交」动作都要再问一次，
// 否则人会以为点完保存就等于确认了新版工艺。
const acking = ref(false)

function routeRowClass({ row }) {
  return row.stale ? 'row-stale' : ''
}

async function confirmVersionNotice(action) {
  const list = detail.value?.notices || []
  if (!list.length) return true
  const lines = list
    .slice(0, 5)
    .map((n) => `· ${n.part || '—'}：${n.message}`)
    .join('\n')
  const more = list.length > 5 ? `\n… 另有 ${list.length - 5} 条` : ''
  try {
    await ElMessageBox.confirm(
      `本单有 ${list.length} 条工艺引用「基线已升版、尚未确认」：\n${lines}${more}\n\n` +
        `${action}不会消除这些提醒；建议先在表头用「采用新版 / 保留现版」处理。仍要继续吗？`,
      '升版待确认',
      { type: 'warning', confirmButtonText: '仍然继续', cancelButtonText: '先去确认' }
    )
    return true
  } catch {
    return false
  }
}

async function ackOne(n, mode) {
  const label = mode === 'keep' ? '保留现版（转成本单副本）' : '采用新版'
  try {
    await ElMessageBox.confirm(
      mode === 'keep'
        ? `「${n.part}」按本单当初开工的那一版固化为副本：之后基线再升版都不影响它。确定？`
        : `「${n.part}」改用现行已发布工艺版本？本单定额会按新版重算。`,
      label,
      { type: 'warning', confirmButtonText: '确定' }
    )
  } catch {
    return
  }
  acking.value = true
  try {
    const r = await api.ackTaskVersion(detail.value.id, {
      tr_id: n.task_route_id,
      mode,
      user: 'operator'
    })
    detail.value = r.task
    const item = (r.items || [])[0] || {}
    if (mode === 'keep') {
      ElMessage.success(`已按现版转成本单副本 v${item.copy_version ?? ''}，不再跟随基线`)
    } else {
      ElMessage.success(
        item.changed === false
          ? `已确认按 v${item.version} 执行`
          : `已切到新版 v${item.version}`
      )
    }
    await reload()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '确认失败')
  } finally {
    acking.value = false
  }
}

async function ackAll(mode) {
  const count = (detail.value?.notices || []).length
  try {
    await ElMessageBox.confirm(
      mode === 'keep'
        ? `本单 ${count} 条引用全部按各自当初那一版固化为副本？此后不再跟随基线升版。`
        : `本单 ${count} 条引用全部切到现行已发布工艺？定额会按新版重算。`,
      mode === 'keep' ? '全部保留现版' : '全部采用新版',
      { type: 'warning', confirmButtonText: '确定' }
    )
  } catch {
    return
  }
  acking.value = true
  try {
    const r = await api.ackTaskVersion(detail.value.id, { mode, user: 'operator' })
    detail.value = r.task
    if (r.failed && r.failed.length) {
      ElMessage.warning(`已处理 ${r.acked} 条，${r.failed.length} 条失败：${r.failed[0].reason}`)
    } else {
      ElMessage.success(`已确认 ${r.acked} 条引用`)
    }
    await reload()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '确认失败')
  } finally {
    acking.value = false
  }
}

async function changeStatus() {
  if (!(await confirmVersionNotice('修改任务状态'))) {
    detail.value = await api.task(detail.value.id)
    return
  }
  try {
    await api.patchTask(detail.value.id, { status: detail.value.status })
    ElMessage.success('状态已更新')
    await reload()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '更新失败')
  }
}

function openCreate() {
  form.value = emptyForm()
  createVisible.value = true
  loadPeriods()
}

async function loadPeriods() {
  if (periods.value.length) return
  try {
    const d = await api.periods()
    periods.value = d.items || d || []
  } catch {
    periods.value = []
  }
}

async function submitCreate() {
  creating.value = true
  try {
    const r = await api.createTask({ ...form.value })
    ElMessage.success(`任务 ${r.task.task_no} 已创建`)
    createVisible.value = false
    await reload()
    detail.value = r.task
    drawer.value = true
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '创建失败')
  } finally {
    creating.value = false
  }
}

async function openPicker() {
  pickerVisible.value = true
  pickerKw.value = ''
  picked.value = []
  await loadCandidates()
}

async function loadCandidates() {
  const d = await api.taskCandidates(detail.value.id, { q: pickerKw.value, limit: 500 })
  candidates.value = d.items || []
  candidateHint.value = d.hint || ''
}

function onPick(list) {
  picked.value = list
}

async function submitPick() {
  if (!(await confirmVersionNotice('添加路线'))) return
  adding.value = true
  try {
    const items = picked.value.map((c) => ({
      product: c.product,
      part: c.part,
      route_id: c.route_id,
      batch_no: '',
      qty: 0
    }))
    const r = await api.addTaskRoutes(detail.value.id, { items, create_baseline: true })
    if (r.failed && r.failed.length) {
      ElMessage.warning(`${r.added} 条已添加，${r.failed.length} 条跳过：${r.failed[0].reason}`)
    } else {
      ElMessage.success(`已添加 ${r.added} 条路线（引用基线，不复制）`)
    }
    detail.value = r.task
    pickerVisible.value = false
    await reload()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '添加失败')
  } finally {
    adding.value = false
  }
}

async function forkRoute(row) {
  try {
    await ElMessageBox.confirm(
      `把「${row.part}」拆成本单副本？之后改这条路线只影响本任务，改基线也不再影响它。`,
      '拆出副本',
      { type: 'warning' }
    )
  } catch {
    return
  }
  try {
    const r = await api.forkTaskRoute(detail.value.id, row.id)
    ElMessage.success(`已拆出副本 v${r.version}（来源基线 v${r.from_version}）`)
    detail.value = await api.task(detail.value.id)
    await reload()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '拆分失败')
  }
}

async function promoteRoute(row) {
  const merged = row.diff?.identical
  try {
    await ElMessageBox.confirm(
      merged
        ? `这份副本与现行基线 v${row.baseline_version || '?'} 完全一致，合并后本任务改回「引用基线」，副本删除。确定？`
        : `把这份副本进阶为「${row.product} / ${row.part}」的新版基线？` +
          '新版会立即发布并生效：所有引用旧版的在制订单一起切到新版，旧版归档，副本随后删除。',
      merged ? '合并回引用' : '进阶为基线',
      { type: 'warning', confirmButtonText: merged ? '合并' : '确定进阶' }
    )
  } catch {
    return
  }
  try {
    const r = await api.promoteTaskRoute(detail.value.id, row.id)
    ElMessage.success(r.message || '已处理')
    detail.value = await api.task(detail.value.id)
    await reload()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '进阶失败')
  }
}

async function removeRoute(row) {
  try {
    await ElMessageBox.confirm(
      row.origin === 'copy'
        ? `移除「${row.part}」并删除本单副本？副本只属于这个任务，删除后不可恢复。`
        : `把「${row.part}」从任务里移除？基线路线本身不受影响。`,
      '移除路线',
      { type: 'warning' }
    )
  } catch {
    return
  }
  try {
    const r = await api.removeTaskRoute(detail.value.id, row.id)
    ElMessage.success(r.deleted_copy ? '已移除并删除副本' : '已移除（基线未改动）')
    detail.value = await api.task(detail.value.id)
    await reload()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '移除失败')
  }
}

function openEdit(row) {
  editRow.value = row
  editVisible.value = true
}

function onRouteSaved(r) {
  // 保存后直接用接口返回的最新详情刷新抽屉，不再多发一次请求
  if (r?.task) detail.value = r.task
  // 改动的是全厂标准时，别的订单会立刻进入「升版待确认」——
  // 必须当场说清是哪几单，否则改的人以为只影响自己这张单
  const affected = r?.affected_tasks || []
  if (affected.length) {
    ElMessageBox.alert(
      `这条标准工艺被 ${affected.length + 1} 个在制订单使用。你的修改已发布为新版本，` +
        `以下订单会亮起「升版待确认」，需要它们各自确认：\n` +
        affected.map((t) => `· ${t.task_no}（${t.part || '—'}）`).join('\n'),
      '已影响其它订单',
      { type: 'warning', confirmButtonText: '知道了' }
    )
  }
  reload()
}

async function showRoute(row) {
  const full = await api.route(row.route_id)
  routeTitle.value = `${row.product} / ${row.part}　${row.origin === 'copy' ? '本单副本' : '基线引用'} v${full.version}`
  routeSteps.value = full.steps || []
  routeVisible.value = true
}

async function rebuild() {
  rebuilding.value = true
  try {
    const r = await api.rebuildTasks()
    ElMessage.success(`归集 ${r.refreshed} 张单据，新建 ${r.created} 个任务`)
    await reload()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '重建失败')
  } finally {
    rebuilding.value = false
  }
}

function statusText(s) {
  return { draft: '草稿', open: '在制', closed: '已结案', cancelled: '已取消' }[s] || s || '—'
}

function statusType(s) {
  return { draft: 'info', open: 'primary', closed: 'success', cancelled: 'danger' }[s] || 'info'
}

function sourceText(s) {
  return { manual: '手工创建', sheet: '单据归集' }[s] || s || ''
}

function fmtTime(s) {
  if (!s) return ''
  return s.replace('T', ' ').slice(0, 16)
}

// ---------------------------------------------------------------- 基本信息与版本
const taskEditVisible = ref(false)
const taskEditTarget = ref(null)
const revisionsVisible = ref(false)
const revisionsOf = ref(null)
const revisionRows = ref([])

function versionTip(t) {
  const v = t?.version || 1
  if (v <= 1) return 'v1：建单原样，口径从未改过'
  const who = t.revised_by || '（未记录）'
  const when = fmtTime(t.revised_at)
  return `v${v}：最近一次由 ${who} 于 ${when} 修改；点击查看修订历史`
}

// 列表行只有聚合字段，编辑要完整详情（notices / version / 最近修订）
async function openEditTask(row) {
  let full = row
  if (!Array.isArray(row.revisions) || row.notices === undefined) {
    try {
      full = await api.task(row.id)
    } catch (e) {
      ElMessage.error(e?.response?.data?.detail || '读取任务详情失败')
      return
    }
  }
  taskEditTarget.value = full
  taskEditVisible.value = true
}

async function onTaskSaved(r) {
  // 同一个任务在抽屉里开着，保存后必须同步刷新，否则表头还显示旧数量/旧版本
  if (detail.value && r?.task && detail.value.id === r.task.id) {
    detail.value = r.task
  }
  await reload()
}

function openRevisions(t) {
  const list = t?.revisions || []
  if (!list.length) {
    ElMessage.info('这个任务自建单以来没有改过口径字段，仍是 v1')
    return
  }
  revisionsOf.value = t
  revisionRows.value = list
  revisionsVisible.value = true
}

// 从工艺页「去任务」跳过来时带 ?task=ID：直接把那个任务的抽屉打开，
// 免去用户在一长串任务里再找一遍
onMounted(async () => {
  await reload()
  const id = Number(route.query.task)
  if (id) {
    const row = rows.value.find((r) => r.id === id)
    if (row) await open(row)
    else {
      try {
        detail.value = await api.task(id)
        drawer.value = true
      } catch {
        ElMessage.warning('任务不存在或已被删除')
      }
    }
  }
})
</script>

<style scoped>
/* 待核对与实报并排但不混淆：灰色小字，一眼看出「这笔还没入账」 */
.pending-hint {
  color: #b07800;
  font-size: 11.5px;
  margin-left: 4px;
  cursor: help;
}

.task-no {
  font-family: ui-monospace, Menlo, Consolas, monospace;
  color: #14539f;
  cursor: pointer;
}
.task-no:hover { text-decoration: underline; }
.op-cell {
  display: flex;
  align-items: center;
  gap: 2px;
}
.op-cell :deep(.el-button + .el-button) { margin-left: 0; }
.cell-sub { font-size: 11.5px; color: #8a94a6; line-height: 1.4; }
.cell-warn { font-size: 11.5px; color: #c0392b; line-height: 1.5; font-weight: 600; }
.ok { color: #1f8b4c; }
.warn { color: #c0392b; }

.no-cell {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.notice-chip { flex: 0 0 auto; }
.ver-arrow { color: #c0392b; margin-left: 4px; }
:deep(.row-stale) td { background: #fff7f6 !important; }
:deep(.row-stale:hover) td { background: #ffefed !important; }

/* 升版提醒块：红底红框，和普通提示区分开，不能靠用户自己找 */
.tk-notice {
  margin-top: 8px;
  border: 1px solid #f0c2bc;
  background: #fdf3f2;
  border-radius: 6px;
  padding: 8px 12px 6px;
}
.tk-notice-head {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  font-size: 12.5px;
  color: #8d3226;
  flex-wrap: wrap;
}
.tk-notice-head b { font-size: 13px; color: #a52a1c; }
.tk-notice-badge {
  background: #c0392b;
  color: #fff;
  border-radius: 3px;
  padding: 1px 6px;
  font-size: 11.5px;
  font-weight: 600;
  white-space: nowrap;
}
.tk-notice-hint { color: #9c6157; font-size: 12px; }
.tk-notice-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 0;
  border-top: 1px dashed #f0d5d1;
  margin-top: 4px;
  font-size: 12.5px;
  line-height: 1.6;
}
.tk-notice-part {
  font-weight: 600;
  color: #1f2329;
  white-space: nowrap;
  max-width: 160px;
  overflow: hidden;
  text-overflow: ellipsis;
}
.tk-notice-msg { color: #7a4a42; min-width: 0; overflow: hidden; text-overflow: ellipsis; }
.tk-notice-foot {
  margin-top: 4px;
  padding-top: 4px;
  border-top: 1px dashed #f0d5d1;
  font-size: 11.5px;
  color: #9c7c76;
  line-height: 1.6;
}

.form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0 12px;
}

.task-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 2px 30px;
  background: #f7f9fc;
  border: 1px solid #e7ebf1;
  border-radius: 6px;
  padding: 8px 14px;
  margin-top: 8px;
}
/* 标签在左、值在右：任何一项都不允许上下堆叠 */
.task-grid > div {
  display: flex;
  flex-direction: row;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  min-width: 0;
  font-size: 12.5px;
  line-height: 2;
}
.task-grid .k {
  flex: 0 0 auto;
  color: #8a94a6;
  white-space: nowrap;
}
.task-grid .v {
  flex: 0 1 auto;
  min-width: 0;
  font-weight: 600;
  color: #1f2329;
  text-align: right;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.tk-head-line {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}
.tk-no {
  font-family: ui-monospace, Menlo, Consolas, monospace;
  font-size: 15px;
  font-weight: 700;
  color: #1f2329;
  white-space: nowrap;
}
.tk-name {
  font-size: 13px;
  color: #4a5361;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tk-src { font-size: 11.5px; color: #8a94a6; white-space: nowrap; }
.tk-locked {
  margin-top: 8px;
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 12.5px;
  line-height: 1.7;
  border-radius: 6px;
  padding: 7px 12px;
  background: #f4f5f7;
  border: 1px solid #e3e6eb;
  color: #5a6474;
}
.tk-locked b { color: #3f4753; white-space: nowrap; }

/* 业务待办条：一条横向清单，点一项直接去处理 */
.todo-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding: 7px 12px;
  margin-bottom: 10px;
  background: #fffaf2;
  border: 1px solid #f3e3c8;
  border-radius: 8px;
  font-size: 12.5px;
}
.todo-label { color: #9a7a3a; font-weight: 600; }
.todo-item {
  border: 1px solid #e8dcc4;
  background: #fff;
  border-radius: 14px;
  padding: 2px 10px;
  font: inherit;
  font-size: 12px;
  color: #6b5a35;
  cursor: pointer;
}
.todo-item:hover { border-color: #c9a961; color: #5a4a25; }
.todo-item b { font-variant-numeric: tabular-nums; }
.todo-error { border-color: #e8c4c0; color: #9b3a2f; background: #fdf3f2; }
.todo-error:hover { border-color: #c9695c; color: #87301f; }

.picker-hint {
  font-size: 12px;
  color: #8a6212;
  background: #fdf6e8;
  border: 1px solid #f2e2bf;
  border-radius: 6px;
  padding: 6px 10px;
  margin-bottom: 8px;
  line-height: 1.6;
}
.tk-remark {
  margin-top: 8px;
  font-size: 12px;
  color: #5a6474;
  line-height: 1.7;
  overflow: hidden;
  text-overflow: ellipsis;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

.block-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 14px 0 8px;
}
.block-title { font-weight: 600; font-size: 13px; color: #1f2329; }

.hint-box {
  font-size: 12px;
  color: #5a6474;
  background: #f5f8fd;
  border: 1px solid #e2eaf6;
  border-radius: 6px;
  padding: 8px 10px;
  line-height: 1.7;
}

/* 版本号：v1 是灰的（建单原样），改过口径的版本用琥珀色，一眼能分出哪些单被改过 */
.ver-chip {
  display: inline-block;
  font-size: 11.5px;
  line-height: 18px;
  padding: 0 7px;
  border-radius: 9px;
  border: 1px solid #e3e6eb;
  background: #f7f9fc;
  color: #6b7482;
  font-weight: 600;
}
.ver-chip.revised {
  border-color: #f0d9a8;
  background: #fff8ec;
  color: #a86400;
}
button.ver-chip {
  cursor: pointer;
  font-family: inherit;
}
button.ver-chip:hover {
  border-color: #d9c294;
  background: #fff3dd;
}

.rv-head { display: flex; align-items: center; gap: 10px; }
.rv-title { font-weight: 600; font-size: 14.5px; color: #1f2329; }
.rv-no { font-size: 12.5px; color: #5a6474; }
.rv-change { display: flex; align-items: baseline; gap: 6px; font-size: 12.5px; line-height: 1.7; }
.rv-field { color: #8a94a6; min-width: 58px; }
.rv-old { color: #8a94a6; text-decoration: line-through; }
.rv-arrow { color: #b6bec9; }
.rv-new { color: #1f2329; font-weight: 600; }
</style>

<style>
/* 抽屉是 teleport 到 body 的，表头/内边距用非 scoped 覆盖，类名已限定 */
.task-drawer.el-drawer {
  max-width: 96vw;
}
.task-drawer .el-drawer__header {
  margin-bottom: 0;
  padding: 14px 18px 12px;
  border-bottom: 1px solid #e7ebf1;
  align-items: flex-start;
  color: #1f2329;
}
.task-drawer .el-drawer__body {
  padding: 12px 18px 20px;
}
.task-drawer .el-drawer__body > .block-head:first-child {
  margin-top: 0;
}
</style>
