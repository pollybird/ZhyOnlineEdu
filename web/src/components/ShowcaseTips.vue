<script setup lang="ts">
/**
 * 页面说明条（M2.14）：统一展示本页验收口径与请求预算，避免每个页面重复写文案。
 * 只做展示，不参与业务逻辑。
 */
withDefaults(
  defineProps<{
    title: string
    description?: string
    points?: string[]
  }>(),
  { description: '', points: () => [] },
)
</script>

<template>
  <el-alert class="showcase-tips" type="info" :closable="false" show-icon>
    <template #title>
      <span class="showcase-title">{{ title }}</span>
    </template>
    <p v-if="description" class="showcase-desc">{{ description }}</p>
    <ul v-if="points.length > 0" class="showcase-points">
      <li v-for="point in points" :key="point">{{ point }}</li>
    </ul>
  </el-alert>
</template>
