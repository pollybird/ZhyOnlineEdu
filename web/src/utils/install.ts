/**
 * 安装向导展示步骤（M1.5/M2.14 实测修正）。
 *
 * `POST /install/token` 成功只换到安装会话：首次建库前还没有 `sys_install_state` 表可写，
 * 服务端 `GET /install/state` 仍返回 `step=start`。若前端只按服务端 step 渲染，
 * 令牌校验成功后会一直停在令牌页（表现为「令牌无效」）。本地持有会话即视为已进入
 * 「数据库」步骤；后续 admin/mode/finish 由服务端状态机推进。
 */
export function resolveInstallStep(step: string, hasSession: boolean): string {
  return step === 'start' && hasSession ? 'db' : step
}
