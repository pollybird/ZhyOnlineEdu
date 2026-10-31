<script setup lang="ts">
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { modeNameLabel, roleLabel } from '../utils/labels'

/**
 * 应用外壳（M1.7 / M2.14）：学员端与管理端共用导航；
 * 管理入口按 RBAC 权限显隐，移动端导航可横向滚动。
 */
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
      <el-tag v-if="auth.mode" size="small" type="info">{{ modeNameLabel(auth.mode) }}</el-tag>
      <nav class="shell-nav">
        <router-link :to="{ name: 'home' }">首页</router-link>
        <router-link :to="{ name: 'learn-progress' }">我的学习</router-link>
        <router-link :to="{ name: 'member' }">会员</router-link>
        <router-link v-if="auth.can('course:edit')" :to="{ name: 'admin' }">管理端</router-link>
        <router-link v-if="auth.can('course:edit')" :to="{ name: 'catalog' }">学科与课程</router-link>
        <router-link v-if="auth.can('user:manage')" :to="{ name: 'admin-users' }">用户</router-link>
        <router-link v-if="auth.can('course:edit')" :to="{ name: 'admin-quiz' }">题库</router-link>
        <router-link v-if="auth.can('course:edit')" :to="{ name: 'admin-media' }">媒体</router-link>
        <router-link v-if="auth.can('member:manage')" :to="{ name: 'admin-member' }">会员管理</router-link>
        <router-link v-if="auth.can('system:manage')" :to="{ name: 'mode-switch' }">
          双模式切换
        </router-link>
      </nav>
      <div class="shell-spacer" />
      <span class="shell-user">{{ auth.user?.display_name || auth.user?.username }}</span>
      <el-tag v-for="role in auth.user?.roles ?? []" :key="role" size="small" type="success">
        {{ roleLabel(role) }}
      </el-tag>
      <el-button link type="primary" @click="logout">退出登录</el-button>
    </header>
    <main class="shell-main">
      <router-view />
    </main>
  </div>
</template>
