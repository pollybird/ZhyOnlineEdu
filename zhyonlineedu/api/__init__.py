"""API 层（§15：82 个内核接口，统一前缀 /api/v1）。

红线（§4.2）：本层只允许调用 modules.learn.gateway.record() 写进度；
出现 learn_progress* 表的 INSERT/UPDATE 直接 PR 失败。
Blueprint 在能力探测完成后注册（§4.3）。
"""
