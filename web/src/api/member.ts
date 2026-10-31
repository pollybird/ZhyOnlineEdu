import { apiGet, apiPost } from './request'
import type { MemberLevelListOut, MemberMeOut, PayOrderListOut, PayOrderOut } from './types'

/** 学员端会员与订单（M2.12 / M2.14）。 */
export const memberApi = {
  /** 学员可见的可购档位（仅启用）；管理端档位接口仍需 member:manage。 */
  offers(): Promise<MemberLevelListOut> {
    return apiGet<MemberLevelListOut>('/member/offers')
  },

  me(): Promise<MemberMeOut> {
    return apiGet<MemberMeOut>('/member/me')
  },

  orders(params: { status?: string; page?: number } = {}): Promise<PayOrderListOut> {
    return apiGet<PayOrderListOut>('/pay/orders', { params })
  },

  createOrder(levelId: number, channel: 'wechat' | 'alipay' | 'mock' = 'mock'): Promise<PayOrderOut> {
    return apiPost<PayOrderOut>('/pay/orders', { level_id: levelId, channel })
  },

  cancelOrder(orderNo: string): Promise<PayOrderOut> {
    return apiPost<PayOrderOut>(`/pay/orders/${orderNo}/cancel`)
  },
}
