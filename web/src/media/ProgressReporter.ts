/**
 * ProgressReporter —— 30s 心跳 + 页面隐藏兜底 + 乐观本地水位（§10.3.4 / §23.4 · M2.5）。
 *
 * 红线：
 * - 心跳单位 30s：`played_seconds` 按真实播放增量累加（向后/向前跳转不计）；
 * - `visibilitychange`（hidden）与 `pagehide` 必须触发一次兜底上报，避免移动端切后台丢进度；
 * - HTTP 202 即成功（网关只写流水），UI 显示「进度已保存」而不是转圈等待投影；
 * - 服务端仍是最终事实源，本地水位只用于**防止 UI 回跳**（`mergeResume`）。
 *
 * 浏览器 `sendBeacon` 无法携带 Authorization 头，因此兜底使用
 * `fetch(..., { keepalive: true })`（语义等价且能带 Bearer），由调用方提供。
 */

import { computePlayedDelta, progressBucket } from '../utils/progress'

export interface ProgressPayload {
  lesson_id: number
  position_sec: number
  played_seconds: number
  speed: number
  client_ts_ms: number
}

export interface ProgressReporterOptions {
  lessonId: number
  send: (payload: ProgressPayload, reason: 'heartbeat' | 'hide') => Promise<unknown>
  intervalMs?: number
  onSaved?: (position: number) => void
  onError?: (error: unknown) => void
  now?: () => number
}

export interface ProgressSource {
  currentTime: number
  duration: number
  paused: boolean
  playbackRate: number
}

const HEARTBEAT_MS = 30_000
const MAX_STEP_SEC = 5

export class ProgressReporter {
  private readonly lessonId: number
  private readonly send: ProgressReporterOptions['send']
  private readonly intervalMs: number
  private readonly onSaved: ProgressReporterOptions['onSaved']
  private readonly onError: ProgressReporterOptions['onError']
  private readonly now: () => number

  private source: ProgressSource | null = null
  private timer: ReturnType<typeof setInterval> | null = null
  private lastTickPosition = 0
  private pending = 0
  private lastSaved = 0
  private lastSentAt = 0
  private sending = false
  private disposed = false

  constructor(options: ProgressReporterOptions) {
    this.lessonId = options.lessonId
    this.send = options.send
    this.intervalMs = options.intervalMs ?? HEARTBEAT_MS
    this.onSaved = options.onSaved
    this.onError = options.onError
    this.now = options.now ?? (() => Date.now())
  }

  attach(source: ProgressSource, resume = 0): void {
    this.source = source
    this.lastTickPosition = Math.max(0, Math.floor(resume))
    this.lastSaved = this.lastTickPosition
    this.timer = setInterval(() => this.tick(), this.intervalMs)
    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', this.onVisibility)
    }
    if (typeof window !== 'undefined') {
      window.addEventListener('pagehide', this.onPageHide)
    }
  }

  detach(): void {
    this.disposed = true
    if (this.timer !== null) {
      clearInterval(this.timer)
      this.timer = null
    }
    if (typeof document !== 'undefined') {
      document.removeEventListener('visibilitychange', this.onVisibility)
    }
    if (typeof window !== 'undefined') {
      window.removeEventListener('pagehide', this.onPageHide)
    }
    this.source = null
  }

  /** 当前应展示的位置：本地已保存水位优先（防止 UI 回跳）。 */
  get displayPosition(): number {
    return Math.max(this.lastSaved, this.safeCurrent())
  }

  track(): void {
    if (this.source === null) return
    const current = this.safeCurrent()
    const delta = computePlayedDelta(this.lastTickPosition, current, MAX_STEP_SEC)
    if (!this.source.paused && delta > 0) {
      this.pending += delta
    }
    this.lastTickPosition = current
  }

  private safeCurrent(): number {
    if (this.source === null) return 0
    const value = this.source.currentTime
    if (!Number.isFinite(value) || value <= 0) return 0
    return Math.floor(value)
  }

  private onVisibility = (): void => {
    if (document.visibilityState === 'hidden') {
      void this.flush('hide')
    }
  }

  private onPageHide = (): void => {
    void this.flush('hide')
  }

  private tick(): void {
    this.track()
    if (this.pending < 1 && this.safeCurrent() === this.lastSaved) return
    void this.flush('heartbeat')
  }

  async flush(reason: 'heartbeat' | 'hide' = 'heartbeat'): Promise<void> {
    if (this.disposed || this.sending || this.source === null) return
    this.track()
    const position = Math.floor(this.safeCurrent())
    const played = Math.max(0, Math.min(3600, Math.floor(this.pending)))
    if (played <= 0 && position <= this.lastSaved) return
    this.sending = true
    try {
      await this.send(
        {
          lesson_id: this.lessonId,
          position_sec: position,
          played_seconds: played,
          speed: Math.max(0.5, Math.min(3, this.source.playbackRate || 1)),
          client_ts_ms: this.now(),
        },
        reason,
      )
      this.pending = Math.max(0, this.pending - played)
      this.lastSaved = Math.max(this.lastSaved, position)
      this.lastSentAt = this.now()
      this.onSaved?.(this.lastSaved)
    } catch (error) {
      this.onError?.(error)
    } finally {
      this.sending = false
    }
  }

  /** 供调试/埋点：当前 30s 桶与待上报秒数。 */
  snapshot(): { bucket: number; pending: number; saved: number } {
    return {
      bucket: progressBucket(this.safeCurrent()),
      pending: Math.floor(this.pending),
      saved: this.lastSaved,
    }
  }

  get lastSavedAt(): number {
    return this.lastSentAt
  }
}
