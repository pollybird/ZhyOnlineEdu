import { describe, expect, it } from 'vitest'
import { hasPermission } from './auth'

describe('RBAC 权限判定（§7.3）', () => {
  it('权限码命中才返回 true', () => {
    expect(hasPermission(['course:read', 'course:write'], 'course:read')).toBe(true)
    expect(hasPermission(['course:read'], 'course:write')).toBe(false)
  })

  it('未登录/无权限列表一律 false', () => {
    expect(hasPermission(undefined, 'course:read')).toBe(false)
    expect(hasPermission([], 'course:read')).toBe(false)
  })

  it('progress:direct 不授予内置角色', () => {
    const teacherPermissions = ['course:read', 'course:write']
    expect(hasPermission(teacherPermissions, 'progress:direct')).toBe(false)
  })
})
