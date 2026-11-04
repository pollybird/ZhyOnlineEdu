<script setup lang="ts">
import { computed, onMounted, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { usePluginStore, visibleMenus } from '../stores/plugin'
import { modeNameLabel, roleLabel } from '../utils/labels'

/**
 * 应用外壳（M1.7 / M2.14 / M3.5）：学员端与管理端共用导航；
 * 管理入口按 RBAC 权限显隐，插件菜单由 `/api/v1/plugins` 下发（未启用不下发），
 * 移动端导航可横向滚动。
 */
const auth = useAuthStore()
const plugins = usePluginStore()
const router = useRouter()

const pluginMenus = computed(() => visibleMenus(plugins.menus, (code) => auth.can(code)))

onMounted(() => {
  if (auth.loggedIn) {
    void plugins.ensureLoaded()
  }
})

watch(
  () => auth.loggedIn,
  (loggedIn) => {
    if (loggedIn) {
      void plugins.ensureLoaded()
    } else {
      plugins.clear()
    }
  },
)

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
        <router-link v-if="auth.can('system:manage')" :to="{ name: 'admin-plugins' }">
          插件管理
        </router-link>
        <router-link
          v-for="item in pluginMenus"
          :key="`${item.code}:${item.title}`"
          :to="item.path"
        >
          {{ item.title }}
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
