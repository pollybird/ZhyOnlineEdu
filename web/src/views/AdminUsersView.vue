<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ApiError } from '../api/request'
import { classAdminApi, usersAdminApi } from '../api/admin'
import type { ClassOut, ImportBatchOut, UserOut } from '../api/types'
import AdminMobileGate from '../components/AdminMobileGate.vue'
import { formatRelative } from '../utils/format'
import {
  batchStatusLabel,
  importConflictLabel,
  importRowActionLabel,
  roleLabel,
  userStatusLabel,
} from '../utils/labels'

/**
 * 管理端用户/班级/导入（M2.14 · M2.7）。
 * - 用户：分页查询、新建、停用、到期；
 * - 班级：列表、新建、加入成员；
 * - 导入：默认 `report_only`，批次可回看逐行结果。
 */
const loading = ref(false)
const keyword = ref('')
const statusFilter = ref('')
const page = ref(1)
const pageSize = 20
const users = ref<UserOut[]>([])
const total = ref(0)
const classes = ref<ClassOut[]>([])
const batches = ref<ImportBatchOut[]>([])
const createVisible = ref(false)
const importVisible = ref(false)
const batchDetail = ref<ImportBatchOut | null>(null)
const createForm = reactive({
  username: '',
  password: '',
  display_name: '',
  expire_at: '',
})
const importText = ref('')
const onConflict = ref<'report_only' | 'overwrite'>('report_only')

const pageCount = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))

function notifyError(error: unknown): void {
  if (error instanceof ApiError) {
    ElMessage.error(error.message)
    return
  }
  ElMessage.error('请求失败，请稍后重试')
}

async function guard(task: () => Promise<void>): Promise<void> {
  loading.value = true
  try {
    await task()
  } catch (error) {
    notifyError(error)
  } finally {
    loading.value = false
  }
}

async function loadUsers(): Promise<void> {
  await guard(async () => {
    const payload = await usersAdminApi.list({
      keyword: keyword.value.trim() === '' ? undefined : keyword.value.trim(),
      status: statusFilter.value === '' ? undefined : statusFilter.value,
      page: page.value,
      page_size: pageSize,
    })
    users.value = payload.items
    total.value = payload.total
  })
}

async function loadSide(): Promise<void> {
  await guard(async () => {
    const [classList, importList] = await Promise.all([
      classAdminApi.list({}),
      usersAdminApi.imports({ page: 1 }),
    ])
    classes.value = classList.items
    batches.value = importList.items
  })
}

function search(): void {
  page.value = 1
  void loadUsers()
}

function changePage(next: number): void {
  page.value = Math.min(pageCount.value, Math.max(1, next))
  void loadUsers()
}

async function createUser(): Promise<void> {
  if (createForm.username.trim() === '' || createForm.password === '') {
    ElMessage.warning('用户名与口令必填')
    return
  }
  await guard(async () => {
    await usersAdminApi.create({
      username: createForm.username.trim(),
      password: createForm.password,
      display_name: createForm.display_name.trim() || undefined,
      expire_at: createForm.expire_at.trim() || undefined,
    })
    createVisible.value = false
    createForm.username = ''
    createForm.password = ''
    createForm.display_name = ''
    createForm.expire_at = ''
    ElMessage.success('用户已创建')
    await loadUsers()
  })
}

async function disableUser(row: UserOut): Promise<void> {
  try {
    await ElMessageBox.confirm(`确认停用「${row.username}」？`, '停用确认', { type: 'warning' })
  } catch {
    return
  }
  await guard(async () => {
    await usersAdminApi.disable(row.id, row.version)
    ElMessage.success('已停用')
    await loadUsers()
  })
}

async function expireUser(row: UserOut): Promise<void> {
  let value = ''
  try {
    const result = await ElMessageBox.prompt('设置到期时间（ISO8601，留空表示立即到期）', '到期设置', {
      inputValue: row.expire_at ?? '',
      confirmButtonText: '保存',
      cancelButtonText: '取消',
    })
    value = result.value
  } catch {
    return
  }
  await guard(async () => {
    await usersAdminApi.expire(row.id, row.version, value.trim() === '' ? undefined : value.trim())
    ElMessage.success('已更新到期时间')
    await loadUsers()
  })
}

async function createClass(): Promise<void> {
  let name = ''
  try {
    const result = await ElMessageBox.prompt('输入班级名称', '新建班级', {
      inputPattern: /\S+/,
      inputErrorMessage: '名称不能为空',
    })
    name = result.value
  } catch {
    return
  }
  await guard(async () => {
    await classAdminApi.create({ name: name.trim() })
    ElMessage.success('班级已创建')
    await loadSide()
  })
}

async function addMembers(row: ClassOut): Promise<void> {
  let raw = ''
  try {
    const result = await ElMessageBox.prompt('输入用户 ID，多个用逗号分隔', '加入班级', {
      inputPattern: /\d+/,
      inputErrorMessage: '请输入数字 ID',
    })
    raw = result.value
  } catch {
    return
  }
  const ids = raw
    .split(',')
    .map((item) => Number(item.trim()))
    .filter((id) => Number.isInteger(id) && id > 0)
  await guard(async () => {
    await classAdminApi.addMembers(row.id, ids)
    ElMessage.success('已加入班级')
  })
}

async function openBatch(batchNo: string): Promise<void> {
  await guard(async () => {
    batchDetail.value = await usersAdminApi.importBatch(batchNo)
  })
}

async function submitImport(): Promise<void> {
  let rows: unknown
  try {
    rows = JSON.parse(importText.value)
  } catch {
    ElMessage.error('JSON 解析失败')
    return
  }
  if (!Array.isArray(rows) || rows.length === 0) {
    ElMessage.warning('请输入至少一行导入数据（JSON 数组）')
    return
  }
  await guard(async () => {
    const batch = await usersAdminApi.importJson(
      rows as { username: string; password?: string }[],
      onConflict.value,
    )
    importVisible.value = false
    importText.value = ''
    batchDetail.value = batch
    ElMessage.success('导入完成')
    await loadSide()
  })
}

onMounted(() => {
  void loadUsers()
  void loadSide()
})
</script>

<template>
  <AdminMobileGate>
    <div class="admin-page" v-loading="loading">
      <el-card class="admin-card">
        <template #header>
          <div class="admin-head">
            <span>用户管理</span>
            <div class="admin-actions">
              <el-button size="small" @click="importVisible = true">批量导入</el-button>
              <el-button type="primary" size="small" @click="createVisible = true">新建用户</el-button>
            </div>
          </div>
        </template>

        <div class="admin-filters">
          <el-input
            v-model="keyword"
            size="small"
            placeholder="用户名 / 学号 / 显示名"
            clearable
            @keyup.enter="search"
            @clear="search"
          />
          <el-select v-model="statusFilter" size="small" placeholder="全部状态" clearable @change="search">
            <el-option label="正常" value="active" />
            <el-option label="已停用" value="disabled" />
            <el-option label="已到期" value="expired" />
          </el-select>
          <el-button size="small" @click="search">搜索</el-button>
        </div>

        <el-table :data="users" size="small" row-key="id">
          <el-table-column prop="id" label="ID" width="64" />
          <el-table-column prop="username" label="用户名" min-width="120" />
          <el-table-column prop="display_name" label="显示名" min-width="110" />
          <el-table-column label="状态" width="90">
            <template #default="{ row }">
              <el-tag
                size="small"
                :type="row.status === 'active' ? 'success' : row.status === 'disabled' ? 'danger' : 'warning'"
              >
                {{ userStatusLabel(row.status) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="角色" min-width="120">
            <template #default="{ row }">
              <el-tag v-for="role in row.roles" :key="role" size="small" type="info">
                {{ roleLabel(role) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="班级" width="70">
            <template #default="{ row }">{{ row.class_count }}</template>
          </el-table-column>
          <el-table-column label="到期" width="120">
            <template #default="{ row }">{{ formatRelative(row.expire_at) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="140">
            <template #default="{ row }">
              <el-button link size="small" @click="expireUser(row)">到期</el-button>
              <el-button
                link
                size="small"
                type="danger"
                :disabled="row.status === 'disabled'"
                @click="disableUser(row)"
              >
                停用
              </el-button>
            </template>
          </el-table-column>
        </el-table>

        <div class="admin-pager">
          <el-button size="small" :disabled="page <= 1" @click="changePage(page - 1)">上一页</el-button>
          <span class="admin-pager-text">第 {{ page }}/{{ pageCount }} 页 · 共 {{ total }} 人</span>
          <el-button
            size="small"
            :disabled="page >= pageCount"
            @click="changePage(page + 1)"
          >
            下一页
          </el-button>
        </div>
      </el-card>

      <el-row :gutter="16">
        <el-col :xs="24" :md="10">
          <el-card class="admin-card">
            <template #header>
              <div class="admin-head">
                <span>班级</span>
                <el-button size="small" type="primary" @click="createClass">新建班级</el-button>
              </div>
            </template>
            <el-table :data="classes" size="small" row-key="id">
              <el-table-column prop="name" label="班级" min-width="120" />
              <el-table-column prop="member_count" label="人数" width="70" />
              <el-table-column label="状态" width="80">
                <template #default="{ row }">
                  <el-tag size="small" :type="row.is_enabled ? 'success' : 'info'">
                    {{ row.is_enabled ? '启用' : '停用' }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="操作" width="90">
                <template #default="{ row }">
                  <el-button link size="small" @click="addMembers(row)">加入成员</el-button>
                </template>
              </el-table-column>
            </el-table>
            <el-empty v-if="classes.length === 0" description="暂无班级" />
          </el-card>
        </el-col>

        <el-col :xs="24" :md="14">
          <el-card class="admin-card">
            <template #header>
              <div class="admin-head">
                <span>导入批次</span>
                <el-button size="small" @click="loadSide">刷新</el-button>
              </div>
            </template>
            <el-table :data="batches" size="small" row-key="batch_no">
              <el-table-column prop="batch_no" label="批次" min-width="150" />
              <el-table-column label="结果" width="150">
                <template #default="{ row }">
                  <span>新建 {{ row.created_rows }} · 更新 {{ row.updated_rows }}</span>
                </template>
              </el-table-column>
              <el-table-column label="失败" width="80">
                <template #default="{ row }">{{ row.invalid_rows }}</template>
              </el-table-column>
              <el-table-column label="操作" width="90">
                <template #default="{ row }">
                  <el-button link size="small" @click="openBatch(row.batch_no)">明细</el-button>
                </template>
              </el-table-column>
            </el-table>
            <el-empty v-if="batches.length === 0" description="暂无导入批次" />
          </el-card>
        </el-col>
      </el-row>

      <el-dialog v-model="createVisible" title="新建用户" width="440px">
        <el-form label-width="80px">
          <el-form-item label="用户名">
            <el-input v-model="createForm.username" />
          </el-form-item>
          <el-form-item label="口令">
            <el-input v-model="createForm.password" type="password" show-password />
          </el-form-item>
          <el-form-item label="显示名">
            <el-input v-model="createForm.display_name" />
          </el-form-item>
          <el-form-item label="到期时间">
            <el-input v-model="createForm.expire_at" placeholder="ISO8601，可留空" />
          </el-form-item>
        </el-form>
        <template #footer>
          <el-button @click="createVisible = false">取消</el-button>
          <el-button type="primary" :loading="loading" @click="createUser">创建</el-button>
        </template>
      </el-dialog>

      <el-dialog v-model="importVisible" title="批量导入用户" width="560px">
        <el-alert
          type="info"
          :closable="false"
          show-icon
          title="导入时默认只报告冲突、不覆盖已有用户；可在批次明细中逐行核对。"
          class="admin-alert"
        />
        <el-form label-width="90px">
          <el-form-item label="冲突策略">
            <el-select v-model="onConflict">
              <el-option label="仅报告，不覆盖" value="report_only" />
              <el-option label="覆盖已有用户" value="overwrite" />
            </el-select>
          </el-form-item>
          <el-form-item label="JSON 行">
            <el-input
              v-model="importText"
              type="textarea"
              :rows="8"
              placeholder='[{"username":"stu001","password":"Passw0rd","display_name":"张三"}]'
            />
          </el-form-item>
        </el-form>
        <template #footer>
          <el-button @click="importVisible = false">取消</el-button>
          <el-button type="primary" :loading="loading" @click="submitImport">开始导入</el-button>
        </template>
      </el-dialog>

      <el-drawer
        :model-value="batchDetail !== null"
        :title="batchDetail?.batch_no ?? '批次明细'"
        size="60%"
        @close="batchDetail = null"
      >
        <template v-if="batchDetail">
          <el-descriptions :column="2" border class="admin-desc">
            <el-descriptions-item label="状态">{{ batchStatusLabel(batchDetail.status) }}</el-descriptions-item>
            <el-descriptions-item label="冲突策略">
              {{ importConflictLabel(batchDetail.on_conflict_action) }}
            </el-descriptions-item>
            <el-descriptions-item label="总行数">{{ batchDetail.total_rows }}</el-descriptions-item>
            <el-descriptions-item label="失败行">{{ batchDetail.invalid_rows }}</el-descriptions-item>
          </el-descriptions>
          <el-table :data="batchDetail.rows ?? []" size="small" row-key="row">
            <el-table-column prop="row" label="行" width="60" />
            <el-table-column prop="username" label="用户名" min-width="110" />
            <el-table-column prop="user_no" label="学号" min-width="100" />
            <el-table-column label="动作" width="90">
              <template #default="{ row }">{{ importRowActionLabel(row.action) }}</template>
            </el-table-column>
            <el-table-column prop="reason" label="原因" min-width="140" />
          </el-table>
        </template>
      </el-drawer>
    </div>
  </AdminMobileGate>
</template>
