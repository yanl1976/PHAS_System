<template>
  <!-- 登录页不吃侧边栏与核算期切换：还没身份，切了也没意义 -->
  <router-view v-if="isLogin" />
  <div v-else class="app-shell">
    <!-- 顶部通栏：横贯整个窗口，与侧栏同一块深色。
         为什么品牌从侧栏搬上来：通栏左边 218px 正好和侧栏同宽，两块深色在
         左上角连成一个 L 形，视线不用在两处找「我在哪个系统里」。 -->
    <header class="topnav">
      <div class="topnav-brand">
        <div class="brand-mark">工</div>
        <div class="brand-text">
          <div class="brand-title">{{ appShort }}</div>
          <div class="brand-sub">加工路线单 · 工时核算</div>
        </div>
      </div>

      <div class="topnav-main">
        <!-- 系统信息：名字与版本常看，主机/运行时长/登录方式放 title 里，
             通栏一行放不下那么多字，但需要的时候鼠标一悬停就能看到 -->
        <div class="tn-info tn-sys" :title="sysTitle">
          <span class="tn-k">系统</span>
          <span class="tn-v">{{ appName }} <i class="tn-ver">v{{ version }}</i></span>
        </div>

        <!-- 人员信息 -->
        <div class="tn-info" :title="personTitle">
          <span class="tn-k">人员</span>
          <span class="tn-v">{{ personText }}</span>
          <el-tooltip
            v-if="user?.role === 'viewer'"
            content="系统角色：只读（能查看，修改会被服务端拒绝）"
            placement="bottom"
          >
            <span class="ro-tag">只读</span>
          </el-tooltip>
        </div>

        <!-- 组织信息：车间 + 岗位。账号没挂名册人员时如实说「未挂名册」，
             而不是拿系统角色冒充岗位 —— 那是两回事。 -->
        <div class="tn-info tn-org" :title="orgTitle">
          <span class="tn-k">组织</span>
          <span class="tn-v">{{ orgText }}</span>
        </div>

        <div class="spacer"></div>

        <span class="tn-k">核算期</span>
        <el-select
          v-model="ymModel"
          size="small"
          class="nav-select"
          style="width: 132px"
          @change="onPeriodChange"
        >
          <el-option v-for="p in store.periods" :key="p.ym" :value="p.ym" :label="p.title || p.ym">
            <span>{{ p.title || p.ym }}</span>
            <el-tag v-if="p.status === 'locked'" size="small" type="info" style="margin-left: 8px">已锁定</el-tag>
          </el-option>
        </el-select>
        <el-tag v-if="periodLocked" size="small" type="info" effect="dark">只读</el-tag>
        <el-tooltip content="新建核算期" placement="bottom">
          <button class="nav-btn" @click="createPeriodDialog = true">
            <el-icon><Plus /></el-icon>
          </button>
        </el-tooltip>

        <span class="nav-sep"></span>

        <el-dropdown trigger="click" @command="onUserCommand">
          <span class="user-chip">
            <el-icon><UserFilled /></el-icon>
            {{ user?.display_name || user?.username || '未登录' }}
            <el-icon class="chip-caret"><ArrowDown /></el-icon>
          </span>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item v-if="user?.role_name" disabled>
                系统角色：{{ user.role_name }}
              </el-dropdown-item>
              <el-dropdown-item v-if="user?.positions?.length" disabled>
                岗位：{{ user.positions.join('、') }}
              </el-dropdown-item>
              <el-dropdown-item command="password" divided>修改密码</el-dropdown-item>
              <el-dropdown-item command="logout">退出登录</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </header>

    <div class="app-body">
      <aside class="sidebar">
        <nav class="nav">
          <template v-for="m in menus" :key="m.path">
            <div v-if="m.path === '/admin'" class="nav-split"></div>
            <router-link :to="m.path" class="nav-item" active-class="active">
              <el-icon><component :is="m.icon" /></el-icon>
              <span>{{ m.label }}</span>
              <el-badge v-if="m.path === '/review' && pendingCount" :value="pendingCount" class="nav-badge" />
              <!-- 识别积压：纸交上来了却没被识别，人会以为「系统没收到」，其实是在排队 -->
              <el-badge
                v-else-if="m.path === '/import' && queuePending"
                :value="queuePending"
                type="warning"
                class="nav-badge"
              />
            </router-link>
          </template>
        </nav>

        <div class="sidebar-foot">
          <div class="engine" :class="engineClass">
            <span class="dot"></span>
            <span>{{ engineText }}</span>
          </div>
          <div class="foot-note">合计 = 合格件数 × 单件工时</div>
        </div>
      </aside>

      <div class="main">
        <main class="content">
          <router-view v-slot="{ Component }">
            <component :is="Component" :key="store.ym" />
          </router-view>
        </main>
      </div>
    </div>

    <!-- 底部状态栏：全宽一条。原来这些数字挤在白色工具条里，占掉一行高度还
         和「核算期」混在一起；挪下来之后上方只剩业务内容。 -->
    <footer class="statusbar">
      <div class="sb-group">
        <span class="sb-dot" :class="dbOk ? 'ok' : 'err'"></span>
        <span class="sb-k">数据库</span>
        <b :class="dbOk ? '' : 'sb-bad'">{{ dbOk ? '正常' : '不可读' }}</b>
      </div>
      <span class="sb-sep"></span>
      <div class="sb-group" :title="queueTitle">
        <span class="sb-k">识别队列</span>
        <b class="mono">{{ queuePending }}</b>
        <span v-if="queueBusy" class="sb-k">处理中 {{ queueBusy }}</span>
      </div>
      <span class="sb-sep"></span>
      <div class="sb-group">
        <span class="sb-k">待核对</span>
        <b class="mono">{{ store.stats?.counts?.recognized ?? 0 }}</b>
      </div>
      <span class="sb-sep"></span>
      <div class="sb-group">
        <span class="sb-k">已确认</span>
        <b class="mono">{{ store.stats?.counts?.verified ?? 0 }}</b>
      </div>
      <template v-if="store.stats?.counts?.scan_items">
        <span class="sb-sep sb-opt"></span>
        <div class="sb-group sb-opt">
          <span class="sb-k">扫码报工</span>
          <b class="mono">{{ store.stats.counts.scan_items }}</b>
          <span class="sb-k">行</span>
        </div>
      </template>
      <span class="sb-sep"></span>
      <div class="sb-group">
        <span class="sb-k">已确认工时</span>
        <b class="mono">{{ (store.stats?.hours?.verified ?? 0).toFixed(1) }}</b>
        <span class="sb-k">h</span>
      </div>

      <div class="spacer"></div>

      <span class="sb-k sb-opt" :title="sysTitle">{{ appHost }}</span>
      <span class="sb-sep sb-opt"></span>
      <span class="sb-clock mono" title="本机时间">{{ clock }}</span>
      <span class="sb-sep"></span>
      <span class="sb-k">v{{ version }}</span>
    </footer>

    <el-dialog v-model="createPeriodDialog" title="新建核算期" width="380px">
      <el-form label-width="80px">
        <el-form-item label="月份">
          <el-date-picker v-model="newPeriod" type="month" value-format="YYYY-MM" placeholder="选择月份" style="width: 100%" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createPeriodDialog = false">取消</el-button>
        <el-button type="primary" @click="doCreatePeriod">创建</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="pwdVisible" title="修改密码" width="380px">
      <el-form label-width="76px">
        <el-form-item label="原密码"><el-input v-model="pwd.old" type="password" show-password /></el-form-item>
        <el-form-item label="新密码"><el-input v-model="pwd.next" type="password" show-password placeholder="至少 6 位" /></el-form-item>
        <el-form-item label="再输一次"><el-input v-model="pwd.repeat" type="password" show-password /></el-form-item>
      </el-form>
      <div class="muted" style="font-size: 12px">改完密码，其它设备上的登录态会立即失效。</div>
      <template #footer>
        <el-button @click="pwdVisible = false">取消</el-button>
        <el-button type="primary" @click="doChangePassword">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { api, clearSession, currentUser, setSession } from './api'
import { bootstrap, loadPeriods, periodLocked, refreshHealth, refreshStats, setPeriod, store } from './store'

const route = useRoute()
const router = useRouter()
const createPeriodDialog = ref(false)
const newPeriod = ref('')
// 健康状态轮询句柄（组件卸载时要清掉，否则热更新会越攒越多）
let healthTimer = 0

const isLogin = computed(() => route.path === '/login')
const user = ref(currentUser())

const pwdVisible = ref(false)
const pwd = ref({ old: '', next: '', repeat: '' })

function onUserCommand(cmd) {
  if (cmd === 'logout') {
    clearSession()
    router.replace('/login')
    return
  }
  if (cmd === 'password') {
    pwd.value = { old: '', next: '', repeat: '' }
    pwdVisible.value = true
  }
}

async function doChangePassword() {
  if (!pwd.value.old || !pwd.value.next) return ElMessage.warning('请填写原密码与新密码')
  if (pwd.value.next.length < 6) return ElMessage.warning('新密码至少 6 位')
  if (pwd.value.next !== pwd.value.repeat) return ElMessage.warning('两次输入的新密码不一致')
  const r = await api.changePassword(pwd.value.old, pwd.value.next)
  // 改密会作废旧令牌，服务端已签了一枚新的，本地要同步换掉，否则下一个请求就 401
  setSession(r.token, currentUser())
  pwdVisible.value = false
  ElMessage.success('密码已修改')
}

const menus = [
  { path: '/import', label: '导入与识别', icon: 'UploadFilled' },
  { path: '/review', label: '核对工作台', icon: 'EditPen' },
  { path: '/sheets', label: '报工台账', icon: 'Files' },
  { path: '/report', label: '扫码报工', icon: 'Camera' },
  { path: '/tasks', label: '生产任务', icon: 'Tickets' },
  { path: '/summary', label: '月度汇总', icon: 'DataAnalysis' },
  { path: '/wages', label: '工资核算', icon: 'Money' },
  { path: '/export', label: '导出与勾稽', icon: 'Download' },
  { path: '/routes', label: '工艺及工时编排', icon: 'SetUp' },
  { path: '/combos', label: '工时组合', icon: 'Grid' },
  { path: '/cards', label: '工艺卡', icon: 'Document' },
  { path: '/admin', label: '系统管理', icon: 'Setting' }
]

const pendingCount = computed(() => store.stats?.counts?.recognized ?? 0)

// ---------------------------------------------------------------- 顶栏信息
// 系统信息来自 /api/health 的 app 段（跟着那盏灯每 30 秒刷同一份数据），
// 不另开接口也不在前端写死版本号：否则页面写 v1.0、后端已经 v1.2，
// 出问题时第一个被怀疑的就是「你看到的是不是旧版本」。
const sysInfo = computed(() => store.health?.app || {})
const appName = computed(() => sysInfo.value.name || '工时识别核算系统')
const appShort = computed(() => sysInfo.value.short || '工时识别核算')
const version = computed(() => sysInfo.value.version || '1.0.0')
const appHost = computed(() => sysInfo.value.host || '')
const workshop = computed(() => store.health?.org?.workshop || '2车间')

function fmtUptime(sec) {
  const s = Math.max(0, Number(sec) || 0)
  const d = Math.floor(s / 86400)
  const h = Math.floor((s % 86400) / 3600)
  const m = Math.floor((s % 3600) / 60)
  if (d) return `${d} 天 ${h} 小时`
  if (h) return `${h} 小时 ${m} 分`
  return `${m} 分钟`
}

const sysTitle = computed(() => {
  const a = sysInfo.value
  const bits = [`${appName.value} v${a.version || '?'}`]
  if (a.host) bits.push(`主机 ${a.host}`)
  if (a.python) bits.push(`Python ${a.python}`)
  if (a.uptime_seconds != null) bits.push(`本次运行 ${fmtUptime(a.uptime_seconds)}`)
  bits.push(a.auth_mode === 'local' ? '本机免登录' : '账号登录')
  return bits.join(' · ')
})

// 人员 = 名册里的真人 + 系统角色。没挂名册时只显示账号名，
// 不去猜「他大概是谁」—— 猜错了比不显示更麻烦。
const personText = computed(() => {
  const u = user.value
  if (!u) return '未登录'
  const who = u.worker_name || u.display_name || u.username
  // 账号名和角色名撞车时只说一次：默认管理员账号叫「管理员」、角色也叫「管理员」，
  // 拼成「管理员 · 管理员」像界面出了 bug。
  if (!u.role_name || who === u.role_name) return who
  return `${who} · ${u.role_name}`
})

const personTitle = computed(() => {
  const u = user.value
  if (!u) return '未登录'
  const bits = [`账号 ${u.username}`]
  if (u.worker_name) bits.push(`名册人员 ${u.worker_name}${u.worker_no ? `（工号 ${u.worker_no}）` : ''}`)
  if (u.role_name) bits.push(`系统角色 ${u.role_name}`)
  if (u.duty_names?.length) bits.push(`有效职能 ${u.duty_names.join('、')}`)
  return bits.join(' · ')
})

const orgText = computed(() => {
  const u = user.value
  if (!u) return '—'
  if (!u.worker_id && !u.worker_name) return '系统账号'
  const team = u.worker_team || workshop.value
  const posts = (u.positions || []).filter(Boolean)
  if (!posts.length) return `${team} · 未设岗位`
  // 一人多岗是常态，超过两个就省略，完整内容在 title 里
  const shown = posts.length > 2 ? `${posts.slice(0, 2).join('、')} 等 ${posts.length} 岗` : posts.join('、')
  return `${team} · ${shown}`
})

const orgTitle = computed(() => {
  const u = user.value
  const posts = (u?.positions || []).filter(Boolean)
  if (!posts.length && !u?.worker_id) return '账号还没关联名册人员：到「系统管理 → 账号」里挂上人员，才能确定所属车间与岗位'
  const bits = [`车间/班组 ${u?.worker_team || workshop.value}`]
  if (posts.length) bits.push(`岗位 ${posts.join('、')}`)
  if (u?.worker_process) bits.push(`工种 ${u.worker_process}`)
  return bits.join(' · ')
})

const dbOk = computed(() => store.health?.db?.ok !== false)
const queueBusy = computed(() => Number(store.health?.queue?.processing || 0))
const queueTitle = computed(() => {
  const q = store.health?.queue || {}
  return `待识别 ${q.pending ?? 0} · 处理中 ${q.processing ?? 0} · 已完成 ${q.done ?? 0} · 失败 ${q.failed ?? 0}`
})

// 状态栏时钟：只做「现在几点」，用本机时间，不跟服务端对时（差几秒不影响任何判断）
const clock = ref('')
let clockTimer = 0
function tickClock() {
  const d = new Date()
  const p = (n) => String(n).padStart(2, '0')
  clock.value = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}


const ymModel = computed({
  get: () => store.ym,
  set: (v) => setPeriod(v)
})

// 识别引擎现在跑在**独立进程**里（C1）。所以「能不能识别」不能只看 ollama 式的
// `ocr.loaded`：Web 进程本身永远不加载 Paddle，那个字段只代表 worker 的引擎。
// 真正要回答的是三件事：worker 在不在、引擎热没热、队列堵没堵。
const workerAlive = computed(() => store.health?.worker?.alive === true)
const queuePending = computed(() => Number(store.health?.queue?.pending || 0))
const engineClass = computed(() => {
  if (store.health?.db?.ok === false) return 'err'
  if (store.ocr?.mode === 'stub') return 'warn'
  if (!workerAlive.value) return 'err'
  return store.ocr?.loaded ? 'ok' : 'warn'
})

const engineText = computed(() => {
  if (store.health?.db?.ok === false) return '数据库不可读'
  if (store.ocr?.mode === 'stub') return '模拟识别模式'
  if (!workerAlive.value) return queuePending.value ? `识别进程未启动（积压 ${queuePending.value}）` : '识别进程未启动'
  if (!store.ocr?.loaded) return '识别引擎启动中'
  const wait = queuePending.value ? ` · 待识别 ${queuePending.value}` : ''
  return `识别引擎就绪 v${store.ocr.version}${wait}`
})

function onPeriodChange() {
  refreshStats()
}

async function doCreatePeriod() {
  if (!newPeriod.value) return ElMessage.warning('请选择月份')
  await api.createPeriod({ ym: newPeriod.value, title: `${newPeriod.value.slice(0, 4)}年${Number(newPeriod.value.slice(5, 7))}月` })
  ElMessage.success('核算期已创建')
  createPeriodDialog.value = false
  await loadPeriods()
  setPeriod(newPeriod.value)
}

/** 用服务端的最新账号信息刷新顶栏。
 *
 * 只信 localStorage 的话，管理员刚把你的系统角色改成只读，界面上还显示旧的，
 * 你会一直点一直报错却不知道为什么。`/me` 是权威来源，进业务页就同步一次。
 */
async function refreshUser() {
  try {
    const r = await api.me()
    if (r?.user) {
      user.value = r.user
      setSession('', r.user) // 只更新用户缓存，令牌不动
    }
  } catch {
    // 拿不到就沿用本地缓存，不要让顶栏变成空白
  }
}

onMounted(async () => {
  // 时钟跟着组件挂载起跑：登录页也用同一个 App 实例，停在登录页时它会空转，
  // 但成本是每秒改一个字符串，比「登录后再判断要不要起」那套分支便宜。
  tickClock()
  clockTimer = window.setInterval(tickClock, 1000)
  if (isLogin.value) return
  refreshUser()
  await bootstrap()
  // 30 秒刷一次健康快照：OCR 进程半路挂掉、队列开始积压，都要能从这盏灯上看出来。
  // 轮询很轻（四个 count + 读一个心跳文件），比「等用户发现识别不动了再问」便宜得多。
  healthTimer = window.setInterval(() => {
    if (!document.hidden) refreshHealth()
  }, 30000)
})

onUnmounted(() => {
  if (healthTimer) window.clearInterval(healthTimer)
  if (clockTimer) window.clearInterval(clockTimer)
})

// 登录成功后 App 不会重新挂载（它在登录页也照常存在），所以顶栏要跟着路由变化再读一次，
// 否则右侧还写着「未登录」、只读账号也没有标记。
watch(
  () => route.path,
  async (p) => {
    if (p === '/login') {
      user.value = currentUser()
      return
    }
    user.value = currentUser()
    await refreshUser()
  }
)
</script>

<style scoped>
/* 外壳是三段式：通栏 / 主体（侧栏+内容）/ 状态栏。
   高度用 100vh 定死、只让 .content 滚，通栏与状态栏就不跟着内容跑。
   配色只有一套深色（#141a24），通栏、侧栏、状态栏共用，避免三种深灰互相打架。 */
.app-shell { display: flex; flex-direction: column; height: 100vh; overflow: hidden; }

/* ---------------------------------------------------------------- 顶部通栏 */
.topnav {
  height: 54px; flex: 0 0 54px;
  background: #141a24; color: #c9d1de;
  display: flex; align-items: stretch;
  border-bottom: 1px solid #232c3a;
}

/* 左边 218px 与侧栏同宽：两块深色在左上角接成 L 形，
   侧栏的竖线与通栏的横线在同一点拐弯。 */
.topnav-brand {
  width: 218px; flex: 0 0 218px;
  display: flex; align-items: center; gap: 10px;
  padding: 0 12px;
  border-right: 1px solid #232c3a;
}

.brand-mark {
  width: 30px; height: 30px; border-radius: 8px;
  background: linear-gradient(135deg, #3b82f6, #2563eb);
  color: #fff; display: flex; align-items: center; justify-content: center;
  font-weight: 700; font-size: 15px; flex: 0 0 30px;
}

.brand-text { min-width: 0; }
.brand-title { color: #f2f5fa; font-size: 13.5px; font-weight: 600; white-space: nowrap; }
.brand-sub { color: #6f7c90; font-size: 11px; margin-top: 1px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

.topnav-main {
  flex: 1; min-width: 0;
  display: flex; align-items: center; gap: 14px;
  padding: 0 14px;
}

/* 三组信息：标签小、值亮。组与组之间用竖线分隔，不用 gap 硬撑出距离感 */
.tn-info {
  display: flex; align-items: baseline; gap: 6px;
  min-width: 0; max-width: 260px;
  padding-left: 14px;
  border-left: 1px solid #232c3a;
}
.tn-info:first-child { padding-left: 0; border-left: none; }
.tn-k { font-size: 11px; color: #6f7c90; letter-spacing: .5px; white-space: nowrap; }
.tn-v {
  font-size: 12.5px; color: #cfd8e5;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.tn-ver { font-style: normal; color: #7c8ba1; font-size: 11.5px; }

.nav-select { flex: 0 0 auto; }

/* el-select 默认是白底控件，放在深色通栏里像贴了一块纸。
   Element Plus 2.x 用 .el-select__wrapper（旧版是 .el-input__wrapper），两个都写。 */
.topnav :deep(.el-select__wrapper),
.topnav :deep(.el-input__wrapper) {
  background: #1c2432;
  box-shadow: 0 0 0 1px #2a3442 inset;
  min-height: 28px;
  color: #e6ecf5;
}
.topnav :deep(.el-select__wrapper.is-hovering),
.topnav :deep(.el-input__wrapper:hover) { box-shadow: 0 0 0 1px #3a4a60 inset; }
.topnav :deep(.el-select__wrapper.is-focused),
.topnav :deep(.el-input__wrapper.is-focus) { box-shadow: 0 0 0 1px #2563eb inset; }
.topnav :deep(.el-select__selected-item) { color: #e6ecf5; font-size: 12.5px; }
.topnav :deep(.el-select__placeholder) { color: #8b97a9; }
.topnav :deep(.el-select__suffix),
.topnav :deep(.el-select__caret) { color: #8b97a9; }

/* 图标按钮：不用 el-button —— 深色底上 text 按钮的文字色、hover 底色都要逐个覆盖，
   原生 button 两行 CSS 就说清了 */
.nav-btn {
  width: 26px; height: 26px; flex: 0 0 26px;
  display: inline-flex; align-items: center; justify-content: center;
  border: none; background: none; padding: 0;
  color: #a9b5c6; cursor: pointer; border-radius: 6px;
  font-family: inherit;
}
.nav-btn:hover { color: #fff; background: #26313f; }

.nav-sep { width: 1px; height: 18px; background: #232c3a; flex: 0 0 1px; margin: 0 2px; }

.user-chip {
  display: inline-flex; align-items: center; gap: 5px;
  cursor: pointer; color: #dfe6f0; font-size: 13px;
  padding: 3px 6px; border-radius: 6px;
}
.user-chip:hover { background: #1c2432; color: #fff; }
.chip-caret { font-size: 11px; color: #7c8ba1; }

/* ---------------------------------------------------------------- 主体 */
.app-body { flex: 1; display: flex; min-height: 0; }

.sidebar {
  width: 218px;
  flex: 0 0 218px;
  background: #141a24;
  color: #c9d1de;
  display: flex;
  flex-direction: column;
  padding: 12px;
}

.nav { display: flex; flex-direction: column; gap: 2px; }

.nav-item {
  display: flex; align-items: center; gap: 10px;
  padding: 9px 11px; border-radius: 8px;
  color: #a9b5c6; text-decoration: none; font-size: 13.5px;
  transition: background .15s, color .15s;
  position: relative;
}

.nav-item:hover { background: #1c2432; color: #e6ecf5; }
.nav-item.active { background: #1d4ed8; color: #fff; }
.nav-badge { margin-left: auto; }
.nav-split { height: 1px; background: #232c3a; margin: 10px 8px; }

.sidebar-foot { margin-top: auto; padding: 10px 6px 0; }

.engine { display: flex; align-items: center; gap: 7px; font-size: 12px; color: #8b97a9; }
.engine .dot { width: 7px; height: 7px; border-radius: 50%; background: #6b7787; flex: 0 0 7px; }
.engine.ok .dot { background: #34d399; }
.engine.warn .dot { background: #fbbf24; }
.engine.err .dot { background: #f87171; }
.foot-note { margin-top: 8px; font-size: 11px; color: #5b6675; }

.main { flex: 1; display: flex; flex-direction: column; min-width: 0; }

/* scrollbar-gutter: stable —— 页面级滚动条**常驻槽位**。
   没有这一行时，内容一长（比如展开树）滚动条出现，视口可用宽度少 15px，
   整页文字会横向抖一下；折叠回去滚动条消失，又抖回来。用户看到的就是
   「展开/折叠时页面轻微漂移」。槽位常驻后宽度恒定，代价是右侧固定留 15px。 */
.content { flex: 1; overflow: auto; scrollbar-gutter: stable; }

/* ---------------------------------------------------------------- 底部状态栏 */
/* 26px 一条，字号 11.5px：它要一直在，但不能抢内容的注意力。
   数字用 mono，滚动时不会因为字宽变化左右跳。 */
.statusbar {
  height: 26px; flex: 0 0 26px;
  display: flex; align-items: center; gap: 12px;
  padding: 0 14px;
  background: #141a24; border-top: 1px solid #232c3a;
  color: #7f8b9d; font-size: 11.5px;
  white-space: nowrap; overflow: hidden;
}
.sb-group { display: inline-flex; align-items: baseline; gap: 5px; }
.sb-group b { color: #cfd8e5; font-weight: 600; }
.sb-group b.sb-bad { color: #f87171; }
.sb-k { color: #6f7c90; }
.sb-sep { width: 1px; height: 11px; background: #333f52; flex: 0 0 1px; }
.sb-dot { width: 6px; height: 6px; border-radius: 50%; background: #6b7787; align-self: center; flex: 0 0 6px; }
.sb-dot.ok { background: #34d399; }
.sb-dot.err { background: #f87171; }
.sb-clock { color: #8b97a9; letter-spacing: .3px; }

/* 只读标记：小、但不含糊 —— 灰底白字，跟状态标签一套观感 */
.ro-tag {
  display: inline-block;
  padding: 0 5px;
  height: 17px;
  line-height: 17px;
  border-radius: 3px;
  background: #909399;
  color: #fff;
  font-size: 11px;
  font-weight: 500;
  letter-spacing: 0.3px;
  align-self: center;
}

/* 窄屏：先丢「主机」这类可悬停查看的次要信息，再丢组织组，
   但核算期、人员、状态栏的时间与版本必须留着 —— 那是判断「看的是哪份数据」的依据。 */
@media (max-width: 1280px) {
  .sb-opt { display: none; }
}
@media (max-width: 1120px) {
  .tn-org { display: none; }
  .tn-sys { max-width: 190px; }
}
</style>
