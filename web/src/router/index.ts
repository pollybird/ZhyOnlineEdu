import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { useInstallStore } from '../stores/install'

/**
 * 路由守卫（M1.7 验收：安装 + 登录 + 框架页）。
 *
 * 顺序：未安装 → /install；已安装但未登录 → 静默刷新一次，失败回 /login。
 * 播放类 402xx 不在此处处理（不跳登录，见 api/request.ts）。
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
        { path: '', name: 'home', component: () => import('../views/HomeView.vue') },
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
