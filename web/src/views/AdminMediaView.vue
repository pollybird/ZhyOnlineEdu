<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { courseAdminApi, mediaAdminApi } from '../api/admin'
import type { AssetOut, CourseDetailOut, LessonOut } from '../api/types'
import AdminMobileGate from '../components/AdminMobileGate.vue'
import { formatBytes, formatDuration } from '../utils/format'
import { assetStatusLabel, courseStatusLabel } from '../utils/labels'

/**
 * 管理端媒体资产（M2.14 · M2.3/M2.4）。
 * - 上传：整文件 sha256 + 8MB 分片 md5，失败保留 upload_id（断点续传）；
 * - 检疫：魔数白名单 + ffprobe；通过后 asset=uploaded，需转码到 encoded 才可上架；
 * - 转码/删除均需 `course:edit`，沙箱失败只返回 ENC-10xx（无 stderr）。
 */
const loading = ref(false)
const courses = ref<{ id: number; title: string; status: string }[]>([])
const courseId = ref<number | null>(null)
const detail = ref<CourseDetailOut | null>(null)
const assets = ref<AssetOut[]>([])
const uploadVisible = ref(false)
const uploadLessonId = ref<number | null>(null)
const uploadFile = ref<File | null>(null)
const uploadPercent = ref(0)
const uploading = ref(false)

const lessons = computed<LessonOut[]>(() =>
  (detail.value?.chapters ?? []).flatMap((chapter) => chapter.lessons),
)
const assetMap = computed(() => {
  const map = new Map<number, AssetOut>()
  for (const asset of assets.value) map.set(asset.lesson_id, asset)
  return map
})
const uploadLessonTitle = computed(() => {
  const row = lessons.value.find((item) => item.id === uploadLessonId.value)
  return row?.title ?? `课时 #${uploadLessonId.value ?? '—'}`
})

function notifyError(error: unknown): void {
  ElMessage.error(error instanceof Error ? error.message : '请求失败，请稍后重试')
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

async function loadCourse(): Promise<void> {
  if (courseId.value === null) {
    detail.value = null
    assets.value = []
    return
  }
  await guard(async () => {
    const [course, assetList] = await Promise.all([
      courseAdminApi.detail(courseId.value as number),
      mediaAdminApi.assets({ course_id: courseId.value as number, page_size: 100 }),
    ])
    detail.value = course
    assets.value = assetList.items
  })
}

function statusType(status: string | null): 'success' | 'warning' | 'danger' | 'info' {
  if (status === 'encoded') return 'success'
  if (status === 'transcoding' || status === 'uploaded') return 'warning'
  if (status === 'failed' || status === 'rejected') return 'danger'
  return 'info'
}

function openUpload(lesson: LessonOut): void {
  uploadLessonId.value = lesson.id
  uploadFile.value = null
  uploadPercent.value = 0
  uploadVisible.value = true
}

function onFileChange(event: Event): void {
  const input = event.target as HTMLInputElement
  uploadFile.value = input.files?.[0] ?? null
}

async function startUpload(): Promise<void> {
  if (uploadLessonId.value === null || uploadFile.value === null) {
    ElMessage.warning('请选择文件')
    return
  }
  uploading.value = true
  uploadPercent.value = 0
  try {
    const result = await mediaAdminApi.upload(
      uploadLessonId.value,
      uploadFile.value,
      (progress) => {
        uploadPercent.value = progress.percent
      },
    )
    ElMessage.success(`上传完成，文件状态：${assetStatusLabel(result.asset_status)}`)
    uploadVisible.value = false
    await loadCourse()
  } catch (error) {
    notifyError(error)
  } finally {
    uploading.value = false
  }
}

async function retranscode(lesson: LessonOut): Promise<void> {
  await guard(async () => {
    const result = await mediaAdminApi.retranscode(lesson.id, ['360', '720', '1080'])
    ElMessage.success(
      result.created ? '已创建转码任务' : '已有排队中的任务，未重复创建',
    )
    await loadCourse()
  })
}

async function removeAsset(lesson: LessonOut): Promise<void> {
  try {
    await ElMessageBox.confirm(`确认删除课时「${lesson.title}」的媒体文件？`, '删除确认', {
      type: 'warning',
    })
  } catch {
    return
  }
  await guard(async () => {
    await mediaAdminApi.remove(lesson.id)
    ElMessage.success('资产已删除')
    await loadCourse()
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
            <span>媒体文件</span>
            <el-button size="small" @click="loadCourse">刷新</el-button>
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
          <el-button size="small" @click="loadCourse">加载课程</el-button>
        </div>

        <el-alert
          type="info"
          :closable="false"
          show-icon
          title="请在本机或 HTTPS 环境下上传；视频转码完成后课时才能上架。"
          class="admin-alert"
        />

        <el-table :data="lessons" size="small" row-key="id">
          <el-table-column prop="title" label="课时" min-width="160" />
          <el-table-column label="时长" width="90">
            <template #default="{ row }">{{ formatDuration(row.duration_sec) }}</template>
          </el-table-column>
          <el-table-column label="资产状态" width="110">
            <template #default="{ row }">
              <el-tag size="small" :type="statusType(assetMap.get(row.id)?.status ?? row.asset_status)">
                {{ assetStatusLabel(assetMap.get(row.id)?.status ?? row.asset_status) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="清晰度" width="140">
            <template #default="{ row }">
              <el-tag
                v-for="profile in assetMap.get(row.id)?.profiles ?? []"
                :key="profile"
                size="small"
                type="info"
              >
                {{ profile }}P
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="更新" width="130">
            <template #default="{ row }">{{ assetMap.get(row.id)?.updated_at ?? '—' }}</template>
          </el-table-column>
          <el-table-column label="操作" width="210">
            <template #default="{ row }">
              <el-button link size="small" @click="openUpload(row)">上传</el-button>
              <el-button link size="small" type="primary" @click="retranscode(row)">
                重新转码
              </el-button>
              <el-button
                link
                size="small"
                type="danger"
                :disabled="assetMap.get(row.id) === undefined"
                @click="removeAsset(row)"
              >
                删除资产
              </el-button>
            </template>
          </el-table-column>
        </el-table>
        <el-empty v-if="lessons.length === 0" description="请选择课程或课程尚无课时" />
      </el-card>

      <el-dialog v-model="uploadVisible" title="上传媒体" width="520px">
        <p class="admin-hint">目标课时：{{ uploadLessonTitle }}</p>
        <input type="file" accept="video/*,audio/*" @change="onFileChange" />
        <p v-if="uploadFile" class="admin-hint">
          已选择 {{ uploadFile.name }}（{{ formatBytes(uploadFile.size) }}）
        </p>
        <el-progress :percentage="uploadPercent" />
        <template #footer>
          <el-button :disabled="uploading" @click="uploadVisible = false">取消</el-button>
          <el-button type="primary" :loading="uploading" @click="startUpload">开始上传</el-button>
        </template>
      </el-dialog>
    </div>
  </AdminMobileGate>
</template>
