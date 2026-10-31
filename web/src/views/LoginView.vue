<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ApiError } from '../api/request'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

const form = reactive({ username: '', password: '' })
const busy = ref(false)
const errorMessage = ref('')

async function submit(): Promise<void> {
  if (form.username.trim() === '' || form.password === '') {
    errorMessage.value = '请输入用户名与口令'
    return
  }
  busy.value = true
  errorMessage.value = ''
  try {
    await auth.login(form.username.trim(), form.password)
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/'
    await router.replace(redirect)
  } catch (error) {
    errorMessage.value =
      error instanceof ApiError ? error.message : '登录失败，请稍后重试'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="auth-page">
    <el-card class="auth-card">
      <template #header>
        <div class="auth-head">
          <span>钟毓在线教育系统</span>
          <el-tag size="small" type="info">登录</el-tag>
        </div>
      </template>

      <el-alert
        v-if="errorMessage"
        type="error"
        :closable="false"
        show-icon
        :title="errorMessage"
        class="auth-alert"
      />

      <el-form label-position="top" @submit.prevent="submit">
        <el-form-item label="用户名">
          <el-input v-model="form.username" autocomplete="username" @keyup.enter="submit" />
        </el-form-item>
        <el-form-item label="口令">
          <el-input
            v-model="form.password"
            type="password"
            show-password
            autocomplete="current-password"
            @keyup.enter="submit"
          />
        </el-form-item>
      </el-form>

      <el-button type="primary" :loading="busy" class="auth-submit" @click="submit">
        登录
      </el-button>
      <p class="auth-hint">登录状态会自动保持；如提示登录已过期，请重新登录。</p>
    </el-card>
  </div>
</template>
