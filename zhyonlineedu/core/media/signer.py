"""媒体票据签名（§21.4 · F3/F4/F8）。

三层 TTL：ts 长（24h~7d）/ playlist 中（5~15min）/ key 短（120s，nonce 单次）。
- kid 与用户无关 → playlist 可 CDN 长缓存；
- 用户身份只在取 key 的 ksession 里验证（media_keysession 表，单次 nonce）；
- ksession 不进 JWT（§7.1）；不绑 IP，绑 ksession+nonce+弱 UA（F8）。
"""


def issue_ticket(lesson_id: int, user_id: int) -> dict[str, object]:
    """开票：{playlist_url, key_url, fallback_urls, ticket_ttl_sec, key_ttl_sec,
    profile_allowed, resume_second, ticket_refresh_url, watermark}（§2.4 第 8~9 步）。"""
    raise NotImplementedError("M2.5")
