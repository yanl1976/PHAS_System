import axios from 'axios'
import { ElMessage } from 'element-plus'

// 登录令牌：身份由服务端签发，写操作的审计人取的就是它。
// 不再把「操作人姓名」放进请求体 —— 那等于让前端自己决定留痕写谁的名字。
export const TOKEN_KEY = 'wt_token'
export const USER_KEY = 'wt_user'

export function getToken() {
  return localStorage.getItem(TOKEN_KEY) || ''
}

export function setSession(token, user) {
  if (token) localStorage.setItem(TOKEN_KEY, token)
  if (user) localStorage.setItem(USER_KEY, JSON.stringify(user))
}

export function clearSession() {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(USER_KEY)
}

export function currentUser() {
  try {
    return JSON.parse(localStorage.getItem(USER_KEY) || 'null')
  } catch (e) {
    return null
  }
}

const client = axios.create({ baseURL: '', timeout: 60000 })

client.interceptors.request.use((cfg) => {
  const token = getToken()
  if (token) cfg.headers.Authorization = `Bearer ${token}`
  return cfg
})

let redirecting = false

client.interceptors.response.use(
  (r) => r.data,
  (err) => {
    const status = err?.response?.status
    const detail = err?.response?.data?.detail
    // 后端用 {code, message} 结构表达「可判断的错误」（如跨期需确认），
    // 直接拼 JSON 会显示成一坨，界面就看不懂该点什么了
    const msg =
      typeof detail === 'string'
        ? detail
        : detail?.message || (detail ? JSON.stringify(detail) : err.message)
    const code = detail?.code || ''
    if (status === 401 && !redirecting) {
      // 令牌过期/账号停用：清掉本地会话并回登录页，别让人在页面上反复点报错
      redirecting = true
      clearSession()
      if (!location.hash.startsWith('#/login')) location.hash = '#/login'
      setTimeout(() => (redirecting = false), 1500)
    }
    if (!err.config?.silent) ElMessage.error(msg || '请求失败')
    err.detailText = msg
    err.detailCode = code
    return Promise.reject(err)
  }
)

export const api = {
  health: () => client.get('/api/health'),
  // ---- 登录与账号
  authStatus: () => client.get('/api/auth/status', { silent: true }),
  login: (username, password) => client.post('/api/auth/login', { username, password }),
  me: () => client.get('/api/auth/me', { silent: true }),
  changePassword: (old_password, new_password) =>
    client.post('/api/auth/password', { old_password, new_password }),
  authUsers: () => client.get('/api/auth/users'),
  createUser: (data) => client.post('/api/auth/users', data),
  updateUser: (id, data) => client.put(`/api/auth/users/${id}`, data),
  // 系统角色（权限）定义与矩阵。与「岗位角色」不是一回事，见 app/domain/sys_roles.py
  systemRoles: () => client.get('/api/auth/system-roles'),
  // ---- 备份与恢复
  backups: () => client.get('/api/admin/backups'),
  runBackup: (keep = 14) => client.post('/api/admin/backup', null, { params: { keep } }),
  // 恢复是覆盖整库的操作：只传备份文件名，路径由服务端在备份目录里解析
  restoreBackup: (name, force = false) => client.post('/api/admin/restore', { name, force }),
  dicts: () => client.get('/api/dicts'),
  stats: (ym) => client.get('/api/stats', { params: { ym } }),

  periods: () => client.get('/api/periods'),
  createPeriod: (data) => client.post('/api/periods', data),
  lockPeriod: (id) => client.post(`/api/periods/${id}/lock`),
  unlockPeriod: (id) => client.post(`/api/periods/${id}/unlock`),

  batches: (period_id) => client.get('/api/batches', { params: { period_id } }),
  createBatch: (data) => client.post('/api/batches', data),
  batchImages: (id) => client.get(`/api/batches/${id}/images`),
  upload: (batchId, files, auto = true) => {
    const fd = new FormData()
    files.forEach((f) => fd.append('files', f))
    return client.post(`/api/batches/${batchId}/images?auto_recognize=${auto}`, fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 120000
    })
  },
  retryFailed: (batchId) => client.post(`/api/batches/${batchId}/retry-failed`),
  image: (id) => client.get(`/api/images/${id}`),
  reocrImage: (id) => client.post(`/api/images/${id}/reocr`, {}),

  sheets: (params) => client.get('/api/sheets', { params }),
  sheet: (id) => client.get(`/api/sheets/${id}`),
  sheetAudit: (id) => client.get(`/api/sheets/${id}/audit`),
  // 打印：走浏览器内置 PDF 阅读器，版式与车间空白单一致
  sheetPrintUrl: (id, download = false) =>
    `/api/sheets/${id}/print.pdf${download ? '?download=true' : ''}`,
  sheetKeeps: (id) => client.get(`/api/sheets/${id}/keeps`),
  updateSheet: (id, data) => client.patch(`/api/sheets/${id}`, data),
  // 确认：跨期单据后端会返回 409（code=OUT_OF_PERIOD），界面上二次确认后再传 confirm
  verifySheet: (id, { force = false, confirmOutOfPeriod = false } = {}) =>
    client.post(`/api/sheets/${id}/verify`, {
      force,
      confirm_out_of_period: confirmOutOfPeriod
    }),
  batchVerify: (ids, { confirmOutOfPeriod = false } = {}) =>
    client.post('/api/sheets/batch-verify', {
      sheet_ids: ids,
      confirm_out_of_period: confirmOutOfPeriod
    }),
  rejectSheet: (id, reason) => client.post(`/api/sheets/${id}/reject`, { reason }),
  reopenSheet: (id) => client.post(`/api/sheets/${id}/reopen`),
  // 作废可逆、删除不可逆；两个入口都要原因，留痕才说得清是谁动的
  restoreSheet: (id) => client.post(`/api/sheets/${id}/restore`),
  deleteSheet: (id, reason) => client.delete(`/api/sheets/${id}`, { params: { reason } }),
  // 已确认单据的维度补录（产品/件名/工序），不动金额、不抹确认时间
  updateSheetDims: (id, items, reason) =>
    client.patch(`/api/sheets/${id}/dims`, { items, reason }),
  // 改挂任务：归集是自动的，归错时必须有人工出口（否则只能改库）
  moveSheetTask: (id, data) => client.post(`/api/sheets/${id}/task`, data),
  batchMoveSheetTask: (data) => client.post('/api/sheets/batch-task', data),
  reocrSheet: (id, keepManual = true) =>
    client.post(`/api/sheets/${id}/reocr`, null, { params: { keep_manual: keepManual } }),
  workQueue: (ym, limit = 8) => client.get('/api/work-queue', { params: { ym, limit } }),

  summary: (ym, include_unverified = false) =>
    client.get('/api/summary', { params: { ym, include_unverified } }),
  matrix: (ym) => client.get('/api/summary/matrix', { params: { ym } }),
  reconcile: (ym) => client.get('/api/reconcile', { params: { ym } }),
  exportUrl: (ym, preview = false) => `/api/export?ym=${ym}&preview=${preview}`,
  exportHistory: () => client.get('/api/export/history'),

  // ---- 工资核算：报工工时 × 该月生效单价
  wages: (ym, include_unverified = false) =>
    client.get('/api/wages', { params: { ym, include_unverified } }),
  wageRates: (ym) => client.get('/api/wages/rates', { params: { ym } }),
  saveWageRates: (data) => client.put('/api/wages/rates', data),
  // 工时工资表：给一个同源 URL，由浏览器自己走下载。
  //
  // 以前是 axios 取 blob + <a download> 落地，看着更「可控」，实际更脆：
  // blob: 下载在部分内嵌浏览器/受限预览环境里会被静默拦掉 —— 点了没反应、
  // 也没有报错，用户只会以为按钮坏了。生成的文件本来就落在服务端
  // data/exports 下，直接让浏览器访问这个地址最省事：
  // 响应头是 attachment，浏览器原生下载，文件名也由后端决定（中文不乱码）。
  // 与「导出与勾稽」页的 window.open 保持同一条路，两处行为才一致。
  wageExportUrl: (ym, include_unverified = false, preview = false) =>
    `/api/wages/export?ym=${encodeURIComponent(ym)}&include_unverified=${include_unverified}&preview=${preview}`,
  // 历史导出列表：导出被浏览器拦掉时的兜底入口（同一个目录，直接点链接下载）
  wageExports: () => client.get('/api/export/history'),

  // ---- 扫码报工：印单 → 扫码 → 提交
  reportCreateForms: (data) => client.post('/api/report/forms', data),
  reportFormList: (params) => client.get('/api/report/forms', { params }),
  reportScan: (code) => client.get('/api/report/scan', { params: { code } }),
  reportSubmit: (data) => client.post('/api/report/submit', data),
  // 撤销必须带原因：撤销是删钱的动作，只留一条 undo 等于没留痕
  reportUndo: (form_no, reason) =>
    client.post('/api/report/undo', null, { params: { form_no, reason } }),
  reportVoid: (data) => client.post('/api/report/void', data),
  // 报工台账的「扫码报工」一侧（以预印单为主键，不是 sheet）
  reportLedger: (params) => client.get('/api/report/ledger', { params }),
  reportLedgerDetail: (form_no) => client.get(`/api/report/ledger/${form_no}`),

  // ---- 系统管理
  adminOverview: () => client.get('/api/admin/overview'),
  adminList: (kind, params) => client.get(`/api/admin/${kind}`, { params }),
  adminCreate: (kind, data) => client.post(`/api/admin/${kind}`, data),
  adminUpdate: (kind, id, data) => client.put(`/api/admin/${kind}/${id}`, data),
  adminDelete: (kind, id) => client.delete(`/api/admin/${kind}/${id}`),
  adminImport: (kind, text, mode = 'append') => client.post(`/api/admin/${kind}/import`, { text, mode }),
  adminImportFile: (kind, file, mode = 'append') => {
    const fd = new FormData()
    fd.append('file', file)
    fd.append('mode', mode)
    return client.post(`/api/admin/${kind}/import-file`, fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 120000
    })
  },
  adminTemplateUrl: (kind) => `/api/admin/${kind}/template`,
  adminRematch: (ym) => client.post('/api/admin/rematch', null, { params: { ym } }),

  // ---- 工时组合（产品 × 件名 台账）
  combos: (params) => client.get('/api/combos', { params }),
  comboDetail: (product, part) => client.get('/api/combos/detail', { params: { product, part } }),
  comboAddStep: (data) => client.post('/api/combos/step', data),
  comboUpdateStep: (id, data) => client.put(`/api/combos/step/${id}`, data),
  comboDeleteStep: (id) => client.delete(`/api/combos/step/${id}`),
  comboRename: (data) => client.post('/api/combos/rename', data),
  comboSetActive: (data) => client.post('/api/combos/active', data),
  comboCreate: (data) => client.post('/api/combos', data),
  // 发布控制：只有 released 的组合才会被任务挂线、核对校验、工艺卡引用
  comboTrees: (params) => client.get('/api/combos/tree', { params }),
  comboRelease: (data) => client.post('/api/combos/release', data),
  comboWithdraw: (data) => client.post('/api/combos/withdraw', data),
  comboBatchRelease: (data) => client.post('/api/combos/batch-release', data),
  comboSetLevel: (data) => client.patch('/api/combos/level', data),
  // 方向键调层级：up/down 调顺序，indent 进一级，outdent 退一级
  comboMove: (data) => client.post('/api/combos/move', data),
  // 撤销上一次方向键操作：把 move 返回的 undo 快照原样送回来
  comboUndoMove: (data) => client.post('/api/combos/undo-move', data),
  // 一批件同时挂到同一个上级（parent_part 空串 = 提到最上层）
  comboAttach: (data) => client.post('/api/combos/attach', data),
  comboAutoNest: (data) => client.post('/api/combos/auto-nest', data),
  // 改层级弹窗用的真实层级树（只认 parent_id，带 blocked=自己+子孙）
  comboLevelTree: (product, part = '') =>
    client.get('/api/combos/level-tree', { params: { product, part } }),
  comboRelayout: (product = '') => client.post('/api/combos/relayout', null, { params: { product } }),
  comboSync: () => client.post('/api/combos/sync'),
  // 在现有结构里新增一件（parent_part 空 = 挂顶层）
  comboCreateNode: (data) => client.post('/api/combos/node', data),
  comboDeleteNode: (product, part) =>
    client.delete('/api/combos/node', { params: { product, part } }),
  // 计划生产数量：改上级下级按用量重算，改下级上级按用量反推
  comboSetPlan: (data) => client.patch('/api/combos/plan', data),

  // ---- 工艺卡（只读投影：已发布组合 + 生效路线 + 部件层级）
  cards: (params) => client.get('/api/cards', { params }),
  cardProducts: () => client.get('/api/cards/products'),
  cardTasks: () => client.get('/api/cards/tasks'),
  // 工艺卡打印出来就是《加工路线单》：页面里直接显示后端渲染的那张纸，
  // 而不是前端再照着自己的表拼一遍 —— 拼出来的版式跟车间实印的不是一张。
  // 用 blob 而不是 <img src>，是为了带上 Authorization（严格鉴权模式下 401）。
  cardPng: async (params) => {
    const blob = await client.get('/api/cards/print.png', { params, responseType: 'blob' })
    return URL.createObjectURL(blob)
  },
  cardPdfUrl: (params) => '/api/cards/print.pdf?' + new URLSearchParams(params).toString(),

  // ---- 工艺路线
  routes: (params) => client.get('/api/routes', { params }),
  route: (id) => client.get(`/api/routes/${id}`),
  routeTree: (params) => client.get('/api/routes/tree', { params }),
  routeCoverage: (limit = 200) => client.get('/api/routes/coverage', { params: { limit } }),
  routeSuggest: (product, part) =>
    client.get('/api/routes/suggest', { params: { product, part } }),
  createRoute: (data) => client.post('/api/routes', data),
  updateRoute: (id, data) => client.put(`/api/routes/${id}`, data),
  batchGenerateRoutes: (data) => client.post('/api/routes/batch-generate', data),
  publishRoute: (id, newVersion = false) =>
    client.post(`/api/routes/${id}/publish`, { new_version: newVersion }),
  archiveRoute: (id) => client.post(`/api/routes/${id}/archive`),
  // 升版：生成可编辑的新草稿，旧版继续生效
  revisionRoute: (id) => client.post(`/api/routes/${id}/revision`),
  // 撤回发布：已发布 → 草稿，可就地改
  reopenRoute: (id) => client.post(`/api/routes/${id}/reopen`),
  // 撤销归档：已归档 → 草稿 / 已发布
  restoreRoute: (id, to = 'draft') => client.post(`/api/routes/${id}/restore`, { to }),
  copyRoute: (id) => client.post(`/api/routes/${id}/copy`),
  deleteRoute: (id) => client.delete(`/api/routes/${id}`),
  // 本单副本清单：副本不参与基线清单（kind=task），必须有独立入口
  routeCopies: (params) => client.get('/api/routes/copies', { params }),
  // 批量发布「从未发布过」的草稿基线（dry_run 先预演）
  publishDrafts: (data) => client.post('/api/routes/publish-drafts', data),

  // 任务层：一个订单 = 一个任务，任务下挂多条工艺路线（默认引用基线，可 fork 副本）
  tasks: (params) => client.get('/api/tasks', { params }),
  // 业务待办：规则产生的缺口汇总（未发布被引用、无路线任务、可合并副本…）
  taskTodos: () => client.get('/api/tasks/todos'),
  task: (id) => client.get(`/api/tasks/${id}`),
  createTask: (data) => client.post('/api/tasks', data),
  // 改基本信息：改口径会升版并留修订记录，返回 { task, version, bumped, changes, message }
  patchTask: (id, data) => client.patch(`/api/tasks/${id}`, data),
  taskRevisions: (id) => client.get(`/api/tasks/${id}/revisions`),
  deleteTask: (id) => client.delete(`/api/tasks/${id}`),
  rebuildTask: (id) => client.post(`/api/tasks/${id}/rebuild`),
  taskCandidates: (id, params) => client.get(`/api/tasks/${id}/candidates`, { params }),
  addTaskRoutes: (id, data) => client.post(`/api/tasks/${id}/routes`, data),
  removeTaskRoute: (id, trId) => client.delete(`/api/tasks/${id}/routes/${trId}`),
  forkTaskRoute: (id, trId, user = 'operator') =>
    client.post(`/api/tasks/${id}/routes/${trId}/fork`, null, { params: { user } }),
  // 抽屉内就地改工序：副本直接生效；基线升版并同步所有引用
  editTaskRouteSteps: (id, trId, data) =>
    client.put(`/api/tasks/${id}/routes/${trId}/steps`, data),
  // 副本进阶为基线：内容一致时自动合并回引用，不一致时升版发布
  promoteTaskRoute: (id, trId, user = 'operator') =>
    client.post(`/api/tasks/${id}/routes/${trId}/promote`, null, { params: { user } }),
  // 升版提醒：确认采用新版 / 本单保留现版（转副本）
  ackTaskVersion: (id, data) => client.post(`/api/tasks/${id}/ack`, data),
  taskNotices: (id) => client.get(`/api/tasks/${id}/notices`),
  taskDiff: (id) => client.get(`/api/tasks/${id}/diff`),
  rebuildTasks: () => client.post('/api/tasks/rebuild')
}
