import { apiGet, apiPatch, apiPost } from './request'
import type {
  BootstrapOut,
  LoginIn,
  LoginOut,
  LogoutOut,
  PasswordIn,
  PasswordOut,
} from './types'

/** 认证接口（M1.6）：AT 走 Bearer，RT 由 HttpOnly Cookie 自动携带。 */
export const authApi = {
  login(body: LoginIn): Promise<LoginOut> {
    return apiPost<LoginOut>('/auth/login', body)
  },
  refresh(): Promise<LoginOut> {
    return apiPost<LoginOut>('/auth/refresh')
  },
  logout(): Promise<LogoutOut> {
    return apiPost<LogoutOut>('/auth/logout')
  },
  bootstrap(): Promise<BootstrapOut> {
    return apiGet<BootstrapOut>('/auth/bootstrap')
  },
  changePassword(body: PasswordIn): Promise<PasswordOut> {
    return apiPatch<PasswordOut>('/auth/password', body)
  },
}
