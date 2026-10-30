<script setup lang="ts">
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const router = useRouter()

async function logout(): Promise<void> {
  await auth.logout()
  await router.replace({ name: 'login' })
}
</script>

<template>
  <div class="shell">
    <header class="shell-header">
      <div class="shell-brand">钟毓在线教育系统</div>
      <el-tag v-if="auth.mode" size="small" type="info">{{ auth.mode }}</el-tag>
      <nav class="shell-nav">
        <router-link :to="{ name: 'home' }">框架页</router-link>
        <router-link v-if="auth.can('course:edit')" :to="{ name: 'catalog' }">学科与课程</router-link>
        <router-link v-if="auth.can('system:manage')" :to="{ name: 'mode-switch' }">
          双模式切换
        </router-link>
      </nav>
      <div class="shell-spacer" />
      <span class="shell-user">{{ auth.user?.display_name || auth.user?.username }}</span>
      <el-tag v-for="role in auth.user?.roles ?? []" :key="role" size="small" type="success">
        {{ role }}
      </el-tag>
      <el-button link type="primary" @click="logout">退出登录</el-button>
    </header>
    <main class="shell-main">
      <router-view />
    </main>
  </div>
</template>
