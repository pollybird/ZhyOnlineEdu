import { describe, expect, it } from 'vitest'
import type { SubjectOut } from '../api/types'
import { buildSubjectTree, siblingIds } from './catalog'

function subject(
  id: number,
  parentId: number | null,
  sortOrder: number,
  name = `学科${id}`,
): SubjectOut {
  return {
    id,
    parent_id: parentId,
    name,
    sort_order: sortOrder,
    is_enabled: true,
    version: 1,
    created_at: null,
    updated_at: null,
  }
}

describe('学科树构建（M2.1）', () => {
  it('按 parent_id 组装并按 sort_order 排序', () => {
    const tree = buildSubjectTree([
      subject(1, null, 1024, '数学'),
      subject(2, null, 2048, '语文'),
      subject(3, 1, 2048, '几何'),
      subject(4, 1, 1024, '代数'),
    ])
    expect(tree.map((node) => node.id)).toEqual([1, 2])
    expect(tree[0].children.map((node) => node.id)).toEqual([4, 3])
    expect(tree[0].children[0].name).toBe('代数')
  })

  it('孤儿节点按根处理，不丢节点', () => {
    const tree = buildSubjectTree([subject(1, 99, 1024), subject(2, null, 2048)])
    expect(tree.map((node) => node.id)).toEqual([1, 2])
  })
})

describe('同级顺序（用于上移/下移的 before_id）', () => {
  const items = [
    subject(1, null, 1024),
    subject(2, null, 2048),
    subject(3, 1, 1024),
    subject(4, 1, 2048),
    subject(5, 3, 1024),
  ]
  const tree = buildSubjectTree(items)

  it('根级返回根顺序', () => {
    expect(siblingIds(tree, null)).toEqual([1, 2])
  })

  it('按父节点返回子级顺序（支持深层）', () => {
    expect(siblingIds(tree, 1)).toEqual([3, 4])
    expect(siblingIds(tree, 3)).toEqual([5])
  })

  it('未知父节点返回空数组', () => {
    expect(siblingIds(tree, 404)).toEqual([])
  })
})
