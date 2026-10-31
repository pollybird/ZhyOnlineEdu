<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import AdminMobileGate from '../components/AdminMobileGate.vue'
import ShowcaseTips from '../components/ShowcaseTips.vue'
import { useAuthStore } from '../stores/auth'
import { modeNameLabel, roleLabel } from '../utils/labels'

/**
 * 管理端首页（M2.14）。
 * - 窄屏（<640px）统一走 `AdminMobileGate`：只展示提示卡片 + 复制桌面端链接，不重定向；
 * - 入口按权限显隐：用户管理 `user:manage`、题库/媒体 `course:edit`、会员 `member:manage`。
 */
const auth = useAuthStore()
const router = useRouter()

const entries = computed(() =>
  [
    {
      name: 'admin-users',
      title: '用户与班级',
      desc: '用户查询/新建/停用/到期、班级成员、导入批次',
      permission: 'user:manage',
    },
    {
      name: 'catalog',
      title: '学科与课程',
      desc: '学科与课程维护、上架检查',
      permission: 'course:edit',
    },
    {
      name: 'admin-quiz',
      title: '题库管理',
      desc: '按课时维护题目、标准答案与解析',
      permission: 'course:edit',
    },
    {
      name: 'admin-media',
      title: '媒体文件',
      desc: '视频上传、转码状态与重新转码',
      permission: 'course:edit',
    },
    {
      name: 'admin-member',
      title: '会员管理',
      desc: '档位维护、赠送开通、订阅与订单查询',
      permission: 'member:manage',
    },
    {
      name: 'mode-switch',
      title: '双模式切换',
      desc: '预检后切换内部 / 对外模式',
      permission: 'system:manage',
    },
  ].filter((item) => auth.can(item.permission)),
)
</script>

<template>
  <AdminMobileGate>
    <div class="admin-home">
      <ShowcaseTips
        title="管理端"
        description="管理入口按账号权限显示；移动端请复制桌面端链接后在电脑上操作。"
        :points="[
          '用户管理：新建、停用、到期与导入批次均可追溯',
          '题库：标准答案仅管理端可见，学员端不下发',
          '媒体：上传 → 检测 → 转码 → 可播放后才能上架',
        ]"
      />

      <el-card class="admin-card">
        <template #header>
          <div class="admin-head">
            <span>当前账号</span>
            <el-tag size="small" type="success">{{ auth.user?.username }}</el-tag>
          </div>
        </template>
        <el-descriptions :column="2" border>
          <el-descriptions-item label="显示名">
            {{ auth.user?.display_name || '—' }}
          </el-descriptions-item>
          <el-descriptions-item label="场景模式">
            {{ auth.mode ? modeNameLabel(auth.mode) : '未设置' }}
          </el-descriptions-item>
          <el-descriptions-item label="角色" :span="2">
            <el-tag v-for="role in auth.user?.roles ?? []" :key="role" size="small" type="success">
              {{ roleLabel(role) }}
            </el-tag>
          </el-descriptions-item>
        </el-descriptions>
      </el-card>

      <el-row :gutter="16">
        <el-col v-for="entry in entries" :key="entry.name" :xs="24" :sm="12" :lg="8">
          <el-card class="admin-entry" shadow="hover" @click="router.push({ name: entry.name })">
            <h3 class="admin-entry-title">{{ entry.title }}</h3>
            <p class="admin-entry-desc">{{ entry.desc }}</p>
            <el-button link type="primary">进入 →</el-button>
          </el-card>
        </el-col>
      </el-row>
      <el-empty v-if="entries.length === 0" description="当前账号没有管理端权限" />
    </div>
  </AdminMobileGate>
</template>
