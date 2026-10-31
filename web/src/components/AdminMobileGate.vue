<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'

/**
 * 管理端窄屏提示（§14.7 验收）。
 * 规则：`/admin` 页面宽度 < 640px 时只展示提示卡片 + **可点击复制桌面端链接**，
 * 不做任何重定向（避免与路由守卫形成死循环）。
 */
const BREAKPOINT = 640
const compact = ref(false)
const copied = ref(false)

const href = computed(() => (typeof window === 'undefined' ? '' : window.location.href))

function update(): void {
  compact.value = typeof window !== 'undefined' && window.innerWidth < BREAKPOINT
}

async function copyLink(): Promise<void> {
  try {
    await navigator.clipboard.writeText(href.value)
    copied.value = true
    ElMessage.success('桌面端链接已复制')
  } catch {
    ElMessage.warning('复制失败，请手动选择下方链接复制')
  }
}

onMounted(() => {
  update()
  window.addEventListener('resize', update)
})

onUnmounted(() => {
  window.removeEventListener('resize', update)
})
</script>

<template>
  <div v-if="compact" class="admin-mobile-gate">
    <el-card>
      <template #header>管理端请在桌面端使用</template>
      <p>管理端包含表格、上传与批量操作，当前屏幕宽度不足，请在电脑浏览器中使用。</p>
      <el-button type="primary" @click="copyLink">
        {{ copied ? '已复制，请到桌面端打开' : '复制桌面端链接' }}
      </el-button>
      <el-input class="gate-link" :model-value="href" readonly />
      <p class="gate-hint">复制或点击链接后，在电脑浏览器中打开即可。</p>
    </el-card>
  </div>
  <slot v-else />
</template>
