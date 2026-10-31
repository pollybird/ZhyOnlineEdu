<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { courseAdminApi, quizAdminApi } from '../api/admin'
import { ApiError } from '../api/request'
import type { LessonOut, QuizOptionOut, QuizQuestionOut } from '../api/types'
import AdminMobileGate from '../components/AdminMobileGate.vue'
import { courseStatusLabel, questionKindLabel } from '../utils/labels'

/**
 * 管理端题库（M2.14 · M2.6）。
 * - 先选课程再选课时；题目按课时维护；
 * - 标准答案只在此页面（管理端）可见，学员端接口永不返回；
 * - 单选/多选需至少 2 个选项，判断题为固定 T/F。
 */
const loading = ref(false)
const courses = ref<{ id: number; title: string; status: string }[]>([])
const courseId = ref<number | null>(null)
const detail = ref<{ chapters: { id: number; title: string; lessons: LessonOut[] }[] } | null>(null)
const lessonId = ref<number | null>(null)
const questions = ref<QuizQuestionOut[]>([])
const dialogVisible = ref(false)
const editing = ref<QuizQuestionOut | null>(null)

const form = reactive({
  kind: 'single',
  stem: '',
  options: [
    { key: 'A', text: '' },
    { key: 'B', text: '' },
  ] as QuizOptionOut[],
  answer: [] as string[],
  explanation: '',
  score: 5,
  is_enabled: true,
})

const lessons = computed<LessonOut[]>(() =>
  (detail.value?.chapters ?? []).flatMap((chapter) => chapter.lessons),
)
const lessonOptions = computed(() =>
  lessons.value.map((item) => ({ id: item.id, title: item.title })),
)

const judgeOptions: QuizOptionOut[] = [
  { key: 'T', text: '正确' },
  { key: 'F', text: '错误' },
]

function notifyError(error: unknown): void {
  if (error instanceof ApiError) {
    ElMessage.error(error.message)
    return
  }
  ElMessage.error('请求失败，请稍后重试')
}

async function guard(task: () => Promise<void>): Promise<void> {
  loading.value = true
  try {
    await task()
  } catch (error) {
    notifyError(error)
  } finally {
    loading.value = false
  }
}

async function loadCourses(): Promise<void> {
  await guard(async () => {
    const payload = await courseAdminApi.courses({ page_size: 100 })
    courses.value = payload.items
    if (courses.value.length > 0 && courseId.value === null) {
      courseId.value = courses.value[0].id
    }
  })
}

async function loadDetail(): Promise<void> {
  if (courseId.value === null) {
    detail.value = null
    return
  }
  await guard(async () => {
    detail.value = await courseAdminApi.detail(courseId.value as number)
    if (lessonId.value === null) {
      lessonId.value = lessons.value[0]?.id ?? null
    }
  })
}

async function loadQuestions(): Promise<void> {
  if (lessonId.value === null) {
    questions.value = []
    return
  }
  await guard(async () => {
    const payload = await quizAdminApi.questions({ lesson_id: lessonId.value as number, page_size: 100 })
    questions.value = payload.items
  })
}

watch(courseId, () => {
  lessonId.value = null
  void loadDetail()
})

watch(lessonId, () => {
  void loadQuestions()
})

function applyKind(kind: string): void {
  form.kind = kind
  form.answer = []
  if (kind === 'judge') {
    form.options = []
    return
  }
  if (form.options.length < 2) {
    form.options = [
      { key: 'A', text: '' },
      { key: 'B', text: '' },
    ]
  }
}

function addOption(): void {
  const nextKey = String.fromCharCode(65 + form.options.length)
  form.options.push({ key: nextKey, text: '' })
}

function removeOption(index: number): void {
  if (form.options.length <= 2) {
    ElMessage.warning('至少保留 2 个选项')
    return
  }
  form.options.splice(index, 1)
  form.options.forEach((option, position) => {
    option.key = String.fromCharCode(65 + position)
  })
  form.answer = form.answer.filter((key) => form.options.some((option) => option.key === key))
}

function openCreate(): void {
  editing.value = null
  form.kind = 'single'
  form.stem = ''
  form.options = [
    { key: 'A', text: '' },
    { key: 'B', text: '' },
  ]
  form.answer = []
  form.explanation = ''
  form.score = 5
  form.is_enabled = true
  dialogVisible.value = true
}

function openEdit(row: QuizQuestionOut): void {
  editing.value = row
  form.kind = row.kind
  form.stem = row.stem
  form.options = row.kind === 'judge' ? [] : row.options.map((option) => ({ ...option }))
  form.answer = [...row.answer]
  form.explanation = row.explanation ?? ''
  form.score = row.score
  form.is_enabled = row.is_enabled
  dialogVisible.value = true
}

async function save(): Promise<void> {
  if (lessonId.value === null) {
    ElMessage.warning('请先选择课时')
    return
  }
  if (form.stem.trim() === '') {
    ElMessage.warning('题干不能为空')
    return
  }
  if (form.answer.length === 0) {
    ElMessage.warning('请选择标准答案')
    return
  }
  if (form.kind !== 'judge') {
    const empty = form.options.some((option) => option.text.trim() === '')
    if (empty) {
      ElMessage.warning('选项内容不能为空')
      return
    }
    if (form.answer.length > 1 && form.kind === 'single') {
      ElMessage.warning('单选题只能有一个答案')
      return
    }
  }
  await guard(async () => {
    if (editing.value === null) {
      await quizAdminApi.create({
        lesson_id: lessonId.value,
        kind: form.kind,
        stem: form.stem.trim(),
        options: form.kind === 'judge' ? null : form.options,
        answer: form.answer,
        explanation: form.explanation.trim() || null,
        score: form.score,
        is_enabled: form.is_enabled,
      })
      ElMessage.success('题目已创建')
    } else {
      await quizAdminApi.update(editing.value.id, {
        version: editing.value.version,
        kind: form.kind,
        stem: form.stem.trim(),
        options: form.kind === 'judge' ? null : form.options,
        answer: form.answer,
        explanation: form.explanation.trim() || null,
        score: form.score,
        is_enabled: form.is_enabled,
      })
      ElMessage.success('题目已更新')
    }
    dialogVisible.value = false
    await loadQuestions()
  })
}

async function remove(row: QuizQuestionOut): Promise<void> {
  try {
    await ElMessageBox.confirm(`确认删除题目「${row.stem.slice(0, 24)}」？`, '删除确认', {
      type: 'warning',
    })
  } catch {
    return
  }
  await guard(async () => {
    await quizAdminApi.remove(row.id, row.version)
    ElMessage.success('已删除')
    await loadQuestions()
  })
}

onMounted(() => {
  void loadCourses()
})
</script>

<template>
  <AdminMobileGate>
    <div class="admin-page" v-loading="loading">
      <el-card class="admin-card">
        <template #header>
          <div class="admin-head">
            <span>题库管理</span>
            <el-button type="primary" size="small" @click="openCreate">新建题目</el-button>
          </div>
        </template>

        <div class="admin-filters">
          <el-select v-model="courseId" size="small" placeholder="选择课程" class="admin-select">
            <el-option
              v-for="course in courses"
              :key="course.id"
              :label="`${course.title}（${courseStatusLabel(course.status)}）`"
              :value="course.id"
            />
          </el-select>
          <el-select v-model="lessonId" size="small" placeholder="选择课时" class="admin-select">
            <el-option
              v-for="lesson in lessonOptions"
              :key="lesson.id"
              :label="lesson.title"
              :value="lesson.id"
            />
          </el-select>
          <el-button size="small" @click="loadQuestions">刷新</el-button>
        </div>

        <el-table :data="questions" size="small" row-key="id">
          <el-table-column prop="stem" label="题干" min-width="200" />
          <el-table-column label="类型" width="80">
            <template #default="{ row }">{{ questionKindLabel(row.kind) }}</template>
          </el-table-column>
          <el-table-column prop="score" label="分值" width="64" />
          <el-table-column label="标准答案" width="110">
            <template #default="{ row }">
              <el-tag v-for="key in row.answer" :key="key" size="small" type="success">
                {{ key }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="启用" width="70">
            <template #default="{ row }">
              <el-tag size="small" :type="row.is_enabled ? 'success' : 'info'">
                {{ row.is_enabled ? '是' : '否' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="110">
            <template #default="{ row }">
              <el-button link size="small" @click="openEdit(row)">编辑</el-button>
              <el-button link size="small" type="danger" @click="remove(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
        <el-empty v-if="questions.length === 0" description="该课时暂无题目" />

        <p class="admin-hint">
          学员端不显示标准答案；交卷按答案完全一致计分，多选题不设部分分。
        </p>
      </el-card>

      <el-dialog
        v-model="dialogVisible"
        :title="editing === null ? '新建题目' : '编辑题目'"
        width="640px"
      >
        <el-form label-width="90px">
          <el-form-item label="题型">
            <el-radio-group :model-value="form.kind" @change="applyKind(String($event))">
              <el-radio-button value="single">单选</el-radio-button>
              <el-radio-button value="multiple">多选</el-radio-button>
              <el-radio-button value="judge">判断</el-radio-button>
            </el-radio-group>
          </el-form-item>
          <el-form-item label="题干">
            <el-input v-model="form.stem" type="textarea" :rows="3" />
          </el-form-item>

          <el-form-item v-if="form.kind !== 'judge'" label="选项">
            <div class="admin-option-list">
              <div v-for="(option, index) in form.options" :key="option.key" class="admin-option">
                <el-tag size="small">{{ option.key }}</el-tag>
                <el-input v-model="option.text" size="small" placeholder="选项内容" />
                <el-button link type="danger" size="small" @click="removeOption(index)">
                  删除
                </el-button>
              </div>
              <el-button link type="primary" size="small" @click="addOption">+ 新增选项</el-button>
            </div>
          </el-form-item>

          <el-form-item label="标准答案">
            <el-radio-group
              v-if="form.kind !== 'multiple'"
              :model-value="form.answer[0] ?? ''"
              @change="form.answer = $event === undefined ? [] : [String($event)]"
            >
              <el-radio
                v-for="option in form.kind === 'judge' ? judgeOptions : form.options"
                :key="option.key"
                :value="option.key"
              >
                {{ option.key }}. {{ option.text }}
              </el-radio>
            </el-radio-group>
            <el-checkbox-group v-else v-model="form.answer">
              <el-checkbox v-for="option in form.options" :key="option.key" :value="option.key">
                {{ option.key }}. {{ option.text }}
              </el-checkbox>
            </el-checkbox-group>
          </el-form-item>

          <el-form-item label="分值">
            <el-input-number v-model="form.score" :min="1" :max="100" />
          </el-form-item>
          <el-form-item label="解析">
            <el-input v-model="form.explanation" type="textarea" :rows="2" />
          </el-form-item>
          <el-form-item label="启用">
            <el-switch v-model="form.is_enabled" />
          </el-form-item>
        </el-form>
        <template #footer>
          <el-button @click="dialogVisible = false">取消</el-button>
          <el-button type="primary" :loading="loading" @click="save">保存</el-button>
        </template>
      </el-dialog>
    </div>
  </AdminMobileGate>
</template>
