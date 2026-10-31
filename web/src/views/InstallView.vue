<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ApiError } from '../api/request'
import type { DbTestOut, InstallFinishOut } from '../api/types'
import { useInstallStore } from '../stores/install'
import { resolveInstallStep } from '../utils/install'

// 5 步向导（M1.5）：状态机以服务端 sys_install_state 为准，前端只做展示与最小校验
const store = useInstallStore()
const router = useRouter()

const token = ref('')
const dbResult = ref<DbTestOut | null>(null)
const finishResult = ref<InstallFinishOut | null>(null)
const errorMessage = ref('')
const busy = ref(false)
const selectedMode = ref<'internal' | 'external'>('internal')
const adminForm = reactive({ username: '', password: '', confirm: '' })

const STEP_TITLES: Record<string, string> = {
  start: '待校验令牌',
  db: '数据库',
  admin: '管理员',
  mode: '场景模式',
  finish: '待完成',
  done: '已安装',
}

const displayStep = computed(() => resolveInstallStep(store.currentStep, store.session !== ''))
const displayIndex = computed(() => {
  if (store.installed) return store.steps.length
  const index = store.steps.indexOf(displayStep.value)
  return index < 0 ? 0 : index
})
const statusText = computed(() => STEP_TITLES[displayStep.value] ?? displayStep.value)

async function run<T>(task: () => Promise<T>): Promise<T | null> {
  busy.value = true
  errorMessage.value = ''
  try {
    return await task()
  } catch (error) {
    // 会话过期（30 分钟）时自动回到令牌页，避免反复点击无反馈
    if (error instanceof ApiError && error.code === 'INSTALL_TOKEN_INVALID') {
      store.resetSession()
    }
    errorMessage.value = error instanceof ApiError ? error.message : '请求失败，请稍后重试'
    return null
  } finally {
    busy.value = false
  }
}

async function refreshState(): Promise<void> {
  await run(() => store.loadState(true))
}

async function doToken(): Promise<void> {
  const candidate = token.value.trim()
  if (candidate === '') {
    errorMessage.value = '请输入安装令牌'
    return
  }
  const result = await run(() => store.exchangeToken(candidate))
  if (result !== null) {
    token.value = ''
  }
}

async function doTest(): Promise<void> {
  dbResult.value = await run(() => store.testDb())
}

async function doSetup(): Promise<void> {
  const result = await run(() => store.setupDb())
  if (result !== null) {
    dbResult.value = null
  }
}

async function doAdmin(): Promise<void> {
  const username = adminForm.username.trim()
  if (username.length < 3) {
    errorMessage.value = '用户名至少 3 个字符'
    return
  }
  if (adminForm.password.length < 8) {
    errorMessage.value = '口令至少 8 个字符'
    return
  }
  if (adminForm.password !== adminForm.confirm) {
    errorMessage.value = '两次输入的口令不一致'
    return
  }
  const result = await run(() => store.createAdmin(username, adminForm.password))
  if (result !== null) {
    adminForm.password = ''
    adminForm.confirm = ''
  }
}

async function doMode(): Promise<void> {
  await run(() => store.selectMode(selectedMode.value))
}

async function doFinish(): Promise<void> {
  finishResult.value = await run(() => store.finish())
}

async function goLogin(): Promise<void> {
  await router.replace({ name: 'login' })
}

onMounted(async () => {
  await run(() => store.loadState(true))
})
</script>

<template>
  <div class="install-page">
    <el-card class="install-card">
      <template #header>
        <div class="install-head">
          <span>安装向导</span>
          <div class="install-head-right">
            <el-tag size="small" type="info">{{ statusText }}</el-tag>
            <el-button link type="primary" :loading="store.loading" @click="refreshState">
              刷新状态
            </el-button>
          </div>
        </div>
      </template>

      <el-steps :active="displayIndex" finish-status="success" align-center class="install-steps">
        <el-step title="开始" description="安装令牌" />
        <el-step title="数据库" description="连接与初始化" />
        <el-step title="管理员" description="首个超级管理员" />
        <el-step title="场景模式" description="内部 / 对外" />
        <el-step title="完成" description="保存配置" />
      </el-steps>

      <el-alert
        v-if="store.blockers.length > 0"
        type="error"
        :closable="false"
        show-icon
        title="检测到阻断项"
        class="install-alert"
      >
        <ul class="install-list">
          <li v-for="item in store.blockers" :key="item">{{ item }}</li>
        </ul>
      </el-alert>

      <el-alert
        v-if="errorMessage"
        type="error"
        :closable="false"
        show-icon
        :title="errorMessage"
        class="install-alert"
      />

      <section v-if="displayStep === 'start'" class="install-step">
        <h3>校验安装令牌</h3>
        <p class="install-hint">
          安装令牌在启动时生成，可从启动日志或部署配置中获取，仅在安装期间有效；
          安装完成后自动作废。
        </p>
        <div class="install-row">
          <el-input
            v-model="token"
            placeholder="粘贴安装令牌"
            show-password
            @keyup.enter="doToken"
          />
          <el-button type="primary" :loading="busy" @click="doToken">验证并继续</el-button>
        </div>
      </section>

      <section v-else-if="displayStep === 'db'" class="install-step">
        <h3>数据库连接与初始化</h3>
        <p class="install-hint">
          先测试连接，再初始化数据库；如有阻断项需先处理才能继续。
          安装会话 30 分钟内有效，超时后可点「重新校验令牌」。
        </p>
        <div class="install-row">
          <el-button :loading="busy" @click="doTest">测试数据库连接</el-button>
          <el-button type="primary" :disabled="dbResult?.ok !== true" :loading="busy" @click="doSetup">
            初始化数据库
          </el-button>
          <el-button link type="info" @click="store.resetSession()">重新校验令牌</el-button>
        </div>

        <el-descriptions v-if="dbResult" :column="2" border class="install-desc">
          <el-descriptions-item label="探测结果">
            <el-tag :type="dbResult.ok ? 'success' : 'danger'" size="small">
              {{ dbResult.ok ? '通过' : '存在阻断项' }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="数据库类型">
            {{ dbResult.flavor || '未知' }}
          </el-descriptions-item>
          <el-descriptions-item label="兼容模式">
            {{ dbResult.dialect || '未知' }}
          </el-descriptions-item>
          <el-descriptions-item label="提示" :span="2">
            <span v-if="(dbResult.warnings ?? []).length === 0">—</span>
            <ul v-else class="install-list">
              <li v-for="item in dbResult.warnings ?? []" :key="item">{{ item }}</li>
            </ul>
          </el-descriptions-item>
          <el-descriptions-item label="阻断项" :span="2">
            <span v-if="(dbResult.blockers ?? []).length === 0">—</span>
            <ul v-else class="install-list">
              <li v-for="item in dbResult.blockers ?? []" :key="item">{{ item }}</li>
            </ul>
          </el-descriptions-item>
        </el-descriptions>
      </section>

      <section v-else-if="displayStep === 'admin'" class="install-step">
        <h3>创建首个超级管理员</h3>
        <p class="install-hint">
          该账号将在安装完成后自动获得超级管理员权限，请妥善保管口令。
        </p>
        <el-form label-width="96px" class="install-form" @submit.prevent>
          <el-form-item label="用户名">
            <el-input v-model="adminForm.username" autocomplete="off" />
          </el-form-item>
          <el-form-item label="口令">
            <el-input v-model="adminForm.password" type="password" show-password autocomplete="new-password" />
          </el-form-item>
          <el-form-item label="确认口令">
            <el-input v-model="adminForm.confirm" type="password" show-password autocomplete="new-password" />
          </el-form-item>
        </el-form>
        <el-button type="primary" :loading="busy" @click="doAdmin">创建管理员</el-button>
      </section>

      <section v-else-if="displayStep === 'mode'" class="install-step">
        <h3>选择场景模式</h3>
        <p class="install-hint">
          内部模式：面向校内教学（默认关闭对外支付与自助注册）；对外模式：面向公众运营。
          选择后将保存为系统运行模式，安装完成后仍可由管理员切换。
        </p>
        <el-radio-group v-model="selectedMode" class="install-row">
          <el-radio value="internal">内部模式（校内闭环）</el-radio>
          <el-radio value="external">对外模式（对外运营）</el-radio>
        </el-radio-group>
        <el-button type="primary" :loading="busy" @click="doMode">保存场景模式</el-button>
      </section>

      <section v-else-if="displayStep === 'finish'" class="install-step">
        <h3>完成安装</h3>
        <p class="install-hint">
          将保存环境配置、生成媒体加密密钥并创建管理员账号；完成后需重启服务才能登录使用。
        </p>
        <el-button type="primary" :loading="busy" @click="doFinish">完成安装</el-button>
      </section>

      <section v-else class="install-step">
        <el-result icon="success" title="安装完成" sub-title="重启服务后即可登录使用">
          <template #extra>
            <el-button type="primary" @click="goLogin">前往登录</el-button>
          </template>
        </el-result>
        <el-alert
          v-if="finishResult?.restart_required"
          type="warning"
          :closable="false"
          show-icon
          title="需要重启服务：安装完成后必须重启后端服务才能登录使用。"
        />
        <el-descriptions v-if="finishResult" :column="2" border>
          <el-descriptions-item label="管理员">{{ finishResult.admin || '—' }}</el-descriptions-item>
          <el-descriptions-item label="数据库">{{ finishResult.flavor || '—' }}</el-descriptions-item>
        </el-descriptions>
      </section>
    </el-card>
  </div>
</template>
