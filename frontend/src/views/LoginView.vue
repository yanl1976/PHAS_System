<template>
  <div class="login-wrap">
    <div class="login-card">
      <div class="brand">
        <div class="brand-mark">工</div>
        <div>
          <div class="brand-title">工时识别核算</div>
          <div class="brand-sub">2车间 · 加工路线单</div>
        </div>
      </div>

      <el-form ref="formRef" :model="form" label-position="top" @submit.prevent="submit">
        <el-form-item label="账号" :error="err">
          <el-input v-model="form.username" size="large" autocomplete="username" placeholder="登录账号" @keyup.enter="submit" />
        </el-form-item>
        <el-form-item label="密码">
          <el-input
            v-model="form.password"
            size="large"
            type="password"
            show-password
            autocomplete="current-password"
            placeholder="密码"
            @keyup.enter="submit"
          />
        </el-form-item>
        <el-button type="primary" size="large" style="width: 100%" :loading="loading" @click="submit">登录</el-button>
      </el-form>

      <div class="login-note">
        <template v-if="status?.need_setup">
          系统还没有账号。首次部署默认管理员：<b>admin / admin123</b>，登录后请立即修改密码。
        </template>
        <template v-else>
          谁登录，操作留痕就记谁。审计里的「操作人」不再由页面传值决定。
        </template>
      </div>
      <div v-if="status && status.auth_required === false" class="login-warn">
        当前后端关闭了强制登录（WT_AUTH_REQUIRED=0），任何人都能直接操作，仅限临时排查使用。
      </div>
    </div>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { api, setSession } from '../api'

const router = useRouter()
const route = useRoute()
const form = reactive({ username: '', password: '' })
const loading = ref(false)
const err = ref('')
const status = ref(null)

onMounted(async () => {
  try {
    status.value = await api.authStatus()
  } catch (e) {
    status.value = null
  }
})

async function submit() {
  if (!form.username.trim() || !form.password) {
    err.value = '请输入账号和密码'
    return
  }
  loading.value = true
  err.value = ''
  try {
    const r = await api.login(form.username.trim(), form.password)
    setSession(r.token, r.user)
    ElMessage.success(`欢迎，${r.user?.display_name || r.user?.username || form.username}`)
    router.replace(route.query.redirect || '/')
  } catch (e) {
    err.value = e.detailText || '登录失败'
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-wrap {
  height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: radial-gradient(circle at 30% 20%, #1e293b, #0f1319 70%);
}
.login-card {
  width: 380px;
  background: #fff;
  border-radius: 14px;
  padding: 26px 26px 20px;
  box-shadow: 0 20px 50px rgba(0, 0, 0, 0.35);
}
.brand { display: flex; align-items: center; gap: 10px; margin-bottom: 18px; }
.brand-mark {
  width: 38px; height: 38px; border-radius: 10px;
  background: linear-gradient(135deg, #3b82f6, #2563eb);
  color: #fff; display: flex; align-items: center; justify-content: center;
  font-weight: 700; font-size: 17px;
}
.brand-title { font-size: 15px; font-weight: 600; color: #1f2430; }
.brand-sub { font-size: 11.5px; color: #7b8798; margin-top: 2px; }
.login-note { margin-top: 14px; font-size: 12px; color: #7b8798; line-height: 1.6; }
.login-warn {
  margin-top: 10px; padding: 8px 10px; border-radius: 8px;
  background: #fff4e5; color: #a15c00; font-size: 12px;
}
</style>
