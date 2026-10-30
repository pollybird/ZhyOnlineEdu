<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ApiError } from '../api/request'
import type { DbTestOut, InstallFinishOut } from '../api/types'
import { useInstallStore } from '../stores/install'

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

const statusText = computed(() => STEP_TITLES[store.currentStep] ?? store.currentStep)
const capsEntries = computed<readonly [string, boolean][]>(() =>
  Object.entries(dbResult.value?.caps ?? {}),
)

async function run<T>(task: () => Promise<T>): Promise<T | null> {
  busy.value = true
  errorMessage.value = ''
  try {
    return await task()
  } catch (error) {
    errorMessage.value = error instanceof ApiError ? error.message : '请求失败，请查看后端日志'
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
    errorMessage.value = '请输入启动日志中的一次性安装令牌'
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

      <el-steps :active="store.activeIndex" finish-status="success" align-center class="install-steps">
        <el-step title="开始" description="一次性令牌" />
        <el-step title="数据库" description="探测与建库" />
        <el-step title="管理员" description="首个超管" />
        <el-step title="场景模式" description="internal / external" />
        <el-step title="完成" description="固化能力位" />
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

      <section v-if="store.currentStep === 'start'" class="install-step">
        <h3>校验安装令牌</h3>
        <p class="install-hint">
          令牌来自启动日志（install_audit）或环境变量 ZHY_INSTALL_TOKEN，仅安装期有效；
          安装完成后所有令牌与会话立即作废。
        </p>
        <div class="install-row">
          <el-input
            v-model="token"
            placeholder="粘贴一次性安装令牌"
            show-password
            @keyup.enter="doToken"
          />
          <el-button type="primary" :loading="busy" @click="doToken">换取安装会话</el-button>
        </div>
      </section>

      <section v-else-if="store.currentStep === 'db'" class="install-step">
        <h3>数据库探测与建库</h3>
        <p class="install-hint">
          先探测（不固化能力位）→ 再执行 Alembic 建库/升级；blockers 非空时禁止下一步。
        </p>
        <div class="install-row">
          <el-button :loading="busy" @click="doTest">测试数据库连接</el-button>
          <el-button type="primary" :disabled="dbResult?.ok !== true" :loading="busy" @click="doSetup">
            建库 / 升级
          </el-button>
        </div>

        <el-descriptions v-if="dbResult" :column="2" border class="install-desc">
          <el-descriptions-item label="探测结果">
            <el-tag :type="dbResult.ok ? 'success' : 'danger'" size="small">
              {{ dbResult.ok ? '通过' : '存在阻断项' }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="数据库">
            {{ dbResult.flavor || '未知' }} / {{ dbResult.dialect || '未知' }}
          </el-descriptions-item>
          <el-descriptions-item label="能力位" :span="2">
            <el-tag
              v-for="[key, value] in capsEntries"
              :key="key"
              size="small"
              :type="value ? 'success' : 'info'"
              class="install-tag"
            >
              {{ key }}
            </el-tag>
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

      <section v-else-if="store.currentStep === 'admin'" class="install-step">
        <h3>创建首个超级管理员</h3>
        <p class="install-hint">
          口令使用 Argon2id 存储；finish 时会落到账号表并授予 super_admin。
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

      <section v-else-if="store.currentStep === 'mode'" class="install-step">
        <h3>选择场景模式</h3>
        <p class="install-hint">
          internal：校内闭环（默认关闭对外支付/注册）；external：对外运营。
          模式写入 sys_mode_config 并锁定能力位快照。
        </p>
        <el-radio-group v-model="selectedMode" class="install-row">
          <el-radio value="internal">internal（校内闭环）</el-radio>
          <el-radio value="external">external（对外运营）</el-radio>
        </el-radio-group>
        <el-button type="primary" :loading="busy" @click="doMode">保存场景模式</el-button>
      </section>

      <section v-else-if="store.currentStep === 'finish'" class="install-step">
        <h3>完成安装</h3>
        <p class="install-hint">
          将固化 sys_compat_flags、生成 RS256 密钥、把超管写入账号表；完成后需重启后端进程，
          业务路由才会在 READY 状态下装配。
        </p>
        <el-button type="primary" :loading="busy" @click="doFinish">完成安装</el-button>
      </section>

      <section v-else class="install-step">
        <el-result icon="success" title="安装完成" sub-title="重启后端进程后即可登录使用">
          <template #extra>
            <el-button type="primary" @click="goLogin">前往登录</el-button>
          </template>
        </el-result>
        <el-alert
          v-if="finishResult?.restart_required"
          type="warning"
          :closable="false"
          show-icon
          title="需要重启后端：安装期未注册业务路由（Flask 首请求后禁止注册 Blueprint）"
        />
        <el-descriptions v-if="finishResult" :column="2" border>
          <el-descriptions-item label="管理员">{{ finishResult.admin || '—' }}</el-descriptions-item>
          <el-descriptions-item label="数据库">{{ finishResult.flavor || '—' }}</el-descriptions-item>
        </el-descriptions>
      </section>
    </el-card>
  </div>
</template>
