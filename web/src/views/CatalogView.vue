<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ApiError } from '../api/request'
import type { CourseOut, SubjectOut } from '../api/types'
import { useCatalogStore, type SubjectNode } from '../stores/catalog'

/**
 * 学科树 + 课程三层管理页（M2.1）。
 * - 学科：新增/重命名/启停/上移下移/删除（有子节点或课程时后端拒绝）；
 * - 课程：列表/新建/编辑/上架（asset 就绪校验）/下架/删除；
 * - 章与课时：增删、上下移（重排接口），冲突统一提示"列表已更新"。
 */
const store = useCatalogStore()

const rootName = ref('')
const courseFilter = ref('')
const publishError = ref<{ reason: string; not_ready: { lesson_id: number; title: string }[] } | null>(
  null,
)
const newCourse = reactive({ title: '', subjectId: null as number | null })
const newCourseVisible = ref(false)
const chapterTitle = ref('')
const lessonForm = reactive({ chapterId: 0, title: '', duration: 9 })

const detail = computed(() => store.detail)
const subjectOptions = computed(() =>
  store.subjects.map((item) => ({
    id: item.id,
    label: item.name,
    disabled: !item.is_enabled,
  })),
)

function notifyError(error: unknown): void {
  if (error instanceof ApiError) {
    ElMessage.error(error.message)
    return
  }
  ElMessage.error('请求失败，请查看后端日志')
}

async function guard(task: () => Promise<void>): Promise<void> {
  try {
    await task()
  } catch (error) {
    notifyError(error)
  }
}

async function addRoot(): Promise<void> {
  const name = rootName.value.trim()
  if (name === '') return
  await guard(async () => {
    await store.createSubject(name, null)
    rootName.value = ''
    ElMessage.success('已新增根学科')
  })
}

async function addChild(node: SubjectNode): Promise<void> {
  try {
    const { value } = await ElMessageBox.prompt(`在「${node.name}」下新增子学科`, '新增学科', {
      inputPattern: /\S+/,
      inputErrorMessage: '名称不能为空',
    })
    await store.createSubject(value.trim(), node.id)
    ElMessage.success('已新增子学科')
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') notifyError(error)
  }
}

async function rename(node: SubjectOut): Promise<void> {
  try {
    const { value } = await ElMessageBox.prompt('修改学科名称', '重命名', {
      inputValue: node.name,
      inputPattern: /\S+/,
      inputErrorMessage: '名称不能为空',
    })
    await store.renameSubject(node, value.trim())
    ElMessage.success('已保存')
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') notifyError(error)
  }
}

async function removeSubject(node: SubjectOut): Promise<void> {
  try {
    await ElMessageBox.confirm(`确认删除学科「${node.name}」？`, '删除确认', { type: 'warning' })
  } catch {
    return
  }
  await guard(async () => {
    await store.deleteSubject(node)
    ElMessage.success('已删除')
  })
}

async function moveSubject(node: SubjectOut, direction: -1 | 1): Promise<void> {
  await guard(() => store.moveSubject(node, direction))
}

async function toggleSubject(node: SubjectOut, enabled: boolean): Promise<void> {
  await guard(() => store.toggleSubject(node, enabled))
}

async function openNewCourse(): Promise<void> {
  newCourse.title = ''
  newCourse.subjectId = store.selectedSubjectId
  newCourseVisible.value = true
}

async function createCourse(): Promise<void> {
  if (newCourse.title.trim() === '' || newCourse.subjectId === null) {
    ElMessage.warning('请填写课程标题并选择学科')
    return
  }
  await guard(async () => {
    await store.createCourse(newCourse.title.trim(), newCourse.subjectId as number)
    newCourseVisible.value = false
    ElMessage.success('已创建课程（草稿）')
  })
}

async function openDetail(course: CourseOut): Promise<void> {
  publishError.value = null
  await guard(() => store.loadDetail(course.id))
}

async function publish(row: CourseOut): Promise<void> {
  publishError.value = null
  try {
    await store.publish(row)
    ElMessage.success('课程已上架')
  } catch (error) {
    if (error instanceof ApiError && error.code === 'COURSE_ASSET_NOT_READY') {
      const data = error.data as { reason?: string; not_ready?: { lesson_id: number; title: string }[] }
      publishError.value = {
        reason: data.reason ?? 'asset_not_ready',
        not_ready: data.not_ready ?? [],
      }
      ElMessage.error('存在未就绪课时，禁止上架')
      return
    }
    notifyError(error)
  }
}

async function unpublish(row: CourseOut): Promise<void> {
  await guard(async () => {
    await store.unpublish(row)
    ElMessage.success('课程已下架')
  })
}

async function removeCourse(row: CourseOut): Promise<void> {
  try {
    await ElMessageBox.confirm(`确认删除课程「${row.title}」？`, '删除确认', { type: 'warning' })
  } catch {
    return
  }
  await guard(async () => {
    await store.removeCourse(row)
    ElMessage.success('已删除')
  })
}

async function addChapter(): Promise<void> {
  const title = chapterTitle.value.trim()
  if (title === '' || detail.value === null) return
  await guard(async () => {
    await store.addChapter(detail.value?.id as number, title)
    chapterTitle.value = ''
    ElMessage.success('已新增章')
  })
}

async function startLesson(chapterId: number): Promise<void> {
  lessonForm.chapterId = chapterId
  lessonForm.title = ''
  lessonForm.duration = 9
}

async function addLesson(): Promise<void> {
  if (lessonForm.title.trim() === '') return
  await guard(async () => {
    await store.addLesson(lessonForm.chapterId, lessonForm.title.trim(), lessonForm.duration * 60)
    lessonForm.chapterId = 0
    ElMessage.success('已新增课时（尚未挂载资产）')
  })
}

async function moveChapter(chapterId: number, direction: -1 | 1): Promise<void> {
  if (detail.value === null) return
  await guard(() => store.moveChapter(detail.value?.id as number, chapterId, direction))
}

async function moveLesson(chapterId: number, lessonId: number, direction: -1 | 1): Promise<void> {
  await guard(() => store.moveLesson(chapterId, lessonId, direction))
}

async function deleteChapter(chapterId: number, version: number): Promise<void> {
  try {
    await ElMessageBox.confirm('确认删除该章？（章下必须无课时）', '删除确认', {
      type: 'warning',
    })
  } catch {
    return
  }
  await guard(() => store.deleteChapter(chapterId, version))
}

async function deleteLesson(lessonId: number, version: number): Promise<void> {
  try {
    await ElMessageBox.confirm('确认删除该课时？', '删除确认', { type: 'warning' })
  } catch {
    return
  }
  await guard(() => store.deleteLesson(lessonId, version))
}

function filterCourses(): void {
  store.keyword = courseFilter.value
  void guard(() => store.loadCourses())
}

onMounted(async () => {
  await guard(async () => {
    await store.loadSubjects()
    await store.loadCourses()
  })
})
</script>

<template>
  <div class="catalog">
    <el-row :gutter="16">
      <el-col :xs="24" :md="7">
        <el-card class="catalog-card">
          <template #header>
            <div class="catalog-head">
              <span>学科树</span>
              <el-tag size="small" type="info">最大深度 5</el-tag>
            </div>
          </template>

          <div class="catalog-row">
            <el-input
              v-model="rootName"
              placeholder="新增根学科名称"
              @keyup.enter="addRoot"
            />
            <el-button type="primary" @click="addRoot">新增</el-button>
          </div>

          <el-tree
            :data="store.tree"
            node-key="id"
            :props="{ label: 'name', children: 'children' }"
            default-expand-all
            highlight-current
            @node-click="(node: SubjectNode) => store.selectSubject(node.id)"
          >
            <template #default="{ data }">
              <div class="catalog-node">
                <span :class="{ 'catalog-node-disabled': !data.is_enabled }">
                  {{ data.name }}
                  <el-tag v-if="!data.is_enabled" size="small" type="danger">停用</el-tag>
                </span>
                <span class="catalog-node-actions" @click.stop>
                  <el-button link size="small" @click="addChild(data)">子级</el-button>
                  <el-button link size="small" @click="rename(data)">改名</el-button>
                  <el-button link size="small" @click="moveSubject(data, -1)">↑</el-button>
                  <el-button link size="small" @click="moveSubject(data, 1)">↓</el-button>
                  <el-button
                    link
                    size="small"
                    @click="toggleSubject(data, !data.is_enabled)"
                  >
                    {{ data.is_enabled ? '停用' : '启用' }}
                  </el-button>
                  <el-button link size="small" type="danger" @click="removeSubject(data)">
                    删除
                  </el-button>
                </span>
              </div>
            </template>
          </el-tree>
          <el-button class="catalog-reset" link @click="store.selectSubject(null)">
            查看全部课程
          </el-button>
        </el-card>
      </el-col>

      <el-col :xs="24" :md="17">
        <el-card class="catalog-card">
          <template #header>
            <div class="catalog-head">
              <span>课程（三层：课程 / 章 / 课时）</span>
              <el-button type="primary" @click="openNewCourse">新建课程</el-button>
            </div>
          </template>

          <div class="catalog-row">
            <el-input
              v-model="courseFilter"
              placeholder="按标题搜索"
              clearable
              @keyup.enter="filterCourses"
            />
            <el-button @click="filterCourses">搜索</el-button>
          </div>

          <el-alert
            v-if="publishError"
            type="error"
            show-icon
            :closable="true"
            class="catalog-alert"
            :title="
              publishError.reason === 'no_lessons'
                ? '课程暂无课时，禁止上架'
                : '存在未就绪课时（需 asset_status=encoded）'
            "
            @close="publishError = null"
          >
            <ul class="catalog-list">
              <li v-for="item in publishError.not_ready" :key="item.lesson_id">
                #{{ item.lesson_id }} {{ item.title }}
              </li>
            </ul>
          </el-alert>

          <el-table :data="store.courses" size="small" row-key="id">
            <el-table-column prop="title" label="课程" min-width="160" />
            <el-table-column prop="subject_name" label="学科" width="110" />
            <el-table-column prop="mode" label="模式" width="90" />
            <el-table-column prop="access_type" label="授权" width="90" />
            <el-table-column label="状态" width="90">
              <template #default="{ row }">
                <el-tag size="small" :type="row.status === 'published' ? 'success' : 'info'">
                  {{ row.status }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="章/课时" width="90">
              <template #default="{ row }">{{ row.chapter_count }} / {{ row.lesson_count }}</template>
            </el-table-column>
            <el-table-column label="操作" width="240">
              <template #default="{ row }">
                <el-button link size="small" @click="openDetail(row)">编辑</el-button>
                <el-button
                  v-if="row.status !== 'published'"
                  link
                  size="small"
                  type="success"
                  @click="publish(row)"
                >
                  上架
                </el-button>
                <el-button v-else link size="small" type="warning" @click="unpublish(row)">
                  下架
                </el-button>
                <el-button link size="small" type="danger" @click="removeCourse(row)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
          <p class="catalog-hint">
            共 {{ store.total }} 门课程；上架前置：学科启用 + 至少 1 课时 + 全部课时资产 encoded。
          </p>
        </el-card>
      </el-col>
    </el-row>

    <el-dialog v-model="newCourseVisible" title="新建课程" width="420px">
      <el-form label-width="72px">
        <el-form-item label="学科">
          <el-select v-model="newCourse.subjectId" placeholder="选择学科" class="catalog-full">
            <el-option
              v-for="item in subjectOptions"
              :key="item.id"
              :label="item.label"
              :value="item.id"
              :disabled="item.disabled"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="标题">
          <el-input v-model="newCourse.title" placeholder="课程标题" @keyup.enter="createCourse" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="newCourseVisible = false">取消</el-button>
        <el-button type="primary" @click="createCourse">创建</el-button>
      </template>
    </el-dialog>

    <el-drawer
      :model-value="detail !== null"
      :title="detail?.title ?? '课程详情'"
      size="60%"
      @close="store.detail = null"
    >
      <template v-if="detail">
        <el-descriptions :column="2" border class="catalog-desc">
          <el-descriptions-item label="学科">{{ detail.subject_name }}</el-descriptions-item>
          <el-descriptions-item label="状态">{{ detail.status }}</el-descriptions-item>
          <el-descriptions-item label="模式">{{ detail.mode }}</el-descriptions-item>
          <el-descriptions-item label="授权">{{ detail.access_type }}</el-descriptions-item>
          <el-descriptions-item label="版本">v{{ detail.version }}</el-descriptions-item>
          <el-descriptions-item label="上架时间">{{ detail.published_at ?? '—' }}</el-descriptions-item>
        </el-descriptions>

        <div class="catalog-row catalog-row-top">
          <el-button
            v-if="detail.status !== 'published'"
            type="success"
            @click="publish(detail)"
          >
            上架
          </el-button>
          <el-button v-else type="warning" @click="unpublish(detail)">下架</el-button>
        </div>

        <h3 class="catalog-title">章与课时</h3>
        <div class="catalog-row">
          <el-input v-model="chapterTitle" placeholder="新增章标题" @keyup.enter="addChapter" />
          <el-button type="primary" @click="addChapter">新增章</el-button>
        </div>

        <el-collapse class="catalog-chapters">
          <el-collapse-item
            v-for="chapter in detail.chapters"
            :key="chapter.id"
            :name="chapter.id"
          >
            <template #title>
              <span class="catalog-chapter-title">
                {{ chapter.title }}
                <el-tag size="small" type="info">v{{ chapter.version }}</el-tag>
              </span>
              <span class="catalog-node-actions" @click.stop>
                <el-button link size="small" @click="moveChapter(chapter.id, -1)">↑</el-button>
                <el-button link size="small" @click="moveChapter(chapter.id, 1)">↓</el-button>
                <el-button link size="small" type="danger" @click="deleteChapter(chapter.id, chapter.version)">
                  删除
                </el-button>
              </span>
            </template>

            <el-table :data="chapter.lessons" size="small" row-key="id">
              <el-table-column prop="title" label="课时" min-width="150" />
              <el-table-column prop="duration_sec" label="时长(秒)" width="90" />
              <el-table-column label="资产" width="110">
                <template #default="{ row }">
                  <el-tag
                    size="small"
                    :type="row.asset_status === 'encoded' ? 'success' : 'danger'"
                  >
                    {{ row.asset_status ?? '未挂载' }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="操作" width="180">
                <template #default="{ row }">
                  <el-button link size="small" @click="moveLesson(chapter.id, row.id, -1)">↑</el-button>
                  <el-button link size="small" @click="moveLesson(chapter.id, row.id, 1)">↓</el-button>
                  <el-button link size="small" type="danger" @click="deleteLesson(row.id, row.version)">
                    删除
                  </el-button>
                </template>
              </el-table-column>
            </el-table>

            <div v-if="lessonForm.chapterId === chapter.id" class="catalog-row">
              <el-input v-model="lessonForm.title" placeholder="课时标题" />
              <el-input-number v-model="lessonForm.duration" :min="0" :max="1440" />
              <el-button type="primary" @click="addLesson">保存课时</el-button>
              <el-button @click="lessonForm.chapterId = 0">取消</el-button>
            </div>
            <el-button
              v-else
              link
              type="primary"
              class="catalog-add-lesson"
              @click="startLesson(chapter.id)"
            >
              + 新增课时
            </el-button>
          </el-collapse-item>
        </el-collapse>
        <el-empty v-if="detail.chapters.length === 0" description="尚无章，先新增一章" />
      </template>
    </el-drawer>
  </div>
</template>
