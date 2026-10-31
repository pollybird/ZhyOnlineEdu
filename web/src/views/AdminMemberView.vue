<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { memberAdminApi } from '../api/admin'
import type { MemberLevelOut, PayOrderOut, SubscriptionOut } from '../api/types'
import AdminMobileGate from '../components/AdminMobileGate.vue'
import { formatRelative } from '../utils/format'
import {
  orderStatusLabel,
  subscriptionSourceLabel,
  subscriptionStatusLabel,
} from '../utils/labels'

/**
 * 管理端会员（M2.14 · M2.12）。
 * - 档位：price 使用十进制字符串（统一 6 位小数，禁止 float）；
 * - 赠送：`source=grant`，不产生订单，已有订阅按最晚到期叠加；
 * - 查单补偿：与回调共用「不倒退」状态机，重复补偿幂等。
 */
const loading = ref(false)
const levels = ref<MemberLevelOut[]>([])
const subscriptions = ref<SubscriptionOut[]>([])
const levelDialog = ref(false)
const editingLevel = ref<MemberLevelOut | null>(null)
const grantDialog = ref(false)
const orderNo = ref('')
const reconcileStatus = ref<'paying' | 'paid' | 'failed'>('paid')
const orderInfo = ref<PayOrderOut | null>(null)

const levelForm = reactive({
  code: '',
  name: '',
  level: 1,
  price: '0.000000',
  duration_days: 30,
  is_enabled: true,
  sort_order: 0,
})
const grantForm = reactive({
  user_id: 1,
  level_id: 1,
  duration_days: 0,
})
const subUserId = ref<number | null>(null)
const subStatus = ref('')

function notifyError(error: unknown): void {
  ElMessage.error(error instanceof Error ? error.message : '请求失败，请稍后重试')
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

async function loadLevels(): Promise<void> {
  await guard(async () => {
    const payload = await memberAdminApi.levels({})
    levels.value = payload.items
    if (grantForm.level_id === 1 && levels.value.length > 0) {
      grantForm.level_id = levels.value[0].id
    }
  })
}

async function loadSubscriptions(): Promise<void> {
  await guard(async () => {
    const payload = await memberAdminApi.subscriptions({
      user_id: subUserId.value ?? undefined,
      status: subStatus.value === '' ? undefined : subStatus.value,
      page: 1,
    })
    subscriptions.value = payload.items
  })
}

function openCreateLevel(): void {
  editingLevel.value = null
  levelForm.code = ''
  levelForm.name = ''
  levelForm.level = 1
  levelForm.price = '0.000000'
  levelForm.duration_days = 30
  levelForm.is_enabled = true
  levelForm.sort_order = 0
  levelDialog.value = true
}

function openEditLevel(row: MemberLevelOut): void {
  editingLevel.value = row
  levelForm.code = row.code
  levelForm.name = row.name
  levelForm.level = row.level
  levelForm.price = row.price
  levelForm.duration_days = row.duration_days
  levelForm.is_enabled = row.is_enabled
  levelForm.sort_order = row.sort_order
  levelDialog.value = true
}

async function saveLevel(): Promise<void> {
  if (levelForm.code.trim() === '' || levelForm.name.trim() === '') {
    ElMessage.warning('档位编码与名称必填')
    return
  }
  if (!/^\d+(\.\d{1,6})?$/.test(levelForm.price.trim())) {
    ElMessage.warning('价格必须是最多 6 位小数的十进制字符串')
    return
  }
  await guard(async () => {
    const body = {
      code: levelForm.code.trim(),
      name: levelForm.name.trim(),
      level: levelForm.level,
      price: levelForm.price.trim(),
      duration_days: levelForm.duration_days,
      is_enabled: levelForm.is_enabled,
      sort_order: levelForm.sort_order,
    }
    if (editingLevel.value === null) {
      await memberAdminApi.createLevel(body)
      ElMessage.success('档位已创建')
    } else {
      await memberAdminApi.updateLevel(editingLevel.value.id, {
        ...body,
        version: editingLevel.value.version,
      })
      ElMessage.success('档位已更新')
    }
    levelDialog.value = false
    await loadLevels()
  })
}

async function toggleLevel(row: MemberLevelOut): Promise<void> {
  await guard(async () => {
    await memberAdminApi.updateLevel(row.id, {
      version: row.version,
      is_enabled: !row.is_enabled,
    })
    ElMessage.success(row.is_enabled ? '档位已停用' : '档位已启用')
    await loadLevels()
  })
}

function openGrant(): void {
  if (levels.value.length === 0) {
    ElMessage.warning('请先创建会员档位')
    return
  }
  grantForm.user_id = 1
  grantForm.level_id = levels.value[0].id
  grantForm.duration_days = 0
  grantDialog.value = true
}

async function grant(): Promise<void> {
  await guard(async () => {
    await memberAdminApi.grant({
      user_id: grantForm.user_id,
      level_id: grantForm.level_id,
      duration_days: grantForm.duration_days > 0 ? grantForm.duration_days : undefined,
    })
    grantDialog.value = false
    ElMessage.success('会员已开通')
    await loadSubscriptions()
  })
}

async function loadOrder(): Promise<void> {
  if (orderNo.value.trim() === '') {
    ElMessage.warning('请输入订单号')
    return
  }
  await guard(async () => {
    orderInfo.value = await memberAdminApi.order(orderNo.value.trim())
  })
}

async function reconcile(): Promise<void> {
  if (orderNo.value.trim() === '') {
    ElMessage.warning('请输入订单号')
    return
  }
  await guard(async () => {
    const result = await memberAdminApi.reconcile(orderNo.value.trim(), reconcileStatus.value)
    ElMessage.success(
      result.duplicate ? '重复补偿，状态未推进' : `补偿已应用：${result.status}`,
    )
    orderInfo.value = await memberAdminApi.order(orderNo.value.trim())
    await loadSubscriptions()
  })
}

onMounted(() => {
  void loadLevels()
  void loadSubscriptions()
})
</script>

<template>
  <AdminMobileGate>
    <div class="admin-page" v-loading="loading">
      <el-card class="admin-card">
        <template #header>
          <div class="admin-head">
            <span>会员档位</span>
            <div class="admin-actions">
              <el-button size="small" @click="openGrant">赠送开通</el-button>
              <el-button type="primary" size="small" @click="openCreateLevel">新建档位</el-button>
            </div>
          </div>
        </template>

        <el-table :data="levels" size="small" row-key="id">
          <el-table-column prop="code" label="编码" min-width="110" />
          <el-table-column prop="name" label="名称" min-width="120" />
          <el-table-column prop="level" label="等级" width="64" />
          <el-table-column prop="price" label="价格" width="110" />
          <el-table-column prop="duration_days" label="天数" width="70" />
          <el-table-column label="启用" width="70">
            <template #default="{ row }">
              <el-tag size="small" :type="row.is_enabled ? 'success' : 'info'">
                {{ row.is_enabled ? '是' : '否' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="140">
            <template #default="{ row }">
              <el-button link size="small" @click="openEditLevel(row)">编辑</el-button>
              <el-button link size="small" @click="toggleLevel(row)">
                {{ row.is_enabled ? '停用' : '启用' }}
              </el-button>
            </template>
          </el-table-column>
        </el-table>
        <el-empty v-if="levels.length === 0" description="暂无会员档位" />
      </el-card>

      <el-card class="admin-card">
        <template #header>
          <div class="admin-head">
            <span>订阅记录</span>
            <div class="admin-filters">
              <el-input-number
                v-model="subUserId"
                size="small"
                :min="1"
                placeholder="用户 ID"
                :controls="false"
              />
              <el-select v-model="subStatus" size="small" placeholder="全部状态" clearable>
                <el-option label="有效" value="active" />
                <el-option label="已到期" value="expired" />
                <el-option label="已撤销" value="revoked" />
              </el-select>
              <el-button size="small" @click="loadSubscriptions">查询</el-button>
            </div>
          </div>
        </template>
        <el-table :data="subscriptions" size="small" row-key="id">
          <el-table-column prop="user_id" label="用户" width="80" />
          <el-table-column prop="level_name" label="档位" width="110" />
          <el-table-column label="来源" width="80">
            <template #default="{ row }">{{ subscriptionSourceLabel(row.source) }}</template>
          </el-table-column>
          <el-table-column label="状态" width="90">
            <template #default="{ row }">
              <el-tag
                size="small"
                :type="row.status === 'active' ? 'success' : row.status === 'revoked' ? 'danger' : 'info'"
              >
                {{ subscriptionStatusLabel(row.status) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="到期" width="150">
            <template #default="{ row }">{{ row.expire_at ?? '—' }}</template>
          </el-table-column>
          <el-table-column prop="order_no" label="订单号" min-width="160" />
        </el-table>
        <el-empty v-if="subscriptions.length === 0" description="暂无订阅记录" />
      </el-card>

      <el-card class="admin-card">
        <template #header>
          <div class="admin-head">
            <span>订单查询与补偿</span>
          </div>
        </template>
        <div class="admin-filters">
          <el-input v-model="orderNo" size="small" placeholder="订单号" />
          <el-select v-model="reconcileStatus" size="small">
            <el-option label="已支付" value="paid" />
            <el-option label="支付中" value="paying" />
            <el-option label="支付失败" value="failed" />
          </el-select>
          <el-button size="small" @click="loadOrder">查单</el-button>
          <el-button type="primary" size="small" @click="reconcile">补偿</el-button>
        </div>
        <el-descriptions v-if="orderInfo" :column="2" border class="admin-desc">
          <el-descriptions-item label="订单号">{{ orderInfo.order_no }}</el-descriptions-item>
          <el-descriptions-item label="状态">{{ orderStatusLabel(orderInfo.status) }}</el-descriptions-item>
          <el-descriptions-item label="档位">{{ orderInfo.level_name }}</el-descriptions-item>
          <el-descriptions-item label="金额">{{ orderInfo.amount }}</el-descriptions-item>
          <el-descriptions-item label="创建时间">
            {{ formatRelative(orderInfo.created_at) }}
          </el-descriptions-item>
          <el-descriptions-item label="支付时间">
            {{ orderInfo.paid_at ?? '—' }}
          </el-descriptions-item>
        </el-descriptions>
        <el-empty v-if="orderInfo === null" description="输入订单号后查单 / 补偿" />
      </el-card>

      <el-dialog
        v-model="levelDialog"
        :title="editingLevel === null ? '新建会员档位' : '编辑会员档位'"
        width="480px"
      >
        <el-form label-width="90px">
          <el-form-item label="编码">
            <el-input v-model="levelForm.code" placeholder="如 vip_month" />
          </el-form-item>
          <el-form-item label="名称">
            <el-input v-model="levelForm.name" />
          </el-form-item>
          <el-form-item label="等级">
            <el-input-number v-model="levelForm.level" :min="1" :max="10" />
          </el-form-item>
          <el-form-item label="价格">
            <el-input v-model="levelForm.price" placeholder="最多 6 位小数，如 99.000000" />
          </el-form-item>
          <el-form-item label="天数">
            <el-input-number v-model="levelForm.duration_days" :min="1" :max="3650" />
          </el-form-item>
          <el-form-item label="排序">
            <el-input-number v-model="levelForm.sort_order" :min="0" :max="9999" />
          </el-form-item>
          <el-form-item label="启用">
            <el-switch v-model="levelForm.is_enabled" />
          </el-form-item>
        </el-form>
        <template #footer>
          <el-button @click="levelDialog = false">取消</el-button>
          <el-button type="primary" :loading="loading" @click="saveLevel">保存</el-button>
        </template>
      </el-dialog>

      <el-dialog v-model="grantDialog" title="赠送开通会员" width="440px">
        <el-form label-width="90px">
          <el-form-item label="用户 ID">
            <el-input-number v-model="grantForm.user_id" :min="1" />
          </el-form-item>
          <el-form-item label="档位">
            <el-select v-model="grantForm.level_id">
              <el-option
                v-for="level in levels"
                :key="level.id"
                :label="`${level.name}（${level.code}）`"
                :value="level.id"
              />
            </el-select>
          </el-form-item>
          <el-form-item label="天数">
            <el-input-number v-model="grantForm.duration_days" :min="0" :max="3650" />
          </el-form-item>
        </el-form>
        <p class="admin-hint">天数填 0 表示使用档位默认时长；已有有效订阅按最晚到期叠加。</p>
        <template #footer>
          <el-button @click="grantDialog = false">取消</el-button>
          <el-button type="primary" :loading="loading" @click="grant">开通</el-button>
        </template>
      </el-dialog>
    </div>
  </AdminMobileGate>
</template>
