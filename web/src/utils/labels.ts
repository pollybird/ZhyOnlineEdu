/**
 * 枚举值 → 面向用户的中文文案。
 *
 * 后端返回的状态码（`published` / `encoded` / `paid` 等）只用于程序判定，
 * 展示层统一走这里，避免把接口字段名直接暴露给学员与管理员。
 */

export function roleLabel(role: string): string {
  return (
    {
      super_admin: '超级管理员',
      admin: '管理员',
      teacher: '教师',
      student: '学员',
    }[role] ?? role
  )
}

export function modeNameLabel(mode: string): string {
  return { internal: '内部模式', external: '对外模式' }[mode] ?? mode
}

/** 课程可见范围（`EduCourse.mode`）。 */
export function courseModeLabel(mode: string): string {
  return { internal: '校内', external: '对外', both: '通用' }[mode] ?? mode
}

export function courseStatusLabel(status: string): string {
  return { published: '已上架', draft: '草稿', archived: '已下架' }[status] ?? status
}

export function assetStatusLabel(status: string | null | undefined): string {
  if (status === null || status === undefined || status === '') return '未上传'
  return (
    {
      pending: '待处理',
      uploaded: '待转码',
      transcoding: '转码中',
      encoded: '可播放',
      failed: '转码失败',
      deleted: '已删除',
    }[status] ?? '处理中'
  )
}

export function userStatusLabel(status: string): string {
  return { active: '正常', disabled: '已停用', expired: '已到期' }[status] ?? status
}

export function orderStatusLabel(status: string): string {
  return (
    {
      created: '待支付',
      paying: '支付中',
      paid: '已支付',
      failed: '支付失败',
      expired: '已关闭',
      cancelled: '已取消',
    }[status] ?? status
  )
}

export function subscriptionStatusLabel(status: string): string {
  return { active: '有效', expired: '已到期', revoked: '已撤销' }[status] ?? status
}

export function subscriptionSourceLabel(source: string): string {
  return { order: '购买', grant: '赠送' }[source] ?? source
}

export function questionKindLabel(kind: string): string {
  return { single: '单选', multiple: '多选', judge: '判断' }[kind] ?? kind
}

export function batchStatusLabel(status: string): string {
  return { completed: '已完成', partial: '部分成功', failed: '失败' }[status] ?? status
}

export function importConflictLabel(action: string): string {
  return { report_only: '仅报告，不覆盖', overwrite: '覆盖已有用户' }[action] ?? action
}

export function importRowActionLabel(action: string): string {
  return { created: '新建', updated: '更新', skipped: '跳过', invalid: '失败' }[action] ?? action
}
