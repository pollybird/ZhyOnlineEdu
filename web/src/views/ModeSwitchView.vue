<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ApiError } from '../api/request'
import { useAuthStore } from '../stores/auth'
import {
  describeSwitchReport,
  modeHint,
  modeLabel,
  summarizeChecks,
  useSystemStore,
  type ModeName,
} from '../stores/system'

/**
 * 双模式切换向导（M2.10~2.11 · §9.6）。
 *
 * 步骤：查看当前状态 → 预检（blocked 即禁止切换）→ 填写原因 → 确认切换。
 * 切换只改 `sys_mode_state` 单行（epoch+1），不改表；存量票据不失效、新签发按新模式。
 */
const store = useSystemStore()
const auth = useAuthStore()

const target = ref<ModeName>('external')
const reason = ref('')
const busy = ref(false)

const summary = computed(() => store.summary)
const checks = computed(() => store.precheck?.checks ?? [])
const counters = computed(() => summarizeChecks(checks.value))

function statusType(status: string): 'success' | 'warning' | 'danger' {
  if (status === 'blocked') return 'danger'
  if (status === 'warn') return 'warning'
  return 'success'
}

function notifyError(error: unknown): void {
  if (error instanceof ApiError) {
    const data = error.data as { reason?: string; epoch?: number } | null
    if (error.code === 'MODE_SWITCH_BLOCKED' && data?.reason === 'epoch_conflict') {
      ElMessage.error('模式已被他人切换（epoch 已过期），请刷新后重试')
      void store.load()
      return
    }
    ElMessage.error(error.message)
    return
  }
  ElMessage.error('请求失败，请查看后端日志')
}

async function load(): Promise<void> {
  busy.value = true
  try {
    await store.load()
    target.value = store.target
  } catch (error) {
    notifyError(error)
  } finally {
    busy.value = false
  }
}

async function runPrecheck(): Promise<void> {
  busy.value = true
  try {
    await store.check(target.value)
  } catch (error) {
    notifyError(error)
  } finally {
    busy.value = false
  }
}

async function applySwitch(): Promise<void> {
  if (!store.switchable) {
    ElMessage.warning('请先预检且没有阻断项')
    return
  }
  const confirm = await ElMessageBox.confirm(
    `确认从「${modeLabel(summary.value?.mode ?? 'internal')}」切换到「${modeLabel(target.value)}」？`,
    '切换双模式',
    { type: 'warning', confirmButtonText: '确认切换', cancelButtonText: '取消' },
  ).catch(() => false)
  if (confirm === false) return

  busy.value = true
  try {
    const result = await store.switchTo(target.value, reason.value)
    auth.mode = result.mode
    ElMessage.success(describeSwitchReport(result))
    reason.value = ''
    await load()
  } catch (error) {
    notifyError(error)
  } finally {
    busy.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="mode">
    <el-card>
      <template #header>
        <div class="mode-head">
          <span>双模式切换向导</span>
          <el-tag size="small" type="info">epoch {{ summary?.epoch ?? '-' }}</el-tag>
        </div>
      </template>

      <el-alert
        type="info"
        :closable="false"
        show-icon
        title="切换只改 sys_mode_state 单行（epoch+1），不改表结构；请求内只读一次 epoch，在途请求不半半。"
      />

      <el-descriptions :column="3" border class="mode-block">
        <el-descriptions-item label="当前模式">
          <el-tag :type="summary?.mode === 'external' ? 'warning' : 'success'">
            {{ modeLabel(summary?.mode ?? 'internal') }}
          </el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="上一模式">
          {{ summary?.previous_mode ? modeLabel(summary.previous_mode) : '—' }}
        </el-descriptions-item>
        <el-descriptions-item label="切换时间">{{ summary?.switched_at || '—' }}</el-descriptions-item>
        <el-descriptions-item label="迁移批次">
          {{ summary?.migration_batch_id || '—' }}
        </el-descriptions-item>
        <el-descriptions-item label="试看秒数">
          {{ summary?.policy.preview_seconds ?? 0 }}
        </el-descriptions-item>
        <el-descriptions-item label="票据 TTL">
          {{ summary?.policy.ttl_playlist ?? 0 }}s / key {{ summary?.policy.ttl_key ?? 0 }}s
        </el-descriptions-item>
      </el-descriptions>

      <h3 class="mode-title">实时计数</h3>
      <div class="mode-tags">
        <el-tag size="small">有效票据 {{ summary?.counts.active_tickets ?? 0 }}</el-tag>
        <el-tag size="small">preview 流水 {{ summary?.counts.preview_logs ?? 0 }}</el-tag>
        <el-tag size="small">在途作答 {{ summary?.counts.quiz_in_progress ?? 0 }}</el-tag>
        <el-tag size="small">转码任务 {{ summary?.counts.transcode_active ?? 0 }}</el-tag>
        <el-tag size="small">不可见课程 {{ summary?.counts.courses_hidden ?? 0 }}</el-tag>
      </div>

      <el-divider />

      <el-form label-width="120px">
        <el-form-item label="目标模式">
          <el-radio-group v-model="target" :disabled="busy">
            <el-radio-button :value="summary?.other_mode ?? 'external'">
              {{ modeLabel(summary?.other_mode ?? 'external') }}
            </el-radio-button>
          </el-radio-group>
          <p class="mode-hint">{{ modeHint(target) }}</p>
        </el-form-item>
        <el-form-item label="切换原因">
          <el-input v-model="reason" maxlength="200" show-word-limit placeholder="写入安全审计日志" />
        </el-form-item>
        <el-form-item>
          <el-button :loading="busy" @click="runPrecheck">执行预检</el-button>
          <el-button type="primary" :disabled="!store.switchable || busy" @click="applySwitch">
            切换模式
          </el-button>
          <span v-if="store.precheck" class="mode-hint">
            阻断 {{ counters.blocked }} / 告警 {{ counters.warn }} / 通过 {{ counters.ok }}
          </span>
        </el-form-item>
      </el-form>

      <template v-if="store.precheck">
        <el-table :data="checks" size="small" border>
          <el-table-column prop="code" label="检查项" width="200" />
          <el-table-column label="结果" width="100">
            <template #default="{ row }">
              <el-tag size="small" :type="statusType(row.status)">{{ row.status }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="count" label="数量" width="90" />
          <el-table-column prop="message" label="说明" />
        </el-table>
        <el-alert
          v-if="!store.switchable"
          class="mode-block"
          type="error"
          :closable="false"
          show-icon
          title="存在阻断项，禁止切换；请先按提示处置（如 manage.py db probe --persist）。"
        />
      </template>

      <el-alert
        v-if="store.lastReport"
        class="mode-block"
        type="success"
        :closable="false"
        show-icon
        :title="describeSwitchReport(store.lastReport)"
      />
    </el-card>
  </div>
</template>

<style scoped>
.mode-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.mode-block {
  margin-top: 12px;
}
.mode-title {
  margin: 18px 0 8px;
  font-size: 14px;
}
.mode-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.mode-hint {
  margin: 6px 0 0;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
