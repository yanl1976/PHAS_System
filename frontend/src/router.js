import { createRouter, createWebHashHistory } from 'vue-router'
import { getToken } from './api'

const routes = [
  { path: '/login', name: 'login', component: () => import('./views/LoginView.vue'), meta: { title: '登录', public: true } },
  { path: '/', redirect: '/import' },
  { path: '/import', name: 'import', component: () => import('./views/ImportView.vue'), meta: { title: '导入与识别' } },
  { path: '/review/:id?', name: 'review', component: () => import('./views/ReviewView.vue'), meta: { title: '核对工作台' } },
  { path: '/sheets', name: 'sheets', component: () => import('./views/SheetsView.vue'), meta: { title: '报工台账' } },
  { path: '/report', name: 'report', component: () => import('./views/ReportView.vue'), meta: { title: '扫码报工' } },
  { path: '/tasks', name: 'tasks', component: () => import('./views/TasksView.vue'), meta: { title: '生产任务' } },
  { path: '/summary', name: 'summary', component: () => import('./views/SummaryView.vue'), meta: { title: '月度汇总' } },
  { path: '/wages', name: 'wages', component: () => import('./views/WagesView.vue'), meta: { title: '工资核算' } },
  { path: '/export', name: 'export', component: () => import('./views/ExportView.vue'), meta: { title: '导出与勾稽' } },
  { path: '/routes', name: 'routes', component: () => import('./views/RouteView.vue'), meta: { title: '工艺及工时编排' } },
  { path: '/combos', name: 'combos', component: () => import('./views/CombosView.vue'), meta: { title: '工时组合' } },
  { path: '/cards', name: 'cards', component: () => import('./views/ProcessCardView.vue'), meta: { title: '工艺卡' } },
  { path: '/admin', name: 'admin', component: () => import('./views/AdminView.vue'), meta: { title: '系统管理' } }
]

const router = createRouter({ history: createWebHashHistory(), routes })

// 没登录就直接进业务页，会在第一个写操作时被后端 401 拦住 ——
// 那时人已经填了半页数据。进来之前先要身份。
router.beforeEach((to) => {
  if (to.meta?.public) return true
  if (!getToken()) return { path: '/login', query: to.fullPath !== '/' ? { redirect: to.fullPath } : {} }
  return true
})

export default router
