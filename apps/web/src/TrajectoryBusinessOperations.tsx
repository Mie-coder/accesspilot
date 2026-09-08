import { useEffect, useState } from 'react'
import { readRequestDetail } from './api'
import type { DecisionPacket, RequestDetail, RequestResult, WorkspaceEvent } from './types'

interface Props {
  events: WorkspaceEvent[]
  requestResult?: RequestResult | null
  decisionPacket?: DecisionPacket | null
  approvalCase?: RequestDetail['approval']
}

export function TrajectoryBusinessOperations({ events, requestResult, decisionPacket, approvalCase }: Props) {
  // Only ids already exposed by this authenticated workspace; the existing detail API checks access again.
  const ids = [...new Set([
    ...events.filter((event) => event.type === 'business.status' && event.payload.status === 'submitted')
      .map((event) => event.payload.request_id),
    requestResult?.request_id,
  ].filter((id): id is string => typeof id === 'string' && id.length > 0))]
  const idsKey = JSON.stringify(ids)
  const [details, setDetails] = useState<Record<string, RequestDetail | null>>({})
  useEffect(() => {
    const controller = new AbortController()
    for (const id of JSON.parse(idsKey) as string[]) {
      void readRequestDetail(id, controller.signal).then((detail) => {
        if (!controller.signal.aborted) setDetails((current) => ({ ...current, [id]: detail }))
      }).catch(() => {
        if (!controller.signal.aborted) setDetails((current) => ({ ...current, [id]: null }))
      })
    }
    return () => controller.abort()
  }, [idsKey, decisionPacket?.packet_id, approvalCase?.approval_case_id, approvalCase?.approval_status])

  if (ids.length === 0) return null
  return <section className="trajectory-business" aria-label="业务操作">
    <h2>业务操作</h2>
    <p>独立于聊天轮次，按正式申请展示。聊天中的确认不代表已提交，也不包含生成审批材料的模型调用。</p>
    {ids.map((id) => {
      const detail = details[id]
      return <article key={id}>
        <h3>{detail?.entitlement.name ?? '正式申请'}</h3>
        <p>工单：{id}</p>
        {detail === undefined ? <p role="status">正在读取业务详情…</p>
          : detail === null ? <p role="status">业务详情暂时无法读取，请刷新重试；不推断材料或审批状态。</p>
            : <ol>
              <li><strong>正式申请已创建</strong><p>{detail.request.created_at}</p></li>
              {detail.decision_packet ? <li>
                <strong>冻结审批材料</strong><p>{detail.decision_packet.created_at}</p>
                <p>{detail.decision_packet.generation_mode === 'provider' ? 'AI 建议来源：模型提供方'
                  : detail.decision_packet.generation_mode === 'deterministic' ? 'AI 建议来源：确定性生成'
                    : 'AI 建议暂不可用'}</p>
                <p>模型调用次数未记录，不计入聊天轮次。</p>
                <small>材料编号：{detail.decision_packet.packet_id}</small>
              </li> : null}
              {detail.approval ? <li><strong>审批已启动</strong>
                <p>{detail.approval.created_at}</p><p>状态：{({ pending: '待审批', approved: '已批准', rejected: '已拒绝' } as Record<string, string>)[detail.approval.approval_status] ?? detail.approval.approval_status}</p>
              </li> : null}
            </ol>}
        <small>来源：受身份校验的申请详情 · 只读</small>
      </article>
    })}
  </section>
}
