import { createPinia } from 'pinia'
import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import App from './App.vue'
import { setSessionExpiredHandler } from './api/request'
import { router } from './router'
import { useAuthStore } from './stores/auth'
import './style.css'

const pinia = createPinia()
const app = createApp(App).use(pinia).use(router).use(ElementPlus)

// 登录态失效（AUTH_* 且刷新失败）：清理内存态并回登录页；
// 播放类 40201/40205 不触发此回调（§14.1）。
setSessionExpiredHandler(() => {
  const auth = useAuthStore(pinia)
  auth.clear()
  const current = router.currentRoute.value
  if (current.name === 'login') {
    return
  }
  void router.replace({ name: 'login', query: { redirect: current.fullPath } })
})

app.mount('#app')
