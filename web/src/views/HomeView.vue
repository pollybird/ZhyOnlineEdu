<script setup lang="ts">
import { onMounted } from 'vue'
import { useAuthStore } from '../stores/auth'

// M1.7 框架页：登录后可看到当前身份、角色、权限与场景模式（业务模块 M2 接入）
const auth = useAuthStore()

onMounted(async () => {
  try {
    await auth.bootstrap()
  } catch {
    // 401 由请求拦截器统一处理（刷新或回登录页）
  }
})
</script>

<template>
  <div class="home">
    <el-card class="home-card">
      <template #header>
        <div class="home-head">
          <span>框架页</span>
          <el-tag size="small" type="success">M1.7 已就绪</el-tag>
        </div>
      </template>

      <el-descriptions :column="2" border>
        <el-descriptions-item label="账号">{{ auth.user?.username }}</el-descriptions-item>
        <el-descriptions-item label="显示名">{{ auth.user?.display_name || '—' }}</el-descriptions-item>
        <el-descriptions-item label="场景模式">{{ auth.mode || '未设置' }}</el-descriptions-item>
        <el-descriptions-item label="令牌版本">tv{{ auth.user?.token_version }}</el-descriptions-item>
        <el-descriptions-item label="服务器时间" :span="2">{{ auth.serverTime || '—' }}</el-descriptions-item>
      </el-descriptions>

      <h3 class="home-title">角色</h3>
      <div class="home-tags">
        <el-tag v-for="role in auth.user?.roles ?? []" :key="role" type="success">{{ role }}</el-tag>
        <span v-if="(auth.user?.roles ?? []).length === 0" class="home-empty">无</span>
      </div>

      <h3 class="home-title">权限（RBAC）</h3>
      <div class="home-tags">
        <el-tag v-for="code in auth.permissions" :key="code" size="small">{{ code }}</el-tag>
      </div>
      <p class="home-hint">
        权限来自 `sys_role_permission`；`progress:direct` 不授予内置角色（计划书 §7.3）。
      </p>
    </el-card>
  </div>
</template>
