/**
 * 展示层纯函数（M2.14）：时长 / 百分比 / 相对时间 / 错误码 → 五态映射。
 *
 * 只放**无副作用**逻辑，便于 vitest（node 环境）覆盖；组件内不再重复实现。
 */

/** 五态：加载 / 空 / 错误 / 无权限 / 试看；ready 由页面自身渲染。 */
export type UiState = 'loading' | 'empty' | 'error' | 'forbidden' | 'preview'

export interface ErrorView {
  state: UiState
  code: string
  title: string
  message: string
  /** member：引导开通会员；retry：可重试；back：回退上一级 */
  action: 'retry' | 'member' | 'back' | 'none'
}

type ErrorAction = ErrorView['action']

/** 播放态错误码（与后端附录 A 对齐）。 */
const MEDIA_HINTS: Record<
  number,
  { state: UiState; title: string; message: string; action: ErrorAction }
> = {
  40201: {
    state: 'error',
    title: '播放凭证已过期',
    message: '已为你重新获取播放凭证，请重新点击播放',
    action: 'retry',
  },
  40202: {
    state: 'error',
    title: '资源尚未就绪',
    message: '视频仍在转码或校验中，请稍后再试',
    action: 'retry',
  },
  40203: {
    state: 'error',
    title: '播放链接无效',
    message: '请从课程详情重新进入播放',
    action: 'back',
  },
  40204: {
    state: 'forbidden',
    title: '授权已失效',
    message: '会员到期或已退班，请重新获取授权',
    action: 'member',
  },
  40205: {
    state: 'error',
    title: '播放无法恢复',
    message: '媒体文件异常，请稍后重试或联系管理员',
    action: 'retry',
  },
  40206: {
    state: 'error',
    title: '播放凭证已失效',
    message: '请重新进入播放',
    action: 'back',
  },
}

export function errorCodeOf(error: unknown): string {
  if (typeof error === 'object' && error !== null && 'code' in error) {
    return String((error as { code: unknown }).code)
  }
  return 'NETWORK_ERROR'
}

export function describeError(error: unknown): ErrorView {
  const code = errorCodeOf(error)
  if (code === 'NETWORK_ERROR') {
    return {
      state: 'error',
      code,
      title: '网络连接失败',
      message: '请检查网络后重试；离线时已保存的进度不会丢失',
      action: 'retry',
    }
  }
  if (code === 'ACCESS_LEVEL_REQUIRED') {
    return {
      state: 'forbidden',
      code,
      title: '需要开通会员',
      message: '该课程为会员课程，开通后即可播放与测验',
      action: 'member',
    }
  }
  if (code === 'ACCESS_CLASS_REQUIRED') {
    return {
      state: 'forbidden',
      code,
      title: '需要加入班级',
      message: '该课程仅限班级成员学习，请联系老师加入班级',
      action: 'back',
    }
  }
  if (code === 'NOT_FOUND') {
    return {
      state: 'empty',
      code,
      title: '内容不存在',
      message: '课程可能已下架或删除，请返回课程列表',
      action: 'back',
    }
  }
  if (code === 'PERMISSION_DENIED') {
    return {
      state: 'forbidden',
      code,
      title: '无访问权限',
      message: '当前账号没有该操作权限',
      action: 'back',
    }
  }
  const numeric = Number(code)
  if (Number.isInteger(numeric) && MEDIA_HINTS[numeric] !== undefined) {
    return { code, ...MEDIA_HINTS[numeric] }
  }
  return {
    state: 'error',
    code,
    title: '请求失败',
    message: '请稍后重试；若持续失败请联系管理员',
    action: 'retry',
  }
}

/** 秒 → `mm:ss` / `h:mm:ss`；非法值按 0。 */
export function formatDuration(seconds: number): string {
  const total = Number.isFinite(seconds) ? Math.max(0, Math.floor(seconds)) : 0
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const rest = total % 60
  const pad = (value: number): string => String(value).padStart(2, '0')
  return hours > 0 ? `${hours}:${pad(minutes)}:${pad(rest)}` : `${minutes}:${pad(rest)}`
}

/** 完成率：0~100，保留 1 位小数并去掉无意义的 `.0`。 */
export function formatPercent(value: number): string {
  const bounded = Number.isFinite(value) ? Math.min(100, Math.max(0, value)) : 0
  const rounded = Math.round(bounded * 10) / 10
  return Number.isInteger(rounded) ? `${rounded}%` : `${rounded.toFixed(1)}%`
}

/** 相对时间（`刚刚` / `X 分钟前` / `X 小时前` / `X 天前`）。 */
export function formatRelative(iso: string | null, now: Date = new Date()): string {
  if (iso === null || iso === '') return '—'
  const moment = new Date(iso)
  if (Number.isNaN(moment.getTime())) return '—'
  const seconds = Math.max(0, Math.floor((now.getTime() - moment.getTime()) / 1000))
  if (seconds < 60) return '刚刚'
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes} 分钟前`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} 小时前`
  return `${Math.floor(hours / 24)} 天前`
}

/** 投影滞后 → `已是最新` / `更新于 X 分钟前`（课程档案的 freshness 文案）。 */
export function freshnessLabel(seconds: number): string {
  const value = Number.isFinite(seconds) ? Math.max(0, Math.floor(seconds)) : 0
  if (value < 5) return '已是最新'
  if (value < 60) return `更新于 ${value} 秒前`
  return `更新于 ${Math.floor(value / 60)} 分钟前`
}

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  let value = bytes
  let index = 0
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024
    index += 1
  }
  return `${index === 0 ? value : Math.round(value * 10) / 10} ${units[index]}`
}

/** 续播位置：本地乐观水位胜过服务端旧值，避免 UI 回跳（F2 客户端安全网）。 */
export function mergeResume(server: number, local: number): number {
  const safe = (value: number): number => (Number.isFinite(value) ? Math.max(0, Math.floor(value)) : 0)
  return Math.max(safe(server), safe(local))
}

/** `access_type` → 标签文案。 */
export function accessLabel(accessType: string): string {
  return { public: '公开课', member: '会员课', class: '班级课' }[accessType] ?? accessType
}
