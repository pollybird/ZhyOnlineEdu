import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { catalogApi } from '../api/catalog'
import type {
  CourseDetailOut,
  CourseListOut,
  CourseOut,
  CourseUpdateIn,
  SubjectOut,
} from '../api/types'

export interface SubjectNode extends SubjectOut {
  children: SubjectNode[]
}

/** 平铺列表 → 树（纯函数，便于单测）：孤儿节点按根处理，避免脏数据导致丢节点。 */
export function buildSubjectTree(items: readonly SubjectOut[]): SubjectNode[] {
  const nodes = new Map<number, SubjectNode>()
  for (const item of items) {
    nodes.set(item.id, { ...item, children: [] })
  }
  const roots: SubjectNode[] = []
  for (const node of nodes.values()) {
    const parent = node.parent_id === null ? undefined : nodes.get(node.parent_id)
    if (parent === undefined) {
      roots.push(node)
    } else {
      parent.children.push(node)
    }
  }
  const sort = (list: SubjectNode[]): void => {
    list.sort((left, right) => left.sort_order - right.sort_order || left.id - right.id)
    list.forEach((node) => sort(node.children))
  }
  sort(roots)
  return roots
}

/** 取某父节点下的同级顺序（根节点 parent_id 为 null）；用于上移/下移计算 before_id。 */
export function siblingIds(tree: readonly SubjectNode[], parentId: number | null): number[] {
  const collect = (list: readonly SubjectNode[]): SubjectNode[] => {
    for (const node of list) {
      if (node.id === parentId) {
        return node.children
      }
      const found = collect(node.children)
      if (found.length > 0) {
        return found
      }
    }
    return []
  }
  if (parentId === null) {
    return tree.map((node) => node.id)
  }
  return collect(tree).map((node) => node.id)
}

/** 目录 / 课程管理状态（M2.1，验收：单元 + UI）。 */
export const useCatalogStore = defineStore('catalog', () => {
  const subjects = ref<SubjectOut[]>([])
  const tree = computed<SubjectNode[]>(() => buildSubjectTree(subjects.value))
  const selectedSubjectId = ref<number | null>(null)

  const courses = ref<CourseOut[]>([])
  const total = ref(0)
  const page = ref(1)
  const pageSize = ref(20)
  const keyword = ref('')

  const detail = ref<CourseDetailOut | null>(null)
  const loading = ref(false)

  async function loadSubjects(): Promise<void> {
    subjects.value = (await catalogApi.subjects()).items
  }

  async function loadCourses(): Promise<void> {
    const result: CourseListOut = await catalogApi.courses({
      subject_id: selectedSubjectId.value ?? undefined,
      keyword: keyword.value.trim() === '' ? undefined : keyword.value.trim(),
      page: page.value,
      page_size: pageSize.value,
    })
    courses.value = result.items
    total.value = result.total
  }

  async function selectSubject(id: number | null): Promise<void> {
    selectedSubjectId.value = id
    page.value = 1
    await loadCourses()
  }

  async function createSubject(name: string, parentId: number | null): Promise<SubjectOut> {
    const created = await catalogApi.createSubject({
      name,
      parent_id: parentId ?? undefined,
    })
    await loadSubjects()
    return created
  }

  async function renameSubject(node: SubjectOut, name: string): Promise<void> {
    await catalogApi.updateSubject(node.id, { version: node.version, name })
    await loadSubjects()
  }

  async function deleteSubject(node: SubjectOut): Promise<void> {
    await catalogApi.deleteSubject(node.id, node.version)
    if (selectedSubjectId.value === node.id) {
      selectedSubjectId.value = null
      await loadCourses()
    }
    await loadSubjects()
  }

  /** 同级上移/下移：只需要一个 before_id，服务端按 1024 步进重排。 */
  async function moveSubject(node: SubjectOut, direction: -1 | 1): Promise<void> {
    const siblings = siblingIds(tree.value, node.parent_id)
    const index = siblings.indexOf(node.id)
    const target = index + direction
    if (index < 0 || target < 0 || target >= siblings.length) {
      return
    }
    const beforeId = direction < 0 ? siblings[target] : siblings[target + 1]
    await catalogApi.updateSubject(node.id, {
      version: node.version,
      before_id: beforeId === undefined ? undefined : beforeId,
    })
    await loadSubjects()
  }

  async function toggleSubject(node: SubjectOut, enabled: boolean): Promise<void> {
    await catalogApi.updateSubject(node.id, { version: node.version, is_enabled: enabled })
    await loadSubjects()
  }

  async function loadDetail(courseId: number): Promise<void> {
    detail.value = await catalogApi.course(courseId)
  }

  async function createCourse(title: string, subjectId: number): Promise<CourseOut> {
    const created = await catalogApi.createCourse({ subject_id: subjectId, title })
    await loadCourses()
    return created
  }

  async function saveCourse(
    course: CourseOut,
    patch: Omit<CourseUpdateIn, 'version'>,
  ): Promise<void> {
    detail.value = {
      ...(detail.value as CourseDetailOut),
      ...(await catalogApi.updateCourse(course.id, { version: course.version, ...patch })),
    }
  }

  async function addChapter(courseId: number, title: string): Promise<void> {
    await catalogApi.createChapter(courseId, title)
    await loadDetail(courseId)
  }

  async function deleteChapter(chapterId: number, version: number): Promise<void> {
    if (detail.value === null) return
    await catalogApi.deleteChapter(chapterId, version)
    await loadDetail(detail.value.id)
  }

  async function moveChapter(courseId: number, chapterId: number, direction: -1 | 1): Promise<void> {
    if (detail.value === null) return
    const ids = detail.value.chapters.map((item) => item.id)
    const index = ids.indexOf(chapterId)
    const target = index + direction
    if (index < 0 || target < 0 || target >= ids.length) return
    const next = [...ids]
    const [moved] = next.splice(index, 1)
    next.splice(target, 0, moved)
    await catalogApi.reorderChapters(courseId, detail.value.version, next)
    await loadDetail(courseId)
  }

  async function addLesson(chapterId: number, title: string): Promise<void> {
    if (detail.value === null) return
    await catalogApi.createLesson(chapterId, { title })
    await loadDetail(detail.value.id)
  }

  async function deleteLesson(lessonId: number, version: number): Promise<void> {
    if (detail.value === null) return
    await catalogApi.deleteLesson(lessonId, version)
    await loadDetail(detail.value.id)
  }

  async function moveLesson(
    chapterId: number,
    lessonId: number,
    direction: -1 | 1,
  ): Promise<void> {
    if (detail.value === null) return
    const chapter = detail.value.chapters.find((item) => item.id === chapterId)
    if (chapter === undefined) return
    const ids = chapter.lessons.map((item) => item.id)
    const index = ids.indexOf(lessonId)
    const target = index + direction
    if (index < 0 || target < 0 || target >= ids.length) return
    const next = [...ids]
    const [moved] = next.splice(index, 1)
    next.splice(target, 0, moved)
    await catalogApi.reorderLessons(chapterId, chapter.version, next)
    await loadDetail(detail.value.id)
  }

  async function publish(course: CourseOut | CourseDetailOut): Promise<void> {
    await catalogApi.publishCourse(course.id, course.version)
    await loadDetail(course.id)
    await loadCourses()
  }

  async function unpublish(course: CourseOut | CourseDetailOut): Promise<void> {
    await catalogApi.unpublishCourse(course.id, course.version)
    await loadDetail(course.id)
    await loadCourses()
  }

  async function removeCourse(course: CourseOut | CourseDetailOut): Promise<void> {
    await catalogApi.deleteCourse(course.id, course.version)
    detail.value = null
    await loadCourses()
  }

  return {
    subjects,
    tree,
    selectedSubjectId,
    courses,
    total,
    page,
    pageSize,
    keyword,
    detail,
    loading,
    loadSubjects,
    loadCourses,
    selectSubject,
    createSubject,
    renameSubject,
    deleteSubject,
    moveSubject,
    toggleSubject,
    loadDetail,
    createCourse,
    saveCourse,
    addChapter,
    deleteChapter,
    moveChapter,
    addLesson,
    deleteLesson,
    moveLesson,
    publish,
    unpublish,
    removeCourse,
  }
})
