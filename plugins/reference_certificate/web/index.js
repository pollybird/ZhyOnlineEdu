/**
 * reference_certificate 前端 ESM 入口（M3.5 契约）。
 *
 * 页面 key 与 manifest `routes` 对齐：菜单 `/plugin/reference_certificate/certificates`
 * 命中 `pages.certificates`；页面数据全部经 `ctx.request('certificates')` 走统一入口。
 */
export default {
  pages: {
    certificates(el, ctx) {
      let disposed = false
      const title = document.createElement('h3')
      title.textContent = '我的结业证书'
      const hint = document.createElement('p')
      hint.textContent = '证书由「课时完成」事件生成；试看票来源的进度不计入。'
      const list = document.createElement('ul')
      el.replaceChildren(title, hint, list)

      ctx
        .request('certificates')
        .then((data) => {
          if (disposed) {
            return
          }
          if (data.items.length === 0) {
            hint.textContent = '暂无证书，完成一节课后自动签发。'
            return
          }
          for (const item of data.items) {
            const li = document.createElement('li')
            li.textContent = `课程 ${item.course_id} · 课时 ${item.lesson_id} · 完成度 ${item.percent}% · ${item.issued_at}`
            list.append(li)
          }
        })
        .catch((error) => {
          if (!disposed) {
            hint.textContent = `加载失败：${error.message}`
          }
        })

      return () => {
        disposed = true
        el.replaceChildren()
      }
    },
  },
}
