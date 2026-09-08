import { render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { readRequestDetail } from './api'
import { TrajectoryBusinessOperations } from './TrajectoryBusinessOperations'
import type { RequestDetail } from './types'

vi.mock('./api', () => ({ readRequestDetail: vi.fn() }))

describe('TrajectoryBusinessOperations', () => {
  it('reads only submitted request ids from this workspace and keeps packet usage unknown', async () => {
    vi.mocked(readRequestDetail).mockResolvedValue({
      request: { request_id: 'request-one', created_at: '2026-09-08T02:00:00Z' },
      entitlement: { name: '仪表盘查看' },
      decision_packet: { packet_id: 'packet-one', created_at: '2026-09-08T02:01:00Z', generation_mode: 'provider' },
      approval: { approval_case_id: 'approval-one', created_at: '2026-09-08T02:02:00Z', approval_status: 'pending' },
    } as RequestDetail)
    render(<TrajectoryBusinessOperations events={[
      { id: 1, type: 'business.status', payload: { status: 'ready_to_submit', turn_id: 'confirm-chat' } },
      { id: 2, type: 'business.status', payload: { status: 'submitted', request_id: 'request-one' } },
      { id: 2, type: 'business.status', payload: { status: 'submitted', request_id: 'request-one' } },
    ]} />)
    expect(await screen.findByText('冻结审批材料')).toBeInTheDocument()
    expect(screen.getByText('正式申请已创建')).toBeInTheDocument()
    expect(screen.getByText('审批已启动')).toBeInTheDocument()
    expect(screen.getByText(/模型调用次数未记录/)).toBeInTheDocument()
    expect(screen.queryByText(/调用 0 次/)).not.toBeInTheDocument()
    expect(readRequestDetail).toHaveBeenCalledTimes(1)
    expect(readRequestDetail).toHaveBeenCalledWith('request-one', expect.any(AbortSignal))
  })
  it('does not turn confirmation into a business operation and shows an honest read failure', async () => {
    vi.mocked(readRequestDetail).mockRejectedValue(new Error('private detail'))
    const { rerender } = render(<TrajectoryBusinessOperations events={[
      { id: 1, type: 'business.status', payload: { status: 'ready_to_submit', turn_id: 'chat' } },
    ]} />)
    expect(screen.queryByText('正式申请已创建')).not.toBeInTheDocument()
    rerender(<TrajectoryBusinessOperations events={[
      { id: 2, type: 'business.status', payload: { status: 'submitted', request_id: 'r-two' } },
    ]} />)
    await waitFor(() => expect(screen.getByText(/业务详情暂时无法读取/)).toBeInTheDocument())
    expect(screen.queryByText('private detail')).not.toBeInTheDocument()
    expect(screen.queryByText('冻结审批材料')).not.toBeInTheDocument()
  })
})
