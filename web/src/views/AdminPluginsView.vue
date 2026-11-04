<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ApiError } from '../api/request'
import { pluginAdminApi } from '../api/plugins'
import type { PluginAvailableOut, PluginOut } from '../api/types'
import {
  capabilityLabel,
  pluginAssetUrl,
  pluginStateLabel,
  pluginStateTag,
  usePluginStore,
} from '../stores/plugin'

/**
 * 插件管理（M3.6）：安装 / 启用 / 禁用 / 卸载。
 *
 * - 「可安装」来自插件目录扫描（`available`），装完即从该列表消失并进入注册表；
 * - 口径与后端一致：禁用后才允许卸载（卸载按 `delete_seq` 强校验删除自有表），
 *   失败态插件可再次「启用」以重试挂载；`system:manage` 由路由与后端双重把关。
 */
const rows = ref<PluginOut[]>([])
const available = ref<PluginAvailableOut[]>([])
const pluginDir = ref('')
const loading = ref(false)
const errorMessage = ref('')
const pluginStore = usePluginStore()

const installableCount = computed(
  () => available.value.filter((item) => item.error === null).length,
)

function notifyError(error: unknown): void {
  if (error instanceof ApiError) {
    ElMessage.error(error.message)
    return
  }
  ElMessage.error(error instanceof Error ? error.message : '请求失败，请稍后重试')
}

async function refresh(): Promise<void> {
  loading.value = true
  errorMessage.value = ''
  try {
    const payload = await pluginAdminApi.list()
    rows.value = payload.items
    available.value = payload.available ?? []
    pluginDir.value = payload.plugin_dir
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '插件列表加载失败'
  } finally {
    loading.value = false
  }
}

async function run(task: () => Promise<string>): Promise<void> {
  loading.value = true
  try {
    ElMessage.success(await task())
    // 启用态变化 → 前端菜单/ESM 需重新对齐
    pluginStore.clear()
    await pluginStore.ensureLoaded()
  } catch (error) {
    notifyError(error)
  } finally {
    loading.value = false
    await refresh()
  }
}

function entryUrl(row: PluginOut): string {
  return pluginAssetUrl(row.code, row.entry_web) ?? '-'
}

async function install(item: PluginAvailableOut): Promise<void> {
  await run(async () => {
    const result = await pluginAdminApi.install(item.code)
    return `已安装 ${result.name} ${result.version}`
  })
}

async function enable(row: PluginOut): Promise<void> {
  await run(async () => {
    await pluginAdminApi.enable(row.code)
    return `已启用 ${row.name}`
  })
}

async function disable(row: PluginOut): Promise<void> {
  await run(async () => {
    await pluginAdminApi.disable(row.code)
    return `已禁用 ${row.name}`
  })
}

async function uninstall(row: PluginOut): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `卸载「${row.name}」会删除其自有表（前缀 ${row.table_prefix}），确认继续？`,
      '卸载确认',
      { type: 'warning' },
    )
  } catch {
    return
  }
  await run(async () => {
    await pluginAdminApi.uninstall(row.code)
    return `已卸载 ${row.name}`
  })
}

onMounted(() => {
  void refresh()
})
</script>

<template>
  <section class="admin-plugins">
    <header class="page-header">
      <h2>插件管理</h2>
      <el-button :loading="loading" @click="refresh">刷新</el-button>
    </header>
    <el-alert
      v-if="errorMessage"
      :title="errorMessage"
      type="error"
      :closable="false"
      show-icon
    />
    <el-alert
      v-if="pluginDir"
      :title="`插件目录：${pluginDir}`"
      description="把插件放进该目录后点「安装」；manifest 不合法的目录会直接标红原因。"
      type="info"
      :closable="false"
    />

    <el-card v-if="available.length > 0" class="plugin-card" shadow="never">
      <template #header>
        <span>可安装插件（{{ installableCount }}）</span>
      </template>
      <el-table :data="available" row-key="code">
        <el-table-column prop="name" label="插件" min-width="160">
          <template #default="{ row }">
            <div class="plugin-name">{{ row.name }}</div>
            <div class="plugin-code">{{ row.code }}</div>
          </template>
        </el-table-column>
        <el-table-column label="版本" width="110">
          <template #default="{ row }">{{ row.version ?? '-' }}</template>
        </el-table-column>
        <el-table-column label="说明" min-width="240">
          <template #default="{ row }">
            <span class="plugin-muted">{{ row.error ?? '未安装' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="120">
          <template #default="{ row }">
            <el-tooltip
              v-if="row.error !== null"
              :content="row.error"
              placement="top"
            >
              <el-button size="small" type="primary" disabled>安装</el-button>
            </el-tooltip>
            <el-button
              v-else
              size="small"
              type="primary"
              :disabled="loading"
              @click="install(row)"
            >
              安装
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-table v-loading="loading" :data="rows" row-key="code" class="plugin-table">
      <el-table-column prop="name" label="插件" min-width="160">
        <template #default="{ row }">
          <div class="plugin-name">{{ row.name }}</div>
          <div class="plugin-code">{{ row.code }}</div>
        </template>
      </el-table-column>
      <el-table-column prop="version" label="版本" width="100" />
      <el-table-column label="状态" width="110">
        <template #default="{ row }">
          <el-tag :type="pluginStateTag(row.state)" size="small">
            {{ pluginStateLabel(row.state) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="表前缀 / 迁移" min-width="180">
        <template #default="{ row }">
          <div>{{ row.table_prefix }}</div>
          <div class="plugin-muted">{{ row.schema_rev ?? '无迁移' }}</div>
        </template>
      </el-table-column>
      <el-table-column label="能力" min-width="180">
        <template #default="{ row }">
          <el-tag
            v-for="capability in row.capabilities"
            :key="capability"
            size="small"
            type="info"
            class="plugin-capability"
          >
            {{ capabilityLabel(capability) }}
          </el-tag>
          <span v-if="row.capabilities.length === 0" class="plugin-muted">无</span>
        </template>
      </el-table-column>
      <el-table-column label="前端入口" min-width="200">
        <template #default="{ row }">
          <span class="plugin-muted">{{ entryUrl(row) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="200" fixed="right">
        <template #default="{ row }">
          <el-button
            v-if="row.state !== 'enabled'"
            size="small"
            type="primary"
            :disabled="loading"
            @click="enable(row)"
          >
            {{ row.state === 'failed' ? '重试启用' : '启用' }}
          </el-button>
          <el-button
            v-if="row.state === 'enabled'"
            size="small"
            :disabled="loading"
            @click="disable(row)"
          >
            禁用
          </el-button>
          <el-button
            size="small"
            type="danger"
            :disabled="loading || row.state === 'enabled'"
            @click="uninstall(row)"
          >
            卸载
          </el-button>
        </template>
      </el-table-column>
      <el-table-column label="错误" min-width="200">
        <template #default="{ row }">
          <span class="plugin-muted">{{ row.error_message ?? '-' }}</span>
        </template>
      </el-table-column>
    </el-table>
  </section>
</template>
