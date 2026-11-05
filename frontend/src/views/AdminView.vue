<template>
  <div class="page">
    <div class="page-head">
      <span class="page-title">系统管理</span>
      <span class="page-sub">
        基础数据即识别兜底名册，改完下一张单据立即生效
      </span>
      <div class="spacer"></div>
      <el-button size="small" :loading="rematching" @click="doRematch">
        <el-icon><Refresh /></el-icon>用最新字典回补历史单据
      </el-button>
    </div>

    <div class="kpi-row">
      <div class="kpi accent">
        <div class="kpi-label">字典条目</div>
        <div class="kpi-value">{{ totalEntries }}</div>
      </div>
      <div class="kpi ok">
        <div class="kpi-label">启用人员</div>
        <div class="kpi-value">{{ activePeople }}</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">岗位角色</div>
        <div class="kpi-value">{{ cards.find((c) => c.key === 'roles')?.count ?? 0 }}</div>
      </div>
      <div class="kpi warn">
        <div class="kpi-label">未入档值</div>
        <div class="kpi-value">{{ unregistered }}</div>
      </div>
    </div>

    <!-- 所有卡片同属一个网格：不分功能区块，依次排、满一行自动换行。
         备份与账号放最前 —— 不常用，但要恢复数据、要停人时必须一眼能找到。 -->
    <div class="card-grid" v-loading="loading">
      <div class="admin-card" @click="openOps('backup')">
        <div class="card-top">
          <div class="card-icon ic-backup">
            <el-icon :size="20"><FolderOpened /></el-icon>
          </div>
          <div class="card-title-wrap">
            <div class="card-title">数据备份</div>
            <div class="card-sub">整库全量 / 逐表校验</div>
          </div>
        </div>

        <div class="card-metric">
          <span class="metric-num">{{ backups.length }}</span>
          <span class="metric-unit">份</span>
        </div>

        <div class="card-extra">
          <template v-if="latestBackup">
            <el-tag size="small" :type="latestBackup.ok ? 'success' : 'danger'" effect="plain">
              {{ latestBackup.ok ? '最近备份可用' : '最近备份异常' }}
            </el-tag>
            <span class="extra-text">
              　{{ shortTime(latestBackup.mtime) }}<template v-if="latestBackup.tables">
                · {{ latestBackup.tables }} 表</template>
            </span>
          </template>
          <el-tag v-else-if="backupDenied" size="small" type="info" effect="plain">需要管理员权限</el-tag>
          <span v-else class="muted" style="font-size: 12px">还没有备份</span>
        </div>

        <div class="card-tip">
          整库全量快照：当前库 {{ backupSource.tables || '—' }} 张表、{{ fmtRows(backupSource.rows) }} 行全部在内。
          WAL 下直接拷 .db 只是空表壳，这里由 SQLite 出在线快照并逐表核对。
        </div>

        <div class="card-foot">
          <span class="go-text">点击设置</span>
          <el-icon class="go-icon"><ArrowRight /></el-icon>
        </div>
      </div>

      <div class="admin-card" @click="openOps('users')">
        <div class="card-top">
          <div class="card-icon ic-account">
            <el-icon :size="20"><Avatar /></el-icon>
          </div>
          <div class="card-title-wrap">
            <div class="card-title">账号</div>
            <div class="card-sub">登录 / 系统角色 / 停用</div>
          </div>
        </div>

        <div class="card-metric">
          <span class="metric-num">{{ users.length }}</span>
          <span class="metric-unit">个</span>
          <span v-if="activeUsers !== users.length" class="metric-dim">（启用 {{ activeUsers }}）</span>
        </div>

        <div class="card-extra">
          <span v-if="me" class="extra-text">当前登录：{{ me }}</span>
          <el-tag v-else-if="userDenied" size="small" type="info" effect="plain">需要管理员权限</el-tag>
          <span v-else class="muted" style="font-size: 12px">未登录（本机免登录模式）</span>
        </div>

        <div class="card-tip">
          系统角色管「能操作什么」，与名册里的操作员/质检员（岗位角色）是两回事。
        </div>

        <div class="card-foot">
          <span class="go-text">点击设置</span>
          <el-icon class="go-icon"><ArrowRight /></el-icon>
        </div>
      </div>

      <!-- 工时单价：工资核算的唯一价格来源。放在这里而不是工资页，
           是因为它属于「口径」不属于「某个月的账」—— 价格在这儿定，账在那儿算。 -->
      <div class="admin-card" @click="openOps('rates')">
        <div class="card-top">
          <div class="card-icon ic-rates">
            <el-icon :size="20"><Money /></el-icon>
          </div>
          <div class="card-title-wrap">
            <div class="card-title">工时单价</div>
            <div class="card-sub">元/小时 · 按生效月留档</div>
          </div>
        </div>

        <div class="card-metric">
          <span class="metric-num">{{ ratePriced }}</span>
          <span class="metric-unit">/ {{ rateTotal }} 人</span>
          <span v-if="rateMissing" class="metric-dim">（未设 {{ rateMissing }}）</span>
        </div>

        <div class="card-extra">
          <el-tag v-if="rateMissing" size="small" type="warning" effect="plain">
            {{ rateMissing }} 人没单价，工资算不出来
          </el-tag>
          <span v-else class="extra-text">在用人人都已定价</span>
        </div>

        <div class="card-tip">
          改价只影响生效月及以后：8 月已经发过的工资，不会因为 9 月调价而变。
        </div>

        <div class="card-foot">
          <span class="go-text">点击设置</span>
          <el-icon class="go-icon"><ArrowRight /></el-icon>
        </div>
      </div>

      <div
        v-for="card in cards"
        :key="card.key"
        class="admin-card"
        :class="{ highlight: card.key === 'workers' }"
        @click="openCard(card)"
      >
        <div class="card-top">
          <div class="card-icon" :class="`ic-${card.key}`">
            <el-icon :size="20"><component :is="iconOf(card.key)" /></el-icon>
          </div>
          <div class="card-title-wrap">
            <div class="card-title">{{ card.title }}</div>
            <div class="card-sub">{{ card.subtitle }}</div>
          </div>
        </div>

        <div class="card-metric">
          <span class="metric-num">{{ card.count }}</span>
          <span class="metric-unit">条</span>
          <span v-if="card.active !== card.count" class="metric-dim">（启用 {{ card.active }}）</span>
        </div>

        <div class="card-extra">
          <el-tag v-if="card.gap" size="small" type="warning" effect="plain">
            未入档 {{ card.gap }} 项
          </el-tag>
          <span v-else-if="card.extra" class="extra-text">{{ card.extra }}</span>
          <span v-else class="muted" style="font-size: 12px">全部已入档</span>
        </div>

        <div class="card-tip">{{ card.tip }}</div>

        <div class="card-foot">
          <span class="go-text">点击设置</span>
          <el-icon class="go-icon"><ArrowRight /></el-icon>
        </div>
      </div>
    </div>

    <!-- 数据备份设置 -->
    <el-dialog
      v-model="backupVisible"
      title="数据备份"
      width="min(1000px, 95vw)"
      top="6vh"
      @open="loadBackups"
    >
      <el-alert
        :title="`全量备份：整个数据库文件的完整副本，当前库 ${backupSource.tables || '—'} 张表、${fmtRows(backupSource.rows)} 行全在快照里，不是只导几张业务表。每份都配一份出厂清单（.db.json）记为它备份时的表清单，校验时逐表核对，少一张即标红。`"
        type="info"
        :closable="false"
        show-icon
        class="dlg-alert"
      />

      <div class="dlg-bar">
        <span class="dlg-path" :title="backupDir">目录：{{ backupDir || '—' }}</span>
        <div class="spacer"></div>
        <span class="muted dlg-keep">备份后保留</span>
        <el-input-number v-model="backupKeep" :min="1" :max="90" size="small" controls-position="right" style="width: 92px" />
        <span class="muted dlg-keep">份</span>
        <el-button size="small" @click="loadBackups">重新校验</el-button>
        <el-button size="small" type="primary" :loading="backingUp" @click="doBackup">立即备份</el-button>
      </div>

      <el-table :data="backups" size="small" max-height="340" style="width: 100%">
        <el-table-column prop="name" label="文件" min-width="268" show-overflow-tooltip />
        <el-table-column prop="mtime" label="备份时间" width="152" show-overflow-tooltip />
        <el-table-column label="校验" width="68" align="center">
          <template #default="{ row }">
            <el-tooltip :content="verifyTip(row)" placement="top">
              <el-tag size="small" :type="row.ok ? 'success' : 'danger'">{{ row.ok ? '可用' : '异常' }}</el-tag>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column label="表" width="56" align="right">
          <template #default="{ row }">
            <span :class="{ 'num-bad': !row.ok }">{{ row.tables ?? '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="行数" width="78" align="right">
          <template #default="{ row }">{{ fmtRows(row.rows) }}</template>
        </el-table-column>
        <el-table-column label="单据" width="58" align="right">
          <template #default="{ row }">{{ row.core?.sheet ?? '—' }}</template>
        </el-table-column>
        <el-table-column label="工时明细" width="78" align="right">
          <template #default="{ row }">{{ row.core?.work_item ?? '—' }}</template>
        </el-table-column>
        <el-table-column label="大小" width="78" align="right">
          <template #default="{ row }">{{ row.size_kb }} KB</template>
        </el-table-column>
        <el-table-column label="操作" width="104" align="center">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openDetail(row)">明细</el-button>
            <el-button
              link
              type="danger"
              size="small"
              :disabled="!row.ok"
              :title="row.ok ? '用这份备份覆盖当前库' : '这份备份本身校验不合格，不能用来恢复'"
              @click="openRestore(row)"
            >
              恢复
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <div v-if="!backups.length" class="muted dlg-empty">
        还没有备份。先点「立即备份」出一份，再用计划任务每天跑
        <code>scripts/backup_db.py</code>（命令示例见文件头注释）。
      </div>

      <div class="dlg-note">
        恢复：点某一行的「恢复」，会把那份备份的数据整库写回当前库；系统会先自动给当前库拍一份
        <code>pre-restore</code> 快照，恢复错了还能退回来。备份目录与库在同一块盘上，
        建议定期把整个备份目录再同步一份到别的机器。<br />
        范围：数据库文件本身（全部表与索引）。<code>data/uploads</code> 里的原始单据照片不在库内，需另行同步。
      </div>

      <template #footer>
        <el-button @click="backupVisible = false">关闭</el-button>
      </template>
    </el-dialog>

    <!-- 单份备份的表清单：逐表对照当前库，证明「全量」而不是「几张核心表」 -->
    <el-dialog
      v-model="detailVisible"
      :title="`表清单 · ${detailRow?.name || ''}`"
      width="min(540px, 94vw)"
      append-to-body
    >
      <div class="muted dlg-empty" style="padding: 0 0 8px">
        {{ detailRow?.tables || 0 }} 张表 / {{ fmtRows(detailRow?.rows) }} 行。
        「当前库」列是备份时点之后的现状，「差异」为正数表示备份后新增的行。<template v-if="detailGap.length">
          其中 {{ detailGap.length }} 张表是这份备份之后才加的（当时还不存在，属正常）。</template>
      </div>
      <el-table :data="detailRows" size="small" max-height="420" style="width: 100%">
        <el-table-column prop="name" label="表" min-width="180" show-overflow-tooltip />
        <el-table-column label="备份" width="82" align="right">
          <template #default="{ row }">{{ row.bak }}</template>
        </el-table-column>
        <el-table-column label="当前库" width="82" align="right">
          <template #default="{ row }">
            <span v-if="row.onlyBak" class="muted">—</span>
            <span v-else>{{ row.src }}</span>
          </template>
        </el-table-column>
        <el-table-column label="差异" width="82" align="right">
          <template #default="{ row }">
            <span v-if="row.onlyBak" class="num-bad">备份独有</span>
            <span v-else :class="{ 'num-warn': row.delta > 0 }">{{ row.delta > 0 ? '+' + row.delta : row.delta }}</span>
          </template>
        </el-table-column>
      </el-table>
      <template #footer>
        <el-button @click="detailVisible = false">关闭</el-button>
      </template>
    </el-dialog>

    <!-- 恢复确认：把「要覆盖什么、能退回哪、哪些表不动」摆清楚再让人点 -->
    <el-dialog
      v-model="restoreVisible"
      :title="restoreStep === 'done' ? '恢复完成' : '从备份恢复'"
      width="min(620px, 94vw)"
      :close-on-click-modal="false"
      :show-close="restoreStep !== 'running'"
      append-to-body
      @closed="resetRestore"
    >
      <template v-if="restoreStep === 'done' && restoreResult">
        <el-result icon="success" title="已恢复" :sub-title="`数据已回到 ${restoreResult.restored_from?.at || restoreResult.restored_from?.mtime || '备份时点'}`">
          <template #extra>
            <div class="restore-done">
              <div>
                行数：{{ fmtRows(restoreResult.before?.rows) }} →
                <b>{{ fmtRows(restoreResult.after?.rows) }}</b>
              </div>
              <div>
                回退快照：<code>{{ restoreResult.pre_restore }}</code>
              </div>
              <div v-if="restoreResult.kept?.length" class="restore-kept">
                保留（这份备份里没有的表）：{{ restoreResult.kept.join('、') }}
              </div>
            </div>
          </template>
        </el-result>
      </template>

      <template v-else-if="restoreRow">
        <table class="restore-cmp">
          <tbody>
            <tr>
              <th></th>
              <th>备份（要恢复过去的样子）</th>
              <th>当前库（会被覆盖）</th>
            </tr>
            <tr>
              <td>时点</td>
              <td>{{ restoreRow.meta?.at || restoreRow.mtime }}</td>
              <td>现在</td>
            </tr>
            <tr>
              <td>规模</td>
              <td>{{ restoreRow.tables }} 表 / {{ fmtRows(restoreRow.rows) }} 行</td>
              <td>{{ backupSource.tables }} 表 / {{ fmtRows(backupSource.rows) }} 行</td>
            </tr>
            <tr>
              <td>单据</td>
              <td>{{ restoreRow.core?.sheet ?? '—' }}</td>
              <td>{{ backupSource.core?.sheet ?? '—' }}</td>
            </tr>
            <tr>
              <td>工时明细</td>
              <td>{{ restoreRow.core?.work_item ?? '—' }}</td>
              <td>{{ backupSource.core?.work_item ?? '—' }}</td>
            </tr>
          </tbody>
        </table>

        <ul class="restore-warn">
          <li>恢复会把当前库的<strong>数据整表替换</strong>成这份备份，今天录入的内容如果备份里没有，会消失。</li>
          <li>系统会<strong>先自动给当前库拍一份快照</strong>（<code>*-pre-restore.db</code>），恢复错了可以用它退回来。</li>
          <li>恢复期间其他人如果正在操作系统，那部分操作会丢失，请先通知他们停手。</li>
          <li v-if="restoreKept.length">
            这份备份里没有 <strong>{{ restoreKept.join('、') }}</strong>（当时还没这功能），
            这些表的数据会<strong>保留</strong>，不回退。
          </li>
          <li>恢复完成后页面会自动刷新。</li>
        </ul>

        <el-checkbox v-model="restoreAck" class="restore-ack">
          我知道这会覆盖当前数据，且已确认要回到 {{ restoreRow.meta?.at || restoreRow.mtime }}
        </el-checkbox>
      </template>

      <template #footer>
        <template v-if="restoreStep === 'done'">
          <el-button type="primary" @click="reloadAfterRestore">刷新页面</el-button>
        </template>
        <template v-else>
          <el-button :disabled="restoreStep === 'running'" @click="restoreVisible = false">取消</el-button>
          <el-button
            type="danger"
            :disabled="!restoreAck"
            :loading="restoreStep === 'running'"
            @click="doRestore"
          >
            确认恢复
          </el-button>
        </template>
      </template>
    </el-dialog>

    <!-- 账号设置 -->
    <el-dialog
      v-model="userVisible"
      title="账号"
      width="min(1200px, 96vw)"
      top="6vh"
      @open="loadUsers"
    >
      <el-alert type="info" :closable="false" show-icon class="dlg-alert">
        <template #title>
          <b>系统角色</b>（能走到哪一步：管理员 / 录入核对 / 只读）是权限上限；
          <b>岗位角色</b>（名册里那个人干什么活）上的<b>岗位职能</b>（任务编制 / 工艺卡编制 / 报工 / 工时单价）
          才是具体授权，一人多岗取并集。账号上挂的人员只用于说明「这个账号是谁」，本身不产生权限。
        </template>
      </el-alert>

      <div class="dlg-bar">
        <span class="muted dlg-keep">
          共 {{ users.length }} 个账号，启用 {{ activeUsers }} 个<template v-if="me">；当前登录 {{ me }}</template>
        </span>
        <div class="spacer"></div>
        <el-button size="small" @click="loadUsers">刷新</el-button>
        <el-button size="small" type="primary" @click="openUserForm()">新增账号</el-button>
      </div>

      <el-table :data="users" size="small" max-height="320" style="width: 100%">
        <el-table-column label="账号" min-width="120">
          <template #default="{ row }">
            <span>{{ row.username }}</span>
            <el-tag v-if="row.username === me" size="small" effect="plain" style="margin-left: 4px">当前</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="display_name" label="姓名" min-width="82" show-overflow-tooltip />
        <el-table-column label="系统角色" width="100">
          <template #default="{ row }">
            <el-tooltip :content="sysRoleSummary(row.role)" placement="top">
              <el-tag size="small" :type="roleType(row.role)" effect="plain">
                {{ row.role_name || roleText(row.role) }}
              </el-tag>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column label="对应人员 / 岗位角色" min-width="196">
          <template #default="{ row }">
            <template v-if="row.worker_id">
              <span :class="{ 'text-off': row.worker_active === false }">
                {{ row.worker_name || `人员 #${row.worker_id}` }}
              </span>
              <el-tag
                v-for="p in row.positions"
                :key="p"
                size="small"
                effect="plain"
                class="role-tag"
              >
                {{ p }}
              </el-tag>
              <span v-if="row.worker_missing" class="muted">（名册里已找不到）</span>
              <span v-else-if="row.worker_active === false" class="muted">（人员已停用）</span>
              <span v-else-if="!row.positions.length" class="muted">（未分配岗位）</span>
            </template>
            <span v-else class="muted">不对应车间人员</span>
          </template>
        </el-table-column>
        <el-table-column label="有效职能" min-width="214">
          <template #default="{ row }">
            <template v-if="row.duty_source === 'admin'">
              <el-tag size="small" type="danger" effect="plain">全部职能（管理员）</el-tag>
            </template>
            <template v-else-if="(row.duties || []).length">
              <el-tooltip :content="(row.duty_names || []).join('、')" placement="top">
                <span>
                  <el-tag
                    v-for="d in (row.duty_names || []).slice(0, 2)"
                    :key="d"
                    size="small"
                    effect="plain"
                    class="role-tag"
                  >
                    {{ d }}
                  </el-tag>
                  <span v-if="(row.duty_names || []).length > 2" class="muted">
                    +{{ row.duty_names.length - 2 }}
                  </span>
                </span>
              </el-tooltip>
              <div v-if="row.duty_source === 'default'" class="hint-inline">
                未配岗位职能，按系统角色默认
              </div>
            </template>
            <span v-else class="muted">无（只读）</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="70" align="center">
          <template #default="{ row }">
            <el-tag size="small" :type="row.active ? 'success' : 'info'">{{ row.active ? '启用' : '停用' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="最后登录" width="134">
          <template #default="{ row }">
            <span :class="{ muted: !row.last_login_at }">{{ row.last_login_at ? row.last_login_at.slice(0, 16).replace('T', ' ') : '从未登录' }}</span>
          </template>
        </el-table-column>
        <!-- 固定在最右：三个操作按钮一行放不下时会换行，宁可让它固定也别挤成两行 -->
        <el-table-column label="操作" width="206" align="right" fixed="right">
          <template #default="{ row }">
            <el-button size="small" text type="primary" @click="openUserForm(row)">编辑</el-button>
            <el-button size="small" text @click="resetPassword(row)">重置密码</el-button>
            <el-button size="small" text :type="row.active ? 'danger' : 'success'" @click="toggleUser(row)">
              {{ row.active ? '停用' : '启用' }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <!-- 权限矩阵：矩阵口径来自后端 app/domain/sys_roles.py，
           不在这里另写一套 —— 界面说能做什么，服务端就必须真的那么做。 -->
      <div class="perm-matrix">
        <div class="pm-head">
          <span class="pm-title">系统角色权限对照</span>
          <span class="muted pm-sub">只读账号的写操作由服务端直接拒绝，不是只把按钮变灰</span>
        </div>
        <table>
          <thead>
            <tr>
              <th class="pm-perm">能力</th>
              <th v-for="r in sysRoles" :key="r.code">
                {{ r.name }}<span class="pm-code">{{ r.code }}</span>
              </th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="p in permRows" :key="p.code">
              <td class="pm-perm">{{ p.label }}</td>
              <td
                v-for="r in sysRoles"
                :key="r.code"
                :class="{ 'pm-yes': r.perms.includes(p.code) }"
              >
                {{ r.perms.includes(p.code) ? '✓' : '—' }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 岗位职能：这几项不看系统角色，看这个人挂了哪些岗位、岗位上勾了什么 -->
      <div class="perm-matrix duty-matrix">
        <div class="pm-head">
          <span class="pm-title">岗位职能对照</span>
          <span class="muted pm-sub">
            按「这个人挂了哪些岗位」授权，一人多岗取并集；岗位没勾职能时按系统角色默认
          </span>
        </div>
        <table>
          <thead>
            <tr>
              <th class="pm-perm">岗位职能</th>
              <th class="pm-scope">管住的操作</th>
              <th>管理员</th>
              <th>录入核对</th>
              <th>只读</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="d in dutyOptions" :key="d.code">
              <td class="pm-perm">{{ d.label }}</td>
              <td class="pm-scope">{{ d.scope }}</td>
              <td class="pm-yes">✓</td>
              <td :class="{ 'pm-yes': defaultDuties('editor').includes(d.code) }">
                {{ defaultDuties('editor').includes(d.code) ? '✓' : '—' }}
              </td>
              <td>{{ defaultDuties('viewer').includes(d.code) ? '✓' : '—' }}</td>
            </tr>
          </tbody>
        </table>
        <div class="pm-foot">
          表里的 ✓ 是「没配岗位职能时的默认值」。一旦给岗位勾了职能，就严格按岗位并集判定——
          没勾的那几项会被服务端拒绝，这就是「必须由专用岗位执行」。
        </div>
      </div>

      <div class="dlg-note">
        岗位角色（工作者 / 检验员 / 接交人）与岗位职能在「岗位角色」卡片里维护，决定工时算谁的、能承担哪些功能；
        系统角色在这里维护，决定账号能走到哪一步。两者各改各的，互不影响。
        给车间每人开一个账号，审计里的经手人才分得清。
      </div>

      <template #footer>
        <el-button @click="userVisible = false">关闭</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="userFormVisible" :title="userForm.id ? '编辑账号' : '新增账号'" width="min(540px, 94vw)">
      <el-form label-width="88px">
        <el-form-item label="账号">
          <el-input v-model="userForm.username" :disabled="!!userForm.id" placeholder="登录名，2-32 位" />
        </el-form-item>
        <el-form-item label="姓名">
          <el-input v-model="userForm.display_name" placeholder="显示名称（可空）" />
        </el-form-item>
        <el-form-item label="系统角色">
          <el-select v-model="userForm.role" style="width: 100%">
            <el-option
              v-for="r in sysRoles"
              :key="r.code"
              :value="r.code"
              :label="`${r.name} —— ${r.summary}`"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="对应人员">
          <el-select
            v-model="userForm.worker_id"
            filterable
            clearable
            placeholder="不对应车间人员（纯管理账号）"
            style="width: 100%"
          >
            <el-option v-for="w in personOptions" :key="w.id" :value="w.id" :label="w.label" />
          </el-select>
          <div class="form-hint">
            <template v-if="pickedPositions.length">
              岗位角色：
              <el-tag v-for="p in pickedPositions" :key="p" size="small" effect="plain" class="role-tag">
                {{ p }}
              </el-tag>
            </template>
            <span v-else-if="userForm.worker_id" class="muted">
              该人还没有分配岗位角色，可在「人员」里补上
            </span>
            <span v-else class="muted">不填就不挂人：适合只管账号/字典的人</span>
            <div v-if="userForm.worker_id && userForm.role !== 'admin'" class="duty-preview">
              <template v-if="pickedDuties.length">
                这个人的岗位授予：
                <el-tag
                  v-for="d in pickedDuties"
                  :key="d"
                  size="small"
                  type="primary"
                  effect="plain"
                  class="role-tag"
                >
                  {{ dutyText(d) }}
                </el-tag>
                <div class="hint-inline">保存后按这几项放行，没列出来的功能会被拒绝。</div>
              </template>
              <template v-else>
                <span class="hint-inline">
                  这些岗位都没勾职能 → 不锁人，按系统角色的默认职能走
                  （录入核对 = 任务编制 + 工艺卡编制 + 报工）。
                </span>
              </template>
            </div>
          </div>
        </el-form-item>
        <el-form-item :label="userForm.id ? '重置密码' : '初始密码'">
          <el-input
            v-model="userForm.password"
            type="password"
            show-password
            :placeholder="userForm.id ? '留空则不修改' : '至少 6 位'"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="userFormVisible = false">取消</el-button>
        <el-button type="primary" :loading="userSaving" @click="submitUser">
          {{ userForm.id ? '保存' : '创建' }}
        </el-button>
      </template>
    </el-dialog>

    <!-- 工时单价：工资 = 报工工时 × 单价。这里是唯一的定价入口。 -->
    <el-dialog
      v-model="rateVisible"
      title="工时单价"
      width="min(920px, 94vw)"
      top="6vh"
      @open="loadRates"
    >
      <el-alert
        title="单价按「生效月」留档：写进哪个月，就从那个月起生效。改价只影响该月及以后，历史月份的工资表不会跟着变。"
        type="info"
        :closable="false"
        show-icon
        class="dlg-alert"
      />

      <div class="dlg-bar">
        <span class="muted dlg-keep">生效月</span>
        <el-input v-model="rateYm" size="small" style="width: 104px" placeholder="YYYY-MM" />
        <span class="muted dlg-keep">元/小时</span>
        <div class="spacer"></div>
        <el-input
          v-model="rateKw"
          size="small"
          placeholder="搜姓名 / 工种"
          clearable
          style="width: 150px"
        />
        <span class="muted dlg-keep">
          已定价 {{ ratePriced }} / {{ rateTotal }} 人<template v-if="rateMissing">，未设 {{ rateMissing }} 人</template>
        </span>
        <el-button size="small" @click="loadRates">刷新</el-button>
        <el-button size="small" type="primary" :loading="rateSaving" @click="saveRates">
          保存修改
        </el-button>
      </div>

      <el-table :data="rateFiltered" size="small" max-height="400" style="width: 100%">
        <el-table-column prop="name" label="姓名" width="92" fixed />
        <el-table-column prop="process" label="工种" width="104" show-overflow-tooltip />
        <el-table-column prop="team" label="车间" width="78" show-overflow-tooltip />
        <el-table-column label="当前生效" width="112" align="right">
          <template #default="{ row }">
            <span v-if="row.has_rate" class="mono">{{ fmtRate(row.rate) }}</span>
            <el-tag v-else size="small" type="info" effect="plain">未设</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="单价（元/小时）" width="164">
          <template #default="{ row }">
            <el-input-number
              v-model="row.edit_rate"
              :min="0"
              :max="2000"
              :step="1"
              :precision="2"
              size="small"
              controls-position="right"
              style="width: 136px"
            />
          </template>
        </el-table-column>
        <el-table-column label="备注" min-width="170">
          <template #default="{ row }">
            <el-input v-model="row.edit_note" size="small" placeholder="选填，如「2026 年调薪」" />
          </template>
        </el-table-column>
        <el-table-column label="状态" width="86" align="center">
          <template #default="{ row }">
            <el-tag v-if="isRateDirty(row)" size="small" type="warning" effect="plain">待保存</el-tag>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
      </el-table>

      <div class="dlg-note">
        只有改动过的行会被写入；同一人同一生效月只留一条记录（再改就是改这条，并进审计）。
        单价为 0 等于「未定价」，工资页会单独列出来提醒，不会静默按 0 元发。
      </div>

      <template #footer>
        <el-button @click="rateVisible = false">关闭</el-button>
        <el-button type="primary" :loading="rateSaving" @click="saveRates">保存修改</el-button>
      </template>
    </el-dialog>

    <!-- 通用字典弹窗 -->
    <DictDialog
      v-if="dictSchema"
      :key="activeKind"
      v-model="dictVisible"
      :kind="activeKind"
      :schema="dictSchema"
      @changed="load"
    />

    <!-- 人员与角色 -->
    <WorkerDialog v-model="workerVisible" :roles="roles" @changed="load" />

    <!-- 岗位角色维护。这里改的只影响「人属于哪类名册」，与登录账号的权限无关；
         账号的系统角色在「账号」弹窗里改，两处故意分开。 -->
    <el-dialog v-model="roleVisible" title="岗位角色（车间身份 · 岗位职能）" width="min(1080px, 95vw)" top="6vh" @open="loadRoles">
      <el-alert
        title="岗位角色回答两件事：这个人在车间是什么身份（决定进哪份识别名册），以及这个岗位能承担哪几件系统功能。一个人可以挂多个岗位，职能取并集。它不等于登录账号的系统角色——账号能不能写、能不能管账号备份，在「账号」里设。"
        type="info"
        :closable="false"
        show-icon
        class="dict-tip"
      />

      <div class="dlg-bar">
        <span class="muted dlg-keep">
          共 {{ roles.length }} 个岗位，其中
          {{ roles.filter((r) => (r.duties || []).length).length }} 个已配职能
        </span>
        <div class="spacer"></div>
        <el-button size="small" @click="loadRoles">刷新</el-button>
        <el-button size="small" type="primary" @click="openRoleForm()">新增岗位角色</el-button>
      </div>

      <el-table :data="roles" size="small" max-height="420" style="width: 100%">
        <el-table-column prop="name" label="岗位角色" min-width="112" show-overflow-tooltip />
        <el-table-column prop="code" label="编码" width="96" show-overflow-tooltip />
        <el-table-column label="识别归类" width="110">
          <template #default="{ row }">
            <el-tag size="small" :type="kindType(row.kind)">{{ kindText(row.kind) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="系统职能" min-width="256">
          <template #default="{ row }">
            <template v-if="(row.duties || []).length">
              <el-tag
                v-for="d in row.duties"
                :key="d"
                size="small"
                type="primary"
                effect="plain"
                class="duty-tag"
              >
                {{ dutyText(d) }}
              </el-tag>
            </template>
            <span v-else class="muted">未配职能（沿用系统角色）</span>
          </template>
        </el-table-column>
        <el-table-column label="人数" width="62" align="right">
          <template #default="{ row }">{{ row.worker_count }}</template>
        </el-table-column>
        <el-table-column prop="description" label="说明" min-width="150" show-overflow-tooltip />
        <el-table-column label="操作" width="148" fixed="right" align="right">
          <template #default="{ row }">
            <el-button size="small" text type="primary" @click="openRoleForm(row)">编辑</el-button>
            <el-button
              size="small"
              text
              type="danger"
              :disabled="row.builtin"
              @click="removeRole(row)"
            >
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="dlg-note">
        岗位职能只决定「能不能做这件事」，不改历史数据：取消勾选后，这个人下次操作会被服务端拒绝，
        但已经报的工、已经发布的工艺卡都不变。<b>没配职能的岗位不锁人</b>，账号按系统角色的默认职能走
        （录入核对 = 任务编制 + 工艺卡编制 + 报工；只读 = 什么都不给）。
      </div>

      <el-dialog v-model="roleFormVisible" :title="roleForm.id ? '编辑岗位角色' : '新增岗位角色'" width="min(600px, 92vw)" append-to-body>
        <el-form :model="roleForm" label-width="104px">
          <el-form-item label="岗位角色名" required>
            <el-input v-model="roleForm.name" placeholder="如 计划员 / 工艺员 / 核算员" />
          </el-form-item>
          <el-form-item label="角色编码">
            <el-input v-model="roleForm.code" placeholder="留空自动生成" />
          </el-form-item>
          <el-form-item label="识别归类">
            <el-select v-model="roleForm.kind" style="width: 100%" :disabled="roleForm.builtin">
              <el-option value="worker" label="工作者 —— 匹配明细「工作者」列" />
              <el-option value="inspector" label="检验员 —— 匹配明细「检验员」列" />
              <el-option value="receiver" label="接交人 —— 匹配明细「接交人」列" />
              <el-option value="other" label="仅归类 —— 不参与识别" />
            </el-select>
            <div class="form-hint">
              只影响手写单据的姓名往哪一列匹配。计划员、工艺员这类不签在单上的岗位选「仅归类」。
            </div>
          </el-form-item>
          <el-form-item label="系统职能">
            <el-checkbox-group v-model="roleForm.duties" class="duty-group">
              <el-checkbox v-for="d in dutyOptions" :key="d.code" :value="d.code" :label="d.code">
                {{ d.label }}
              </el-checkbox>
            </el-checkbox-group>
            <div class="form-hint">
              勾了才授权。一个人挂多个岗位时取并集；一旦这个人的任一岗位勾了职能，
              没勾的那几项就不再放行（这就是「必须由专用岗位执行」）。全不勾 = 不锁人，按系统角色。
            </div>
          </el-form-item>
          <el-form-item label="说明">
            <el-input v-model="roleForm.description" type="textarea" :rows="2" />
          </el-form-item>
          <el-form-item label="排序">
            <el-input-number v-model="roleForm.sort_order" :min="0" />
          </el-form-item>
        </el-form>
        <template #footer>
          <el-button @click="roleFormVisible = false">取消</el-button>
          <el-button type="primary" :loading="roleSaving" @click="saveRole">保存</el-button>
        </template>
      </el-dialog>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api'
import { store } from '../store'
import DictDialog from '../components/DictDialog.vue'
import WorkerDialog from '../components/WorkerDialog.vue'

const route = useRoute()

const cards = ref([])
const roles = ref([])
const loading = ref(false)
const rematching = ref(false)

const dictVisible = ref(false)
const activeKind = ref('products')
const workerVisible = ref(false)
const roleVisible = ref(false)
const roleFormVisible = ref(false)
const roleSaving = ref(false)
const roleForm = ref({
  name: '',
  code: '',
  kind: 'other',
  description: '',
  sort_order: 999,
  duties: []
})
// 岗位职能勾选项：由后端给，界面不另写一份（加功能时只改一处）
const dutyOptions = ref([])

// ---- 运维：备份与账号（卡片 + 设置窗口）
const backupVisible = ref(false)
const userVisible = ref(false)
const backups = ref([])
const backupDir = ref('')
const backupKeep = ref(14)
const backingUp = ref(false)
const backupDenied = ref(false)
// 当前库规模：界面上用来证明「备份覆盖了全部这些表」，而不是只导了几张业务表
const backupSource = ref({ tables: 0, rows: 0, counts: {} })
const detailVisible = ref(false)
const detailRow = ref(null)
const users = ref([])
const me = ref('')
const userDenied = ref(false)
const userFormVisible = ref(false)
const userSaving = ref(false)
const userForm = ref({ id: 0, username: '', display_name: '', role: 'editor', worker_id: null, password: '' })

// 系统角色（权限）与权限矩阵：全部来自后端 app/domain/sys_roles.py，
// 前端不自己写一份"能做什么"的说明 —— 那种说明迟早和服务端实际拦截对不上。
const sysRoles = ref([])
const permRows = ref([])

const latestBackup = computed(() => backups.value[0] || null)
const activeUsers = computed(() => users.value.filter((u) => u.active).length)

// 名册人员：账号可以挂一个，用来回答「这个账号是谁」。
// 停用的人也列出来：历史账号要能看出它当时对应谁，只是标一下。
const personOptions = computed(() =>
  (store.dicts.workers || []).map((w) => {
    const kinds = (w.kinds || []).filter((k) => k !== 'other').map(kindText)
    const tail = [kinds.length ? kinds.join('·') : '', w.active === false ? '停用' : '']
      .filter(Boolean)
      .join(' · ')
    return { id: w.id, label: `${w.name}${tail ? `（${tail}）` : ''}` }
  })
)

// 选中人员后回显他的岗位角色：只做展示，说明「这个人平时干什么活」。
// 权限看的是上面那个系统角色，两者互不推导。
const pickedPositions = computed(() => {
  const id = userForm.value.worker_id
  if (!id) return []
  const w = (store.dicts.workers || []).find((x) => x.id === id)
  return (w?.kinds || []).filter((k) => k !== 'other').map(kindText)
})

// 该人的岗位职能并集。它决定保存后哪些功能能点——所以放在选人后面直接给出来，
// 别让人配完账号才发现「计划员做不了报工」。
const pickedDuties = computed(() => {
  const id = userForm.value.worker_id
  if (!id) return []
  const w = (store.dicts.workers || []).find((x) => x.id === id)
  return w?.duties || []
})

// 卡片只放摘要：「11-04 09:08」比整串时间更好扫
function shortTime(t) {
  return (t || '').slice(5, 16)
}

// 行数带千分位：「5083」扫一眼容易和「50,83」看混
function fmtRows(n) {
  const v = Number(n || 0)
  return v.toLocaleString('zh-CN')
}

// 校验不合格时要说清「缺什么」，只标红不解释等于让人去猜
function verifyTip(row) {
  if (row.error) return row.error
  const gap = row.schema_gap?.missing_vs_now || []
  const gapTxt = gap.length
    ? `；本份之后新增 ${gap.length} 张表（${gap.slice(0, 4).join('、')}${gap.length > 4 ? ' …' : ''}）`
    : ''
  if (row.ok) {
    const delta = row.row_delta > 0 ? `，比当前库少 ${fmtRows(row.row_delta)} 行（备份后新增）` : ''
    const scope = row.meta
      ? `与备份时清单（${row.meta.at || '—'}）逐表一致`
      : '历史备份，无出厂清单，仅核核心表'
    return `quick_check=ok，${row.tables} 张表 / ${fmtRows(row.rows)} 行，${scope}${gapTxt}${delta}`
  }
  if (row.empty) return '快照里一张表都没有'
  const miss = row.missing_tables || []
  if (miss.length) {
    return `比出厂清单少 ${miss.length} 张表：${miss.slice(0, 6).join('、')}${miss.length > 6 ? ' …' : ''}`
  }
  const core = row.core_missing || []
  if (core.length) return `缺核心表：${core.join('、')}`
  const bad = row.bad_tables || []
  if (bad.length) return `这些表读不出来：${bad.join('、')}`
  return `quick_check=${row.quick_check || '失败'}`
}

// 逐表对照：备份 vs 当前库。正数=备份后新增，负数=当前库删过行
const detailRows = computed(() => {
  const r = detailRow.value
  if (!r) return []
  const bak = r.counts || {}
  const src = backupSource.value.counts || {}
  const names = Array.from(new Set([...Object.keys(bak), ...Object.keys(src)])).sort()
  return names.map((name) => {
    const b = bak[name] ?? 0
    const s = src[name]
    return {
      name,
      bak: b,
      src: s ?? 0,
      delta: (s ?? 0) - b,
      onlyBak: s === undefined
    }
  })
})

function openDetail(row) {
  detailRow.value = row
  detailVisible.value = true
}

// 这份备份之后才新增的表：不是缺，是当时还没有，明细里点一句免得看着像少了东西
const detailGap = computed(() => detailRow.value?.schema_gap?.missing_vs_now || [])

// ---- 恢复：覆盖整库的操作，界面必须把代价说清楚再让人点
const restoreVisible = ref(false)
const restoreRow = ref(null)
const restoreStep = ref('confirm') // confirm / running / done
const restoreAck = ref(false)
const restoreResult = ref(null)
// 备份里没有、当前库有的表：恢复时这些表保留不回退，要提前点出来
const restoreKept = computed(() => restoreRow.value?.schema_gap?.missing_vs_now || [])

function openRestore(row) {
  restoreRow.value = row
  restoreAck.value = false
  restoreResult.value = null
  restoreStep.value = 'confirm'
  restoreVisible.value = true
}

function resetRestore() {
  restoreStep.value = 'confirm'
  restoreAck.value = false
  restoreResult.value = null
  restoreRow.value = null
}

async function doRestore() {
  if (!restoreRow.value || !restoreAck.value) return
  restoreStep.value = 'running'
  try {
    restoreResult.value = await api.restoreBackup(restoreRow.value.name)
    restoreStep.value = 'done'
    await loadBackups()
  } catch (e) {
    // 错误提示由请求拦截器统一弹出，这里只把界面放回可操作状态
    restoreStep.value = 'confirm'
  }
}

// 恢复后内存里的期间/产品/单据 id 全是旧的，整页重载最干净，别让人对着旧列表操作
function reloadAfterRestore() {
  location.reload()
}

function openOps(kind) {
  if (kind === 'backup') backupVisible.value = true
  else if (kind === 'rates') rateVisible.value = true
  else userVisible.value = true
}

// ---- 工时单价：工资核算的价格来源
const rateVisible = ref(false)
const rateYm = ref(store.ym || '')
const rateKw = ref('')
const rateRows = ref([])
const rateSaving = ref(false)

const rateFiltered = computed(() => {
  const kw = (rateKw.value || '').trim()
  if (!kw) return rateRows.value
  return rateRows.value.filter(
    (r) => (r.name || '').includes(kw) || (r.process || '').includes(kw)
  )
})
const rateTotal = computed(() => rateRows.value.length)
const ratePriced = computed(() => rateRows.value.filter((r) => r.has_rate).length)
const rateMissing = computed(() => rateTotal.value - ratePriced.value)

function fmtRate(v) {
  return Number(v || 0).toFixed(2)
}

// 只提交改动过的行：没动过的也提交，审计表里会堆满「0 → 0」这种噪声
function isRateDirty(row) {
  return Math.abs(Number(row.edit_rate || 0) - Number(row.rate || 0)) > 0.001 || !!row.edit_note
}

async function loadRates() {
  const ym = (rateYm.value || '').trim() || store.ym
  if (!/^\d{4}-\d{2}$/.test(ym)) {
    ElMessage.warning('生效月格式应为 YYYY-MM')
    return
  }
  const r = await api.wageRates(ym)
  rateRows.value = (r.items || []).map((x) => ({
    ...x,
    edit_rate: Number(x.rate || 0),
    edit_note: ''
  }))
}

async function saveRates() {
  const dirty = rateRows.value.filter(isRateDirty)
  if (!dirty.length) {
    ElMessage.info('没有改动')
    return
  }
  rateSaving.value = true
  try {
    const r = await api.saveWageRates({
      ym: (rateYm.value || '').trim(),
      items: dirty.map((x) => ({
        worker_id: x.worker_id,
        rate: Number(x.edit_rate || 0),
        note: x.edit_note || ''
      }))
    })
    ElMessage.success(`已保存 ${r.saved} 人（其中改价 ${r.changed} 人）`)
    await loadRates()
  } finally {
    rateSaving.value = false
  }
}

// 卡片上的「已定价 / 全部」要一进页面就能看到，否则这卡片等于没信息
async function loadRateSummary() {
  try {
    const r = await api.wageRates(store.ym || rateYm.value)
    const items = r.items || []
    rateRows.value = items.map((x) => ({ ...x, edit_rate: Number(x.rate || 0), edit_note: '' }))
  } catch {
    rateRows.value = []
  }
}

// 系统角色的中文名：优先用后端返回的 role_name，这里只做兜底（老接口没有该字段时）
function roleText(r) {
  return { admin: '管理员', editor: '录入核对', viewer: '只读' }[r] || r
}

function roleType(r) {
  return { admin: 'danger', editor: 'primary', viewer: 'info' }[r] || 'info'
}

function sysRoleSummary(code) {
  return sysRoles.value.find((r) => r.code === code)?.description || roleText(code)
}

// 某系统角色在「没配岗位职能」时默认拿到哪些职能（矩阵表最后三列用）
function defaultDuties(code) {
  return sysRoles.value.find((r) => r.code === code)?.default_duties || []
}

async function loadSystemRoles() {
  try {
    const r = await api.systemRoles()
    sysRoles.value = r.roles || []
    permRows.value = r.perms || []
    if (r.duties?.length) dutyOptions.value = r.duties
  } catch {
    // 拿不到就退化成内置三类：账号页不能因为这张说明表打不开就整个不能用
    sysRoles.value = [
      { code: 'admin', name: '管理员', summary: '全部权限', perms: [] },
      { code: 'editor', name: '录入核对', summary: '业务与基础数据', perms: [] },
      { code: 'viewer', name: '只读', summary: '只能查看', perms: [] }
    ]
    permRows.value = []
  }
}

async function loadBackups() {
  try {
    const r = await api.backups()
    backups.value = r.items || []
    backupDir.value = r.dir || ''
    if (r.source) backupSource.value = r.source
    backupDenied.value = false
  } catch (e) {
    backups.value = []
    backupDenied.value = true
  }
}

async function doBackup() {
  backingUp.value = true
  try {
    const r = await api.runBackup(backupKeep.value)
    const pruned = r.pruned?.length ? `，清理 ${r.pruned.length} 份旧备份` : ''
    ElMessage.success(
      `已备份 ${r.name}：${r.tables} 张表 / ${fmtRows(r.rows)} 行（${r.size_kb}KB，校验 ${r.quick_check}${pruned}）`
    )
    await loadBackups()
  } finally {
    backingUp.value = false
  }
}

async function loadUsers() {
  try {
    const r = await api.authUsers()
    users.value = r.items || []
    me.value = r.current || ''
    userDenied.value = false
  } catch (e) {
    users.value = []
    userDenied.value = true
  }
}

function openUserForm(row) {
  userForm.value = row
    ? {
        id: row.id,
        username: row.username,
        display_name: row.display_name || '',
        role: row.role,
        worker_id: row.worker_id || null,
        password: ''
      }
    : {
        id: 0,
        username: '',
        display_name: '',
        role: sysRoles.value.find((r) => r.code === 'editor')?.code || 'editor',
        worker_id: null,
        password: ''
      }
  userFormVisible.value = true
}

async function submitUser() {
  const f = userForm.value
  const editing = !!f.id
  if (!editing && (!f.username.trim() || (f.password || '').length < 6)) {
    return ElMessage.warning('账号必填，密码至少 6 位')
  }
  if (editing && f.password && f.password.length < 6) return ElMessage.warning('密码至少 6 位')
  userSaving.value = true
  try {
    if (editing) {
      const payload = {
        display_name: f.display_name || f.username,
        role: f.role,
        worker_id: f.worker_id || null,
        // 显式解绑：worker_id=null 在接口上分不清「没传」和「要清空」
        clear_worker: !f.worker_id
      }
      if (f.password) payload.password = f.password
      await api.updateUser(f.id, payload)
      ElMessage.success('账号已更新')
    } else {
      await api.createUser({
        username: f.username.trim(),
        password: f.password,
        display_name: f.display_name || f.username,
        role: f.role,
        worker_id: f.worker_id || null
      })
      ElMessage.success('账号已创建')
    }
    userFormVisible.value = false
    await loadUsers()
  } finally {
    userSaving.value = false
  }
}

async function toggleUser(row) {
  const action = row.active ? '停用' : '启用'
  try {
    await ElMessageBox.confirm(
      `确定${action}账号「${row.username}」？${row.active ? '停用后该账号的登录态立即失效。' : ''}`,
      `${action}账号`,
      { type: 'warning' }
    )
  } catch {
    return
  }
  await api.updateUser(row.id, { active: !row.active })
  ElMessage.success(`已${action}`)
  await loadUsers()
}

async function resetPassword(row) {
  let value = ''
  try {
    const r = await ElMessageBox.prompt(`为「${row.username}」设置新密码（至少 6 位）`, '重置密码', {
      inputType: 'password'
    })
    value = (r.value || '').trim()
  } catch {
    return
  }
  if (value.length < 6) return ElMessage.warning('密码至少 6 位')
  await api.updateUser(row.id, { password: value })
  ElMessage.success('密码已重置，对方需重新登录')
  await loadUsers()
}

const totalEntries = computed(() =>
  cards.value.filter((c) => c.key !== 'roles').reduce((s, c) => s + c.count, 0)
)
const activePeople = computed(() => cards.value.find((c) => c.key === 'workers')?.active ?? 0)
const unregistered = computed(() => cards.value.reduce((s, c) => s + (c.gap || 0), 0))

const SCHEMAS = {
  products: {
    title: '产品名称管理',
    searchHint: '产品名称 / 代号 / 别名',
    tip: '表头「产品名称 / 产品代号」按此收敛。别名可列出同一产品的多种写法；识别时精确命中的写法会原样保留，只有错字才纠正为标准名。',
    importHint: '每行一个产品：产品名称, 代号, 别名（逗号分隔）, 规格',
    importSample: 'YUJ-18BZ, YUJ-18BZ, 鱼机/YUJ18BZ, 鱼机 18BZ',
    importColumns: '名称 / 代号 / 别名 / 规格',
    columns: [
      { prop: 'name', label: '产品名称', minWidth: 150 },
      { prop: 'code', label: '代号', width: 130 },
      { prop: 'spec', label: '规格', width: 130 },
      { prop: 'aliases', label: '别名', minWidth: 180 },
      { prop: 'active', label: '状态', width: 78, type: 'bool' }
    ],
    fields: [
      { prop: 'name', label: '产品名称', required: true, placeholder: '与单据上写法一致，如 YUJ-18BZ' },
      { prop: 'code', label: '产品代号', placeholder: '选填' },
      { prop: 'spec', label: '规格型号', placeholder: '选填' },
      { prop: 'aliases', label: '识别别名', type: 'textarea', hint: '逗号分隔，如 鱼机,YUJ18BZ' },
      { prop: 'active', label: '启用', type: 'bool' }
    ]
  },
  processes: {
    title: '工序字典管理',
    searchHint: '工序名 / 编码 / 工种',
    tip: '明细「工序」列按此收敛。别名覆盖手写变体（铣1 / 铣床 / 铣工），停用的工序不再参与匹配。工艺默认序 = 这道工序通常在路线里排在第几段（备料→车铣→钳→焊→装配），生成工艺路线时按它排序，可按本厂实际改。',
    importHint: '每行一个工序：编码, 名称, 工种, 别名（逗号分隔）',
    importSample: 'XI, 铣, 铣工, 铣/铣1/铣床',
    importColumns: '编码 / 名称 / 工种 / 别名',
    columns: [
      { prop: 'code', label: '编码', width: 120 },
      { prop: 'name', label: '工序名称', width: 130 },
      { prop: 'category', label: '所属工种', width: 120 },
      { prop: 'default_seq', label: '工艺序', width: 82, type: 'number' },
      { prop: 'aliases', label: '别名', minWidth: 200 },
      { prop: 'active', label: '状态', width: 78, type: 'bool' }
    ],
    fields: [
      { prop: 'name', label: '工序名称', required: true, placeholder: '如 铣' },
      { prop: 'code', label: '工序编码', placeholder: '留空自动生成' },
      { prop: 'category', label: '所属工种', placeholder: '如 铣工' },
      {
        prop: 'default_seq',
        label: '工艺默认序',
        type: 'number',
        hint: '越小越靠前。备料 10 / 切削 20 / 钳工 40 / 钣金 50 / 焊 60 / 装配 70，段内留空位便于插入'
      },
      { prop: 'aliases', label: '识别别名', type: 'textarea', hint: '逗号分隔，如 铣,铣工,铣1,铣床' },
      { prop: 'active', label: '启用', type: 'bool' }
    ]
  },
  parts: {
    title: '件名字典管理',
    searchHint: '件名 / 别名 / 归类',
    tip: '明细「件名」栏按此收敛。件名是零件（机架 / 刀轮 / 进料斗），工序是干法，两者组合才定得出单件工时。新零件随时会出现，没收录的不会拦，只提示补充。',
    importHint: '每行一个件名：件名, 别名（逗号分隔）, 归类',
    importSample: '机架, 机架体/主机架,\n刀轮, 刀具轮/切刀轮, 制丸机',
    importColumns: '件名 / 别名 / 归类',
    columns: [
      { prop: 'name', label: '件名', width: 150 },
      { prop: 'category', label: '归类', width: 130 },
      { prop: 'aliases', label: '别名', minWidth: 200 },
      { prop: 'active', label: '状态', width: 78, type: 'bool' }
    ],
    fields: [
      { prop: 'name', label: '件名', required: true, placeholder: '与单据上写法一致，如 机架' },
      { prop: 'category', label: '归类', placeholder: '所属设备，选填' },
      { prop: 'aliases', label: '识别别名', type: 'textarea', hint: '逗号分隔，如 机架体,主机架' },
      { prop: 'active', label: '启用', type: 'bool' }
    ]
  },
  materials: {
    title: '材料字典管理',
    searchHint: '材料名称 / 编码 / 别名',
    tip: '表头「材料」栏按此收敛。别名可覆盖 Q235 / 45# / 304 这类手写写法。',
    importHint: '每行一种材料：材料名称, 编码, 别名（逗号分隔）, 规格',
    importSample: 'Q235, Q235, Q235B/235, 碳素结构钢',
    importColumns: '名称 / 编码 / 别名 / 规格',
    columns: [
      { prop: 'name', label: '材料', width: 140 },
      { prop: 'code', label: '编码', width: 110 },
      { prop: 'spec', label: '规格/牌号', width: 160 },
      { prop: 'aliases', label: '别名', minWidth: 180 },
      { prop: 'active', label: '状态', width: 78, type: 'bool' }
    ],
    fields: [
      { prop: 'name', label: '材料名称', required: true, placeholder: '如 Q235' },
      { prop: 'code', label: '材料编码', placeholder: '选填' },
      { prop: 'spec', label: '规格/牌号', placeholder: '选填' },
      { prop: 'aliases', label: '识别别名', type: 'textarea', hint: '逗号分隔，如 Q235B,235' },
      { prop: 'active', label: '启用', type: 'bool' }
    ]
  }
}

const dictSchema = computed(() => SCHEMAS[activeKind.value] || null)

function iconOf(key) {
  return (
    {
      products: 'Box',
      processes: 'SetUp',
      parts: 'Tools',
      materials: 'Files',
      workers: 'UserFilled',
      roles: 'Key'
    }[key] || 'Setting'
  )
}

function kindText(kind) {
  return { worker: '工作者', inspector: '检验员', receiver: '接交人', other: '仅归类' }[kind] || kind
}

function kindType(kind) {
  return { worker: 'primary', inspector: 'warning', receiver: 'success', other: 'info' }[kind] || 'info'
}

// 岗位职能：文案由后端下发，前端只查表。查不到就把 code 显示出来，
// 免得以后后端加了新职能、前端没跟上时显示成空白。
function dutyText(code) {
  return dutyOptions.value.find((d) => d.code === code)?.label || code
}

async function loadRoles() {
  try {
    const r = await api.adminList('roles')
    roles.value = r.items || []
    if (r.duty_options?.length) dutyOptions.value = r.duty_options
  } catch (e) {
    // 非管理员打开会 403：保留 overview 里已有的一份，不让弹窗空着
  }
}

async function load() {
  loading.value = true
  try {
    const r = await api.adminOverview()
    cards.value = r.cards
    roles.value = r.roles
    if (r.duty_options?.length) dutyOptions.value = r.duty_options
  } finally {
    loading.value = false
  }
}

function openCard(card) {
  if (card.key === 'workers') {
    workerVisible.value = true
    return
  }
  if (card.key === 'roles') {
    roleVisible.value = true
    loadRoles()
    return
  }
  activeKind.value = card.key
  dictVisible.value = true
}

function openRoleForm(row) {
  roleForm.value = row
    ? { ...row, duties: [...(row.duties || [])] }
    : { name: '', code: '', kind: 'other', description: '', sort_order: 999, builtin: false, duties: [] }
  roleFormVisible.value = true
}

async function saveRole() {
  if (!roleForm.value.name?.trim()) return ElMessage.warning('请填写岗位角色名称')
  roleSaving.value = true
  try {
    const payload = {
      name: roleForm.value.name,
      code: roleForm.value.code || '',
      kind: roleForm.value.kind,
      description: roleForm.value.description || '',
      sort_order: roleForm.value.sort_order ?? 999,
      duties: roleForm.value.duties || []
    }
    if (roleForm.value.id) await api.adminUpdate('roles', roleForm.value.id, payload)
    else await api.adminCreate('roles', payload)
    ElMessage.success('已保存')
    roleFormVisible.value = false
    await Promise.all([loadRoles(), load()])
  } finally {
    roleSaving.value = false
  }
}

async function removeRole(row) {
  await ElMessageBox.confirm(`确认删除角色「${row.name}」？该角色下的人员不会被删除。`, '删除确认', {
    type: 'warning',
    confirmButtonText: '删除',
    cancelButtonText: '取消'
  })
  await api.adminDelete('roles', row.id)
  ElMessage.success('已删除')
  await Promise.all([loadRoles(), load()])
}

async function doRematch() {
  rematching.value = true
  try {
    const r = await api.adminRematch()
    ElMessage.success(
      `已回补 ${r.sheets} 张单据 / ${r.items} 条明细（扫描 ${r.scanned} 张）`
    )
    await load()
  } finally {
    rematching.value = false
  }
}

onMounted(async () => {
  await load()
  // 备份、账号、单价不阻塞主内容：拿不到也不该让整页报错（如非管理员）
  loadBackups()
  loadUsers()
  loadRateSummary()
  loadSystemRoles()
  // 支持深链：#/admin?open=products / #/admin?card=rates
  // 前者是管理项，后者从工资核算页「去设置」跳过来
  const open = route.query.open || route.query.card
  if (open === 'backup' || open === 'users' || open === 'rates') {
    openOps(open)
    return
  }
  if (open) {
    const card = cards.value.find((c) => c.key === open)
    if (card) openCard(card)
  }
})

// 已经在 /admin 里再被「去设置」跳一次时，路由 query 变了但组件不会重新挂载，
// 只靠 onMounted 的话弹窗不出现，看着像按钮没反应
watch(
  () => [route.query.open, route.query.card],
  ([open, card]) => {
    const key = open || card
    if (!key) return
    if (key === 'backup' || key === 'users' || key === 'rates') {
      openOps(key)
      return
    }
    const hit = cards.value.find((c) => c.key === key)
    if (hit) openCard(hit)
  }
)
</script>

<style scoped>
.ic-backup { background: linear-gradient(135deg, #6a8bd0, #3f63ad); }
.ic-account { background: linear-gradient(135deg, #7fb7a8, #3f8f7b); }
.ic-rates { background: linear-gradient(135deg, #f0b45e, #d68a1c); }

/* ---- 设置窗口 */
.dlg-alert { margin-bottom: 12px; }
.dlg-alert :deep(.el-alert__title) { font-size: 12.5px; line-height: 1.6; }
/* 表数与当前库不一致 / 备份里多出来的表：用颜色把「这份备份不完整」直接指出来 */
.num-bad { color: #d14343; font-weight: 600; }
.num-warn { color: #c07a11; }

/* ---- 恢复确认 */
.restore-cmp {
  width: 100%;
  border-collapse: collapse;
  font-size: 12.5px;
  margin-bottom: 12px;
}
.restore-cmp th,
.restore-cmp td {
  border: 1px solid #e4e8ef;
  padding: 6px 9px;
  text-align: left;
}
.restore-cmp thead th,
.restore-cmp tr:first-child th {
  background: #f6f8fc;
  font-weight: 600;
  color: #4b5563;
}
.restore-cmp td:first-child {
  width: 76px;
  color: #6b7280;
  background: #fafbfd;
}
.restore-warn {
  margin: 0 0 12px;
  padding-left: 18px;
  font-size: 12.5px;
  line-height: 1.85;
  color: #4b5563;
}
.restore-warn strong { color: #b42318; font-weight: 600; }
.restore-warn code,
.restore-done code {
  background: #f2f4f8;
  padding: 1px 4px;
  border-radius: 3px;
  font-size: 11.5px;
}
.restore-ack { font-size: 12.5px; }
.restore-done {
  font-size: 12.5px;
  line-height: 2;
  color: #4b5563;
  text-align: left;
}
.restore-kept { color: #c07a11; }

/* ---- 系统角色权限矩阵：表小、字小，不喧宾夺主，但要能一眼对上口径 */
.perm-matrix {
  margin-top: 12px;
  border: 1px solid #e8ebf0;
  border-radius: 8px;
  overflow: hidden;
}
.pm-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  padding: 8px 12px;
  background: #f7f9fc;
  border-bottom: 1px solid #e8ebf0;
  flex-wrap: wrap;
}
.pm-title { font-size: 12.5px; font-weight: 600; color: #303133; }
.pm-sub { font-size: 11.5px; }
.perm-matrix table { width: 100%; border-collapse: collapse; font-size: 12px; }
.perm-matrix th,
.perm-matrix td {
  padding: 6px 10px;
  text-align: center;
  border-bottom: 1px solid #f0f2f5;
  color: #909399;
}
.perm-matrix th { font-weight: 600; color: #606266; background: #fcfdff; }
.perm-matrix tbody tr:last-child td { border-bottom: none; }
.perm-matrix .pm-perm { text-align: left; color: #4b5563; white-space: nowrap; }
/* 「管住的操作」一列是长句子：左对齐、允许换行，别居中成一坨 */
.perm-matrix .pm-scope { text-align: left; color: #909399; font-size: 11.5px; line-height: 1.5; }
.pm-foot {
  padding: 7px 12px;
  font-size: 11.5px;
  line-height: 1.7;
  color: #909399;
  background: #fcfdff;
  border-top: 1px solid #f0f2f5;
}
/* 账号表「有效职能」里的补充说明：小一号灰字，不抢视觉 */
.hint-inline { font-size: 11px; color: #b0b6c0; margin-top: 1px; }
/* 选人后预演「保存后会拿到哪些职能」 */
.duty-preview { margin-top: 4px; padding-top: 4px; border-top: 1px dashed #eef0f4; }
/* 职能对照表：三个角色的列收窄，把宽度留给「管住的操作」 */
.duty-matrix th:nth-child(n + 3),
.duty-matrix td:nth-child(n + 3) { width: 96px; }
.perm-matrix td.pm-yes { color: #1f9254; font-weight: 700; }
.pm-code {
  display: block;
  font-size: 10.5px;
  font-weight: 400;
  color: #b0b6c0;
  font-family: ui-monospace, Consolas, monospace;
}
/* 账号表里人员姓名旁的岗位角色小标签（与 WorkerDialog 里的观感保持一致） */
.role-tag { margin-left: 4px; }
/* 岗位职能标签：一行放不下就换行，别把「生产任务编制」压成两行 */
.duty-tag { margin: 0 4px 3px 0; }
.form-hint {
  font-size: 11.5px;
  line-height: 1.9;
  color: #909399;
  margin-top: 2px;
}
/* 职能勾选框：4 项按 2 列排，弹窗不会拉太高 */
.duty-group { display: grid; grid-template-columns: 1fr 1fr; gap: 0 16px; }
/* 勾选项标签不折行：折成两行会让 4 个职能看起来像 8 个 */
.duty-group :deep(.el-checkbox) { margin-right: 0; height: 26px; }
.duty-group :deep(.el-checkbox__label) { font-size: 12.5px; white-space: nowrap; }
.form-hint .role-tag { margin-right: 2px; }
/* 人员已停用/名册里查不到：灰显但保留原文，别让人以为账号挂错了人 */
.text-off { color: #b0b6c0; }

.dlg-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
  flex-wrap: wrap;
}
.dlg-path {
  font-size: 12px;
  color: #6b7280;
  max-width: 420px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.dlg-keep { font-size: 12px; }
.dlg-empty { font-size: 12px; padding: 10px 2px 0; line-height: 1.7; }
.dlg-empty code { background: #f2f4f8; padding: 1px 4px; border-radius: 3px; }
.dlg-note {
  margin-top: 10px;
  padding: 8px 10px;
  background: #f7f9fc;
  border-left: 3px solid #d5deec;
  border-radius: 4px;
  font-size: 12px;
  color: #6b7280;
  line-height: 1.7;
}

.card-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 14px;
}

.admin-card {
  background: #fff;
  border: 1px solid #e7ebf1;
  border-radius: 12px;
  padding: 16px 18px 12px;
  cursor: pointer;
  transition: transform 0.16s, box-shadow 0.16s, border-color 0.16s;
  display: flex;
  flex-direction: column;
  gap: 10px;
  /* 统一高度：卡片并排时两行不能一行高一行矮，内容少的靠 card-tip 的 flex 撑住 */
  min-height: 240px;
}

.admin-card:hover {
  transform: translateY(-2px);
  border-color: #c7d7ef;
  box-shadow: 0 8px 22px rgba(24, 55, 105, 0.1);
}

.admin-card:hover .go-text,
.admin-card:hover .go-icon {
  color: #1f6fd0;
}

.card-top {
  display: flex;
  align-items: center;
  gap: 11px;
}

.card-icon {
  width: 38px;
  height: 38px;
  border-radius: 10px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
  flex: 0 0 38px;
}

.ic-products { background: linear-gradient(135deg, #4f8cff, #2563eb); }
.ic-processes { background: linear-gradient(135deg, #34c08a, #159d6c); }
.ic-materials { background: linear-gradient(135deg, #f5a623, #e0861a); }
.ic-workers { background: linear-gradient(135deg, #8b6bf0, #6b46d6); }
.ic-roles { background: linear-gradient(135deg, #5aa9d6, #2f7ead); }

.card-title {
  font-size: 15px;
  font-weight: 600;
  color: #1f2430;
}

.card-sub {
  font-size: 12px;
  color: #8b93a1;
  margin-top: 2px;
}

.card-metric {
  display: flex;
  align-items: baseline;
  gap: 4px;
}

.metric-num {
  font-size: 30px;
  font-weight: 600;
  line-height: 1;
  color: #1f2430;
  font-variant-numeric: tabular-nums;
}

.metric-unit {
  font-size: 12.5px;
  color: #9aa3b2;
}

.metric-dim {
  font-size: 12px;
  color: #9aa3b2;
  margin-left: 4px;
}

.card-extra {
  min-height: 22px;
}

.extra-text {
  font-size: 12px;
  color: #6b7280;
}

.card-tip {
  font-size: 12px;
  color: #8b93a1;
  line-height: 1.55;
  flex: 1;
}

.card-foot {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 4px;
  border-top: 1px dashed #eef1f6;
  padding-top: 9px;
}

.go-text {
  font-size: 12.5px;
  color: #9aa3b2;
  transition: color 0.16s;
}

.go-icon {
  color: #b6bfcd;
  font-size: 12px;
  transition: color 0.16s;
}

.dict-tip {
  margin-bottom: 10px;
}
</style>
