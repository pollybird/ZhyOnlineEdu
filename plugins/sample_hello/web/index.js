/**
 * sample_hello 前端 ESM 入口（M3.5 契约）。
 *
 * `export default { pages }`，key 为 manifest `routes` 的子路径；
 * 页面卸载时执行返回的清理函数。
 */
export default {
  pages: {
    index(el, ctx) {
      let disposed = false
      const title = document.createElement('h3')
      title.textContent = 'sample_hello'
      const body = document.createElement('pre')
      body.textContent = '加载中…'
      el.replaceChildren(title, body)

      ctx
        .request('hello')
        .then((data) => {
          if (!disposed) {
            body.textContent = JSON.stringify(data, null, 2)
          }
        })
        .catch((error) => {
          if (!disposed) {
            body.textContent = `加载失败：${error.message}`
          }
        })

      return () => {
        disposed = true
        el.replaceChildren()
      }
    },
  },
}
