import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { systemApi } from '../api/system'
import type {
  ModeCheckOut,
  ModePrecheckOut,
  ModeSummaryOut,
  ModeSwitchOut,
} from '../api/types'

export type ModeName = 'internal' | 'external'

/**
 * 切换向导纯逻辑（供 vitest 直接断言，不触碰网络）。
 *
 * 口径：`blocked` 项即拒绝切换；`warn` 项只提示（存量票据不失效、preview 流水会被隔离）。
 */
export function modeLabel(mode: string): string {
  return mode === 'external' ? '对外模式' : '内部模式'
}

export function modeHint(mode: string): string {
  return mode === 'external'
    ? '面向公众售卖：仅 public / member 授权生效，班级授权不启用'
    : '面向校内教学：public / member / class 三种授权全部生效'
}

export function otherMode(mode: string): ModeName {
  return mode === 'external' ? 'internal' : 'external'
}

/** 预检是否可切换：必须 ready 且没有 blocked 项（两条都查，防后端字段漂移）。 */
export function canSwitch(result: ModePrecheckOut | null): boolean {
  return result !== null && result.ready && result.blockers.length === 0
}

export interface CheckSummary {
  ok: number
  warn: number
  blocked: number
}

export function summarizeChecks(checks: readonly ModeCheckOut[]): CheckSummary {
  const summary: CheckSummary = { ok: 0, warn: 0, blocked: 0 }
  for (const item of checks) {
    if (item.status === 'blocked') {
      summary.blocked += 1
    } else if (item.status === 'warn') {
      summary.warn += 1
    } else {
      summary.ok += 1
    }
  }
  return summary
}

/** epoch 乐观锁：页面上的 epoch 已过期（别人先切了），需要重新拉状态。 */
export function isEpochStale(expected: number, current: number): boolean {
  return expected !== current
}

/** 切换报告摘要：切换向导确认页与审计回执共用。 */
export function describeSwitchReport(result: ModeSwitchOut): string {
  const parts = [
    `${modeLabel(result.previous_mode ?? 'internal')} → ${modeLabel(result.mode)}（epoch ${result.epoch}）`,
  ]
  if (result.counts.quarantined_logs > 0) {
    parts.push(`隔离 preview 流水 ${result.counts.quarantined_logs} 条`)
  }
  if (result.counts.affected_users > 0) {
    const batch = result.migration_batch_id ?? '-'
    parts.push(`停用自注册账号 ${result.counts.affected_users} 个（批次 ${batch}）`)
  }
  if (result.counts.active_tickets > 0) {
    parts.push(`存量票据 ${result.counts.active_tickets} 个不失效`)
  }
  return parts.join('；')
}

export const useSystemStore = defineStore('system', () => {
  const summary = ref<ModeSummaryOut | null>(null)
  const precheck = ref<ModePrecheckOut | null>(null)
  const lastReport = ref<ModeSwitchOut | null>(null)

  const current = computed(() => summary.value?.mode ?? null)
  const target = computed<ModeName>(() =>
    summary.value === null ? 'external' : otherMode(summary.value.mode),
  )
  const switchable = computed(() => canSwitch(precheck.value))

  async function load(): Promise<ModeSummaryOut> {
    summary.value = await systemApi.mode()
    return summary.value
  }

  async function check(next: ModeName): Promise<ModePrecheckOut> {
    precheck.value = await systemApi.precheck({ target_mode: next })
    return precheck.value
  }

  async function switchTo(next: ModeName, reason: string): Promise<ModeSwitchOut> {
    if (summary.value === null) {
      await load()
    }
    const epoch = summary.value?.epoch ?? 1
    const result = await systemApi.switchMode({
      target_mode: next,
      epoch,
      reason: reason.trim() === '' ? null : reason.trim(),
    })
    lastReport.value = result
    const current = summary.value
    if (current !== null) {
      summary.value = {
        ...current,
        mode: result.mode,
        epoch: result.epoch,
        previous_mode: result.previous_mode,
        switched_at: result.switched_at,
        switched_by: result.switched_by,
        migration_batch_id: result.migration_batch_id,
        version: result.version,
        counts: result.counts,
        other_mode: otherMode(result.mode),
      }
    }
    precheck.value = null
    return result
  }

  function reset(): void {
    summary.value = null
    precheck.value = null
    lastReport.value = null
  }

  return { summary, precheck, lastReport, current, target, switchable, load, check, switchTo, reset }
})
