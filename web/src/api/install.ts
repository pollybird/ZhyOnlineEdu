import { apiGet, apiPost } from './request'
import type {
  DbSetupOut,
  DbTestOut,
  InstallAdminIn,
  InstallAdminOut,
  InstallFinishOut,
  InstallModeOut,
  InstallStateOut,
  InstallTokenOut,
} from './types'

/** 安装会话请求头（M1.5）：由 `POST /install/token` 换取，30 分钟过期。 */
export const INSTALL_SESSION_HEADER = 'X-ZHY-Install-Session'
const STORAGE_KEY = 'zhy.install.session'

let session = ''

function storage(): Storage | null {
  try {
    return typeof sessionStorage === 'undefined' ? null : sessionStorage
  } catch {
    return null
  }
}

/** 读取已缓存的安装会话（刷新页面后向导可继续）。 */
export function loadInstallSession(): string {
  if (session === '') {
    session = storage()?.getItem(STORAGE_KEY) ?? ''
  }
  return session
}

export function setInstallSession(token: string): void {
  session = token
  if (token === '') {
    storage()?.removeItem(STORAGE_KEY)
    return
  }
  storage()?.setItem(STORAGE_KEY, token)
}

export function clearInstallSession(): void {
  setInstallSession('')
}

function sessionConfig(): { headers: Record<string, string> } {
  const token = loadInstallSession()
  return { headers: token === '' ? {} : { [INSTALL_SESSION_HEADER]: token } }
}

/** 安装向导 7 端点（免鉴权；除 state/token 外均需安装会话头）。 */
export const installApi = {
  state(): Promise<InstallStateOut> {
    return apiGet<InstallStateOut>('/install/state')
  },
  async exchangeToken(token: string): Promise<InstallTokenOut> {
    const result = await apiPost<InstallTokenOut>('/install/token', { token })
    setInstallSession(result.session)
    return result
  },
  testDb(): Promise<DbTestOut> {
    return apiPost<DbTestOut>('/install/db/test', undefined, sessionConfig())
  },
  setupDb(): Promise<DbSetupOut> {
    return apiPost<DbSetupOut>('/install/db/setup', undefined, sessionConfig())
  },
  createAdmin(body: InstallAdminIn): Promise<InstallAdminOut> {
    return apiPost<InstallAdminOut>('/install/admin', body, sessionConfig())
  },
  setMode(mode: 'internal' | 'external'): Promise<InstallModeOut> {
    return apiPost<InstallModeOut>('/install/mode', { mode }, sessionConfig())
  },
  finish(): Promise<InstallFinishOut> {
    return apiPost<InstallFinishOut>('/install/finish', undefined, sessionConfig())
  },
}
