<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useRouter } from 'vue-router'
import { memberApi } from '../api/member'
import type { MemberLevelOut, MemberMeOut, PayOrderListOut, PayOrderOut } from '../api/types'
import StateView from '../components/StateView.vue'
import { describeError, formatRelative } from '../utils/format'
import { orderStatusLabel } from '../utils/labels'
import type { ErrorView } from '../utils/format'

/**
 * 学员端会员与订单（M2.14 · M2.12）。
 * - 档位只读 `/member/offers`（仅启用），管理端维护仍走 `/member/levels`；
 * - 下单冻结档位快照（金额/等级/时长），改价不影响在途订单；
 * - mock 通道不自动到账，演示环境由管理员「查单补偿」推进状态。
 */
const router = useRouter()

const loading = ref(true)
const error = ref<ErrorView | null>(null)
const me = ref<MemberMeOut | null>(null)
const offers = ref<MemberLevelOut[]>([])
const orders = ref<PayOrderListOut | null>(null)
const busy = ref(false)

const state = computed(() => {
  if (loading.value) return 'loading'
  if (error.value !== null) return error.value.state
  return offers.value.length === 0 ? 'empty' : 'ready'
})

async function load(): Promise<void> {
  loading.value = true
  error.value = null
  try {
    const [member, levels, history] = await Promise.all([
      memberApi.me(),
      memberApi.offers(),
      memberApi.orders({ page: 1 }),
    ])
    me.value = member
    offers.value = levels.items
    orders.value = history
  } catch (caught) {
    error.value = describeError(caught)
  } finally {
    loading.value = false
  }
}

async function buy(level: MemberLevelOut): Promise<void> {
  if (busy.value) return
  busy.value = true
  error.value = null
  try {
    const order = await memberApi.createOrder(level.id, 'mock')
    ElMessage.success(`订单已创建：${order.order_no}`)
    const [member, history] = await Promise.all([
      memberApi.me(),
      memberApi.orders({ page: 1 }),
    ])
    me.value = member
    orders.value = history
  } catch (caught) {
    error.value = describeError(caught)
  } finally {
    busy.value = false
  }
}

async function cancel(order: PayOrderOut): Promise<void> {
  if (busy.value) return
  busy.value = true
  try {
    await memberApi.cancelOrder(order.order_no)
    ElMessage.success('订单已取消')
    orders.value = await memberApi.orders({ page: 1 })
  } catch (caught) {
    error.value = describeError(caught)
  } finally {
    busy.value = false
  }
}

function statusType(status: string): 'success' | 'warning' | 'info' | 'danger' {
  if (status === 'paid') return 'success'
  if (status === 'created' || status === 'paying') return 'warning'
  if (status === 'failed' || status === 'expired') return 'danger'
  return 'info'
}

function handleErrorAction(): void {
  if (error.value?.action === 'back') {
    void router.push({ name: 'home' })
    return
  }
  void load()
}

onMounted(() => {
  void load()
})
</script>

<template>
  <div class="member-page">
    <StateView
      :state="state"
      :title="error?.title"
      :message="error?.message"
      :code="error?.code"
      action-text="重新加载"
      @action="handleErrorAction"
    >
      <el-card v-if="me" class="member-status">
        <template #header>
          <div class="member-status-head">
            <span>我的会员</span>
            <el-tag :type="me.active ? 'success' : 'info'">
              {{ me.active ? '会员有效' : '未开通会员' }}
            </el-tag>
          </div>
        </template>
        <el-descriptions :column="2" border>
          <el-descriptions-item label="等级">
            {{ me.level_name ?? '—' }}
          </el-descriptions-item>
          <el-descriptions-item label="到期时间" :span="2">
            {{ me.expire_at ?? '—' }}
          </el-descriptions-item>
        </el-descriptions>
        <p class="member-hint">
          会员课程播放与测验以实际会员状态为准；会员到期或退班后将无法继续学习。
        </p>
      </el-card>

      <el-card class="member-offers">
        <template #header>可购档位</template>
        <el-row :gutter="16">
          <el-col v-for="level in offers" :key="level.id" :xs="24" :sm="12" :md="8">
            <el-card class="member-offer" shadow="hover">
              <div class="member-offer-head">
                <span class="member-offer-name">{{ level.name }}</span>
              </div>
              <div class="member-offer-price">¥{{ level.price }}</div>
              <p class="member-offer-meta">
                会员等级 {{ level.level }} · 有效期 {{ level.duration_days }} 天
              </p>
              <el-button type="primary" :loading="busy" @click="buy(level)">立即开通</el-button>
            </el-card>
          </el-col>
        </el-row>
      </el-card>

      <el-card class="member-orders">
        <template #header>
          <div class="member-status-head">
            <span>我的订单</span>
            <el-button link @click="load">刷新</el-button>
          </div>
        </template>
        <el-table :data="orders?.items ?? []" size="small" row-key="order_no">
          <el-table-column prop="order_no" label="订单号" min-width="180" />
          <el-table-column prop="level_name" label="档位" width="110" />
          <el-table-column prop="amount" label="金额" width="90" />
          <el-table-column label="状态" width="90">
            <template #default="{ row }">
              <el-tag size="small" :type="statusType(row.status)">{{ orderStatusLabel(row.status) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="创建时间" width="130">
            <template #default="{ row }">{{ formatRelative(row.created_at) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="110">
            <template #default="{ row }">
              <el-button
                link
                size="small"
                type="danger"
                :disabled="row.status !== 'created' && row.status !== 'paying'"
                @click="cancel(row)"
              >
                取消
              </el-button>
            </template>
          </el-table-column>
        </el-table>
        <el-empty v-if="(orders?.items.length ?? 0) === 0" description="暂无订单" />
        <p class="member-hint">
          下单后请按页面提示完成支付；如长时间未到账，请联系管理员处理。
        </p>
      </el-card>
    </StateView>
  </div>
</template>
