import { describe, expect, it } from 'vitest'
import type { ModeCheckOut, ModeCountsOut, ModePrecheckOut, ModeSwitchOut } from '../api/types'
import {
  canSwitch,
  describeSwitchReport,
  isEpochStale,
  modeHint,
  modeLabel,
  otherMode,
  summarizeChecks,
} from './system'

function counts(overrides: Partial<ModeCountsOut> = {}): ModeCountsOut {
  return {
    active_tickets: 0,
    preview_logs: 0,
    quarantined_logs: 0,
    affected_users: 0,
    revoked_sessions: 0,
    quiz_in_progress: 0,
    transcode_active: 0,
    courses_hidden: 0,
    ...overrides,
  }
}

function check(code: string, status: ModeCheckOut['status'], count: number | null = null): ModeCheckOut {
  return { code, status, message: `${code}:${status}`, count }
}

function precheck(overrides: Partial<ModePrecheckOut> = {}): ModePrecheckOut {
  return {
    target_mode: 'external',
    current_mode: 'internal',
    epoch: 3,
    ready: true,
    checks: [check('mode_supported', 'ok')],
    blockers: [],
    warnings: [],
    counts: counts(),
    ...overrides,
  }
}

function report(overrides: Partial<ModeSwitchOut> = {}): ModeSwitchOut {
  return {
    mode: 'external',
    epoch: 4,
    previous_mode: 'internal',
    switched_at: '2026-10-30T00:00:00Z',
    switched_by: 1,
    migration_batch_id: null,
    version: 1,
    counts: counts(),
    reason: null,
    warnings: [],
    checks: [],
    ...overrides,
  }
}

describe('模式文案（M2.10~2.11）', () => {
  it('internal / external 映射为内部 / 对外模式', () => {
    expect(modeLabel('internal')).toBe('内部模式')
    expect(modeLabel('external')).toBe('对外模式')
    // 未知值兜底为内部模式，避免界面出现空标签
    expect(modeLabel('')).toBe('内部模式')
  })

  it('提示语覆盖授权差异（班级授权仅内部模式生效）', () => {
    expect(modeHint('internal')).toContain('class')
    expect(modeHint('external')).toContain('班级授权不启用')
  })

  it('另一个模式为当前模式的互补值', () => {
    expect(otherMode('internal')).toBe('external')
    expect(otherMode('external')).toBe('internal')
  })
})

describe('预检判定（blocked 即禁止切换）', () => {
  it('未预检视为不可切换', () => {
    expect(canSwitch(null)).toBe(false)
  })

  it('ready 且无 blockers 才可切换', () => {
    expect(canSwitch(precheck())).toBe(true)
  })

  it('ready=false 或存在 blockers 均禁止切换（防字段漂移）', () => {
    expect(canSwitch(precheck({ ready: false }))).toBe(false)
    expect(canSwitch(precheck({ blockers: ['compat_flags'] }))).toBe(false)
  })

  it('汇总检查项状态计数', () => {
    const summary = summarizeChecks([
      check('mode_supported', 'ok'),
      check('transcode_idle', 'warn'),
      check('active_tickets', 'warn'),
      check('compat_flags', 'blocked'),
    ])
    expect(summary).toEqual({ ok: 1, warn: 2, blocked: 1 })
    expect(summarizeChecks([])).toEqual({ ok: 0, warn: 0, blocked: 0 })
  })
})

describe('epoch 乐观锁', () => {
  it('epoch 一致才允许提交切换', () => {
    expect(isEpochStale(3, 3)).toBe(false)
    expect(isEpochStale(3, 4)).toBe(true)
    expect(isEpochStale(1, 4)).toBe(true)
  })
})

describe('切换报告摘要', () => {
  it('基础摘要含模式与 epoch', () => {
    expect(describeSwitchReport(report())).toBe('内部模式 → 对外模式（epoch 4）')
  })

  it('隔离 preview 流水 / 停用自注册账号 / 存量票据逐项列出', () => {
    const summary = describeSwitchReport(
      report({
        counts: counts({ quarantined_logs: 7, affected_users: 2, active_tickets: 5 }),
        migration_batch_id: 'FLP20261030000000ab12cd34',
      }),
    )
    expect(summary).toContain('隔离 preview 流水 7 条')
    expect(summary).toContain('停用自注册账号 2 个（批次 FLP20261030000000ab12cd34）')
    expect(summary).toContain('存量票据 5 个不失效')
  })

  it('计数为 0 时省略对应片段', () => {
    const summary = describeSwitchReport(report({ previous_mode: null }))
    expect(summary).not.toContain('隔离')
    expect(summary).not.toContain('停用')
    expect(summary).not.toContain('存量票据')
  })
})
