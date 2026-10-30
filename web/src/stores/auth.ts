import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { authApi } from '../api/auth'
import {
  ApiError,
  setAccessToken,
  setRefreshHandler,
  singleFlight,
} from '../api/request'
import type { AuthUserOut, LoginOut } from '../api/types'

/** 权限判定（纯函数，便于单测）：`progress:direct` 等码不在内置角色中即为 false。 */
export function hasPermission(permissions: readonly string[] | undefined, code: string): boolean {
  return permissions?.includes(code) ?? false
}

/**
 * 登录态（M1.6/M1.7）：AT 只存内存，页面刷新后靠 RT（HttpOnly Cookie）静默恢复。
 *
 * `restore()` 与请求拦截器的刷新共用同一个单飞任务，避免并发 401 触发多次 RT 轮换。
 */
export const useAuthStore = defineStore('auth', () => {
  const accessToken = ref('')
  const user = ref<AuthUserOut | null>(null)
  const mode = ref<string | null>(null)
  const serverTime = ref('')
  /** RT 重放（AUTH_REFRESH_REPLAY）后禁止再静默刷新，必须重新登录。 */
  const canRestore = ref(true)

  const loggedIn = computed(() => accessToken.value !== '' && user.value !== null)
  const permissions = computed<readonly string[]>(() => user.value?.permissions ?? [])

  function can(code: string): boolean {
    return hasPermission(permissions.value, code)
  }

  function apply(result: LoginOut): void {
    accessToken.value = result.access_token
    user.value = result.user
    canRestore.value = true
    setAccessToken(result.access_token)
  }

  function clear(): void {
    accessToken.value = ''
    user.value = null
    mode.value = null
    setAccessToken('')
  }

  async function refreshSession(): Promise<boolean> {
    try {
      apply(await authApi.refresh())
      return true
    } catch (error) {
      if (error instanceof ApiError && error.code === 'AUTH_REFRESH_REPLAY') {
        canRestore.value = false
      }
      clear()
      return false
    }
  }

  const refreshOnce = singleFlight(refreshSession)

  async function login(username: string, password: string): Promise<void> {
    apply(await authApi.login({ username, password }))
  }

  async function bootstrap(): Promise<void> {
    const payload = await authApi.bootstrap()
    user.value = payload.user
    mode.value = payload.mode ?? null
    serverTime.value = payload.server_time
  }

  async function restore(): Promise<boolean> {
    if (loggedIn.value) {
      return true
    }
    if (!canRestore.value) {
      return false
    }
    return refreshOnce()
  }

  async function logout(): Promise<void> {
    try {
      await authApi.logout()
    } catch {
      // 会话已失效也按退出成功处理，避免卡在框架页
    }
    clear()
  }

  // 请求拦截器遇到 AUTH_* 401 时调用（单飞）
  setRefreshHandler(refreshOnce)

  return {
    accessToken,
    user,
    mode,
    serverTime,
    loggedIn,
    permissions,
    can,
    clear,
    login,
    bootstrap,
    restore,
    logout,
  }
})
