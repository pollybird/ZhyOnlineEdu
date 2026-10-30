import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import {
  clearInstallSession,
  installApi,
  loadInstallSession,
} from '../api/install'
import type {
  DbSetupOut,
  DbTestOut,
  InstallAdminOut,
  InstallFinishOut,
  InstallModeOut,
  InstallStateOut,
  InstallTokenOut,
} from '../api/types'

const DEFAULT_STEPS: readonly string[] = ['start', 'db', 'admin', 'mode', 'finish']

/** 安装向导状态（M1.5）：步骤以服务端状态机为准，前端只做展示与最小校验。 */
export const useInstallStore = defineStore('install', () => {
  const status = ref<InstallStateOut | null>(null)
  const session = ref(loadInstallSession())
  const loading = ref(false)
  const finished = ref<InstallFinishOut | null>(null)

  const steps = computed<readonly string[]>(() => status.value?.steps ?? DEFAULT_STEPS)
  const installed = computed(() => status.value?.is_installed === true)
  const currentStep = computed(() => status.value?.step ?? 'start')
  const blockers = computed<readonly string[]>(() => status.value?.blockers ?? [])
  /** el-steps 用（0 基）：done = 全部完成。 */
  const activeIndex = computed(() => {
    if (installed.value) {
      return steps.value.length
    }
    const index = steps.value.indexOf(currentStep.value)
    return index < 0 ? 0 : index
  })

  async function loadState(force = false): Promise<InstallStateOut> {
    if (status.value !== null && !force) {
      return status.value
    }
    loading.value = true
    try {
      status.value = await installApi.state()
      return status.value
    } finally {
      loading.value = false
    }
  }

  async function exchangeToken(token: string): Promise<InstallTokenOut> {
    const result = await installApi.exchangeToken(token)
    session.value = result.session
    await loadState(true)
    return result
  }

  async function testDb(): Promise<DbTestOut> {
    return installApi.testDb()
  }

  async function setupDb(): Promise<DbSetupOut> {
    const result = await installApi.setupDb()
    await loadState(true)
    return result
  }

  async function createAdmin(username: string, password: string): Promise<InstallAdminOut> {
    const result = await installApi.createAdmin({ username, password })
    await loadState(true)
    return result
  }

  async function selectMode(mode: 'internal' | 'external'): Promise<InstallModeOut> {
    const result = await installApi.setMode(mode)
    await loadState(true)
    return result
  }

  async function finish(): Promise<InstallFinishOut> {
    const result = await installApi.finish()
    finished.value = result
    // finish 后安装令牌与会话全部作废（后端 revoke），本地同步清理
    clearInstallSession()
    session.value = ''
    await loadState(true)
    return result
  }

  return {
    status,
    session,
    loading,
    finished,
    steps,
    installed,
    currentStep,
    blockers,
    activeIndex,
    loadState,
    exchangeToken,
    testDb,
    setupDb,
    createAdmin,
    selectMode,
    finish,
  }
})
