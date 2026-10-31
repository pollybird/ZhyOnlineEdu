import { describe, expect, it } from 'vitest'
import {
  assetStatusLabel,
  batchStatusLabel,
  courseModeLabel,
  courseStatusLabel,
  importConflictLabel,
  importRowActionLabel,
  modeNameLabel,
  orderStatusLabel,
  questionKindLabel,
  roleLabel,
  subscriptionSourceLabel,
  subscriptionStatusLabel,
  userStatusLabel,
} from './labels'

describe('枚举值 → 界面文案', () => {
  it('角色与模式中文化', () => {
    expect(roleLabel('super_admin')).toBe('超级管理员')
    expect(roleLabel('student')).toBe('学员')
    expect(modeNameLabel('internal')).toBe('内部模式')
    expect(courseModeLabel('both')).toBe('通用')
  })

  it('课程与资产状态中文化', () => {
    expect(courseStatusLabel('published')).toBe('已上架')
    expect(assetStatusLabel('encoded')).toBe('可播放')
    expect(assetStatusLabel(null)).toBe('未上传')
    expect(assetStatusLabel('unknown')).toBe('处理中')
  })

  it('用户 / 订单 / 订阅状态中文化', () => {
    expect(userStatusLabel('disabled')).toBe('已停用')
    expect(orderStatusLabel('paid')).toBe('已支付')
    expect(subscriptionStatusLabel('revoked')).toBe('已撤销')
    expect(subscriptionSourceLabel('grant')).toBe('赠送')
  })

  it('题库与导入批次文案中文化', () => {
    expect(questionKindLabel('multiple')).toBe('多选')
    expect(batchStatusLabel('partial')).toBe('部分成功')
    expect(importConflictLabel('report_only')).toBe('仅报告，不覆盖')
    expect(importRowActionLabel('created')).toBe('新建')
  })

  it('未知值原样返回，避免界面出现空白', () => {
    expect(roleLabel('guest')).toBe('guest')
    expect(courseStatusLabel('reviewing')).toBe('reviewing')
  })
})
