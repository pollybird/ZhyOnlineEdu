<script setup lang="ts">
import type { UiState } from '../utils/format'

/**
 * 五态视图（M2.14 验收：5 态齐全）。
 * loading / empty / error / forbidden / preview；ready 时渲染默认插槽。
 *
 * `code` 只用于兜底排查（控制台/埋点），**不展示**给学员，避免暴露接口错误码。
 */
const props = withDefaults(
  defineProps<{
    state: UiState | 'ready'
    title?: string
    message?: string
    code?: string
    actionText?: string
  }>(),
  { title: '', message: '', code: '', actionText: '' },
)

const emit = defineEmits<{ action: [] }>()

const iconOf = (state: UiState): 'success' | 'warning' | 'info' | 'error' => {
  if (state === 'forbidden') return 'warning'
  if (state === 'preview') return 'info'
  if (state === 'empty') return 'info'
  return 'error'
}
</script>

<template>
  <div v-if="props.state === 'loading'" class="state-view">
    <el-skeleton :rows="4" animated />
  </div>
  <div v-else-if="props.state === 'empty'" class="state-view">
    <el-empty :description="props.title || '暂无内容'">
      <p v-if="props.message" class="state-message">{{ props.message }}</p>
      <el-button v-if="props.actionText" type="primary" @click="emit('action')">
        {{ props.actionText }}
      </el-button>
    </el-empty>
  </div>
  <div v-else-if="props.state === 'ready'">
    <slot />
  </div>
  <div v-else class="state-view">
    <el-result :icon="iconOf(props.state)" :title="props.title" :sub-title="props.message">
      <template #extra>
        <div class="state-extra">
          <el-button v-if="props.actionText" type="primary" @click="emit('action')">
            {{ props.actionText }}
          </el-button>
        </div>
      </template>
    </el-result>
  </div>
</template>
