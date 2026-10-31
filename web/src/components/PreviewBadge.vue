<script setup lang="ts">
/** 超管/教师预览态标记（§9.4 #18）：提示「本播放不计入学习统计」。 */
defineProps<{ text?: string }>()
</script>

<template>
  <el-alert
    class="preview-badge"
    type="warning"
    show-icon
    :closable="false"
    :title="text || '预览模式：本次播放仅用于预览，不计入学习统计'"
  />
</template>
