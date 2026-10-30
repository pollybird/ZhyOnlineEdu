import { apiGet, apiPost } from './request'
import type {
  ModePrecheckIn,
  ModePrecheckOut,
  ModeSummaryOut,
  ModeSwitchIn,
  ModeSwitchOut,
} from './types'

/**
 * 双模式状态 / 预检 / 切换（M2.10~2.11 · §9.3 / §9.6）。
 *
 * 红线：切换是"改单行 + epoch+1"，不改表；`epoch` 必须原样回传（乐观锁）。
 */
export const systemApi = {
  mode(): Promise<ModeSummaryOut> {
    return apiGet<ModeSummaryOut>('/system/mode')
  },

  precheck(body: ModePrecheckIn): Promise<ModePrecheckOut> {
    return apiPost<ModePrecheckOut>('/system/mode/precheck', body)
  },

  switchMode(body: ModeSwitchIn): Promise<ModeSwitchOut> {
    return apiPost<ModeSwitchOut>('/system/mode/switch', body)
  },
}
