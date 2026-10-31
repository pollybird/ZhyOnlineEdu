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
 * 口径：`blocked` 项即拒绝切换；`warn` 项只提示（已签发的播放凭证继续有效、试看记录不计入学习统计）。
 */
export function modeLabel(mode: string): string {
  return mode === 'external' ? '对外模式' : '内部模式'
}

export function modeHint(mode: string): string {
  return mode === 'external'
    ? '面向公众售卖：仅公开课与会员课开放，班级授权不启用'
    : '面向校内教学：公开课、会员课与班级授权全部生效'
}

/** 预检检查项 → 面向管理员的业务名称。 */
export function checkLabel(code: string): string {
  return (
    {
      mode_supported: '目标模式可用性',
      transcode_idle: '转码任务',
      quiz_idle: '进行中的作答',
      course_visibility: '课程可见性',
      active_tickets: '有效播放凭证',
      preview_pollution: '试看记录',
      compat_flags: '系统环境一致性',
      self_registered: '自助注册账号',
    }[code] ?? '其他检查项'
  )
}

/** 预检结果 → 面向管理员的判定文案。 */
export function checkStatusLabel(status: string): string {
  return { ok: '通过', warn: '提醒', blocked: '阻断' }[status] ?? '待确认'
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
    `已从${modeLabel(result.previous_mode ?? 'internal')}切换到${modeLabel(result.mode)}`,
  ]
  if (result.counts.quarantined_logs > 0) {
    parts.push(`隔离 ${result.counts.quarantined_logs} 条试看记录（不计入学习统计）`)
  }
  if (result.counts.affected_users > 0) {
    parts.push(`停用 ${result.counts.affected_users} 个自助注册账号（可批量恢复）`)
  }
  if (result.counts.active_tickets > 0) {
    parts.push(`${result.counts.active_tickets} 个已签发的播放凭证继续有效`)
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
