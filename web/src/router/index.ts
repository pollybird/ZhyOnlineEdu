import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { useInstallStore } from '../stores/install'

/**
 * 路由守卫（M1.7 / M2.14）。
 *
 * 顺序：未安装 → /install；已安装但未登录 → 静默刷新一次，失败回 /login。
 * 播放类 402xx 不在此处处理（不跳登录，见 api/request.ts）。
 * 管理端页面按 RBAC 权限码守卫；窄屏提示页不做重定向前置（§14.7）。
 */
export const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/install',
      name: 'install',
      component: () => import('../views/InstallView.vue'),
      meta: { public: true },
    },
    {
      path: '/login',
      name: 'login',
      component: () => import('../views/LoginView.vue'),
      meta: { public: true },
    },
    {
      path: '/',
      component: () => import('../components/AppShell.vue'),
      children: [
        { path: '', name: 'home', component: () => import('../views/LearnHomeView.vue') },
        {
          path: 'learn/courses/:courseId',
          name: 'learn-course',
          component: () => import('../views/CourseView.vue'),
        },
        {
          path: 'learn/lessons/:lessonId',
          name: 'learn-player',
          component: () => import('../views/PlayerView.vue'),
        },
        {
          path: 'learn/lessons/:lessonId/quiz',
          name: 'learn-quiz',
          component: () => import('../views/QuizView.vue'),
        },
        {
          path: 'learn/progress',
          name: 'learn-progress',
          component: () => import('../views/MyProgressView.vue'),
        },
        {
          path: 'member',
          name: 'member',
          component: () => import('../views/MemberView.vue'),
        },
        {
          path: 'catalog',
          name: 'catalog',
          component: () => import('../views/CatalogView.vue'),
          meta: { permission: 'course:edit' },
        },
        {
          path: 'system/mode',
          name: 'mode-switch',
          component: () => import('../views/ModeSwitchView.vue'),
          meta: { permission: 'system:manage' },
        },
        {
          path: 'admin',
          name: 'admin',
          component: () => import('../views/AdminHomeView.vue'),
          meta: { permission: 'course:edit' },
        },
        {
          path: 'admin/users',
          name: 'admin-users',
          component: () => import('../views/AdminUsersView.vue'),
          meta: { permission: 'user:manage' },
        },
        {
          path: 'admin/quiz',
          name: 'admin-quiz',
          component: () => import('../views/AdminQuizView.vue'),
          meta: { permission: 'course:edit' },
        },
        {
          path: 'admin/media',
          name: 'admin-media',
          component: () => import('../views/AdminMediaView.vue'),
          meta: { permission: 'course:edit' },
        },
        {
          path: 'admin/member',
          name: 'admin-member',
          component: () => import('../views/AdminMemberView.vue'),
          meta: { permission: 'member:manage' },
        },
        {
          path: 'admin/plugins',
          name: 'admin-plugins',
          component: () => import('../views/AdminPluginsView.vue'),
          meta: { permission: 'system:manage' },
        },
        {
          // 插件前端页面（M3.5）：`/plugin/<code>/<subpath>`，由插件 ESM 模块自行渲染
          path: 'plugin/:code/:subpath(.*)*',
          name: 'plugin-page',
          component: () => import('../views/PluginPageView.vue'),
        },
      ],
    },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

router.beforeEach(async (to) => {
  const install = useInstallStore()
  try {
    await install.loadState()
  } catch {
    // 后端不可达：放行由页面展示错误，避免守卫死循环
  }

  if (to.name === 'install') {
    return install.installed ? { name: 'home' } : true
  }
  if (!install.installed) {
    return { name: 'install', query: { redirect: to.fullPath } }
  }

  const auth = useAuthStore()
  if (to.meta.public === true) {
    return to.name === 'login' && auth.loggedIn ? { name: 'home' } : true
  }
  if (!auth.loggedIn && !(await auth.restore())) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  const permission = to.meta.permission
  if (typeof permission === 'string' && !auth.can(permission)) {
    return { name: 'home' }
  }
  return true
})
