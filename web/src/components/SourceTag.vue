<script setup lang="ts">
import { freshnessLabel } from '../utils/format'

/**
 * 统计来源标记（§14.5）：对学员只展示「学习统计」与更新时间，
 * `metricVersion` 仅用于排查，不直接显示。
 */
withDefaults(
  defineProps<{
    metricVersion: string
    freshnessSeconds?: number
    generatedAt?: string | null
  }>(),
  { freshnessSeconds: 0, generatedAt: null },
)
</script>

<template>
  <div class="source-tag">
    <el-tooltip content="统计由系统自动汇总，预览与试看不计入" placement="top">
      <el-tag size="small" type="info">学习统计</el-tag>
    </el-tooltip>
    <el-tag v-if="freshnessSeconds !== undefined" size="small" type="success">
      {{ freshnessLabel(freshnessSeconds) }}
    </el-tag>
    <span v-if="generatedAt" class="source-time">{{ generatedAt }}</span>
  </div>
</template>
