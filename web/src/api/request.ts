import axios, { AxiosError, type AxiosRequestConfig, type InternalAxiosRequestConfig } from 'axios'
import type { ApiEnvelope, ErrorCode } from './types'

/**
 * 前端请求层（§14.1 [V2.3]）。
 *
 * 红线：
 * - 40201/40205 等播放票据错误由 `media/TicketedHls.ts` 接管：**不刷新 RT、不跳登录**；
 * - 只有 `action=relogin` 的 AUTH_*（附录 A）才静默刷新一次，失败后清理登录态并回登录页；
 * - 刷新采用单飞（single-flight）：并发 401 共用同一个 refresh 请求，避免 RT 轮换竞态。
 *
 * 判断逻辑抽成纯函数，便于 vitest（node 环境）直接测试。
 */

/** 播放票据类错误码：由播放器专属处理器接管（§14.1）。 */
export const MEDIA_CODES: readonly number[] = [40201, 40202, 40203, 40204, 40205, 40206]

/** 登录态失效类错误码（附录 A action=relogin）：才允许跳登录/刷新。 */
export const RELOGIN_CODES: readonly string[] = [
  'AUTH_TOKEN_EXPIRED',
  'AUTH_TOKEN_REVOKED',
  'AUTH_REFRESH_REPLAY',
  'ACCOUNT_EXPIRED',
  'ACCOUNT_DISABLED',
]

export function isMediaCode(code: unknown): boolean {
  return typeof code === 'number' && MEDIA_CODES.includes(code)
}

export function shouldRelogin(code: unknown): boolean {
  return typeof code === 'string' && RELOGIN_CODES.includes(code)
}

/** 单飞：并发调用共享同一个 Promise，结算后自动复位。 */
export function singleFlight<T>(task: () => Promise<T>): () => Promise<T> {
  let inflight: Promise<T> | null = null
  return () => {
    if (inflight === null) {
      inflight = task().finally(() => {
        inflight = null
      })
    }
    return inflight
  }
}

export class ApiError extends Error {
  readonly code: ErrorCode | 'NETWORK_ERROR'
  readonly http: number
  readonly data: unknown
  readonly requestId: string

  constructor(
    code: ErrorCode | 'NETWORK_ERROR',
    message: string,
    http: number,
    data: unknown,
    requestId: string,
  ) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.http = http
    this.data = data
    this.requestId = requestId
  }
}

let accessToken = ''

export function setAccessToken(token: string): void {
  accessToken = token
}

export function getAccessToken(): string {
  return accessToken
}

type Refresher = () => Promise<boolean>
type SessionExpiredHandler = () => void

let refresher: Refresher | null = null
let sessionExpiredHandler: SessionExpiredHandler | null = null

/** 由 auth store 注册：静默刷新 AT（RT 走 HttpOnly Cookie）。 */
export function setRefreshHandler(handler: Refresher | null): void {
  refresher = handler
}

/** 由 main.ts 注册：清理登录态并跳转 /login（播放码不会触发）。 */
export function setSessionExpiredHandler(handler: SessionExpiredHandler | null): void {
  sessionExpiredHandler = handler
}

export const request = axios.create({
  baseURL: '/api/v1',
  timeout: 15_000,
  withCredentials: true,
})

interface RetriableConfig extends InternalAxiosRequestConfig {
  __zhyRetried?: boolean
}

function toApiError(error: AxiosError<ApiEnvelope<unknown>>): ApiError {
  const body = error.response?.data
  const code = (body?.code ?? 'NETWORK_ERROR') as ErrorCode | 'NETWORK_ERROR'
  return new ApiError(
    code,
    body?.message ?? error.message,
    error.response?.status ?? 0,
    body?.data ?? null,
    body?.request_id ?? '',
  )
}

request.interceptors.request.use((config) => {
  if (accessToken) {
    config.headers.set('Authorization', `Bearer ${accessToken}`)
  }
  return config
})

request.interceptors.response.use(
  (response) => response,
  async (error: AxiosError<ApiEnvelope<unknown>>) => {
    const config = error.config as RetriableConfig | undefined
    const code = error.response?.data?.code

    // 播放票据错误：交给 TicketedHls 换票，不做任何登录态处理（§14.1）
    if (isMediaCode(code) || !shouldRelogin(code) || config === undefined) {
      return Promise.reject(toApiError(error))
    }

    const url = String(config.url ?? '')
    const skipRefresh = url.includes('/auth/login') || url.includes('/auth/refresh')
    if (!skipRefresh && config.__zhyRetried !== true) {
      config.__zhyRetried = true
      const refreshed = refresher === null ? false : await refresher()
      if (refreshed) {
        return request.request(config)
      }
    }
    sessionExpiredHandler?.()
    return Promise.reject(toApiError(error))
  },
)

async function unwrap<T>(pending: Promise<{ data: ApiEnvelope<T> }>): Promise<T> {
  const response = await pending
  return response.data.data
}

export function apiGet<T>(url: string, config?: AxiosRequestConfig): Promise<T> {
  return unwrap(request.get<ApiEnvelope<T>>(url, config))
}

export function apiPost<T>(
  url: string,
  body?: unknown,
  config?: AxiosRequestConfig,
): Promise<T> {
  return unwrap(request.post<ApiEnvelope<T>>(url, body, config))
}

export function apiPatch<T>(
  url: string,
  body?: unknown,
  config?: AxiosRequestConfig,
): Promise<T> {
  return unwrap(request.patch<ApiEnvelope<T>>(url, body, config))
}

export function apiDelete<T>(
  url: string,
  body?: unknown,
  config?: AxiosRequestConfig,
): Promise<T> {
  return unwrap(request.delete<ApiEnvelope<T>>(url, { ...config, data: body }))
}
