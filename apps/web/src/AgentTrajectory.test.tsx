import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { AgentTrajectory } from './AgentTrajectory'
import type { WorkspaceEvent } from './types'

function event(
  id: number,
  type: string,
  turnId: string | null,
  payload: Record<string, unknown> = {},
): WorkspaceEvent {
  return {
    id,
    type,
    payload: {
      ...payload,
      ...(turnId === null ? {} : { turn_id: turnId }),
    },
  }
}

describe('AgentTrajectory', () => {
  it('explains its read-only event boundary and renders a useful empty state', () => {
    render(<AgentTrajectory events={[]} />)

    expect(screen.getByRole('heading', { name: 'Agent 运行轨迹' })).toBeInTheDocument()
    expect(screen.getByText(/ConversationService/)).toHaveTextContent('当前主编排器是 ConversationService，不是 LangGraph')
    expect(screen.getByText(/持久化的安全事件/)).toHaveTextContent('只读')
    expect(screen.getByText(/不包含隐藏推理/)).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('还没有可回放的 Agent 轨迹')
  })

  it('groups the latest three turns and expands only the newest turn', () => {
    render(
      <AgentTrajectory
        events={[
          event(1, 'turn.started', 'turn-1'),
          event(2, 'message.completed', 'turn-1', { message_id: 'm-1', content: '第一轮' }),
          event(3, 'turn.started', 'turn-2'),
          event(4, 'message.completed', 'turn-2', { message_id: 'm-2', content: '第二轮' }),
          event(5, 'turn.started', 'turn-3'),
          event(6, 'message.completed', 'turn-3', { message_id: 'm-3', content: '第三轮' }),
          event(7, 'turn.started', 'turn-4'),
          event(8, 'message.completed', 'turn-4', { message_id: 'm-4', content: '第四轮' }),
          event(9, 'message.user', null, { content: '无法可靠归组的旧事件' }),
        ]}
      />,
    )

    expect(screen.queryByText('第一轮')).not.toBeInTheDocument()
    expect(screen.queryByText('无法可靠归组的旧事件')).not.toBeInTheDocument()
    expect(screen.getByText('第二轮')).toBeInTheDocument()
    expect(screen.getByText('第三轮')).toBeInTheDocument()
    expect(screen.getByText('第四轮')).toBeInTheDocument()

    const turns = screen.getAllByTestId('agent-trajectory-turn')
    expect(turns).toHaveLength(3)
    expect(turns[0]).not.toHaveAttribute('open')
    expect(turns[1]).not.toHaveAttribute('open')
    expect(turns[2]).toHaveAttribute('open')
  })

  it('maps persisted events into an educational Agent Loop with tool results', () => {
    render(
      <AgentTrajectory
        events={[
          event(10, 'turn.started', 'turn-rag'),
          event(11, 'message.user', 'turn-rag', { content: '导出权限最多能申请多久？' }),
          event(12, 'intent.detected', 'turn-rag', {
            intent: 'policy_question',
            security_flagged: false,
          }),
          event(13, 'tool.summary', 'turn-rag', {
            tool: 'search_policies',
            status: 'grounded',
            summary: '返回 POL-003：最长 30 天',
          }),
          event(14, 'draft.updated', 'turn-rag', {
            draft: { entitlement_id: 'insighthub.customer_export' },
            missing_fields: ['justification'],
            can_enter_approval: false,
            draft_revision: 2,
          }),
          event(15, 'business.status', 'turn-rag', { status: 'collecting' }),
          event(16, 'message.assistant', 'turn-rag', { content: '请补充业务理由' }),
          event(17, 'message.completed', 'turn-rag', {
            message_id: 'm-rag',
            content: '政策规定最长 30 天',
          }),
        ]}
      />,
    )

    const trajectory = screen.getByLabelText('turn-rag Agent Loop')
    expect(within(trajectory).getByText('Orchestrator')).toBeInTheDocument()
    expect(within(trajectory).getByText('Input')).toBeInTheDocument()
    expect(within(trajectory).getByText('Router')).toBeInTheDocument()
    expect(within(trajectory).getByText('RAG · pgvector')).toBeInTheDocument()
    expect(within(trajectory).getByText('Agent state')).toBeInTheDocument()
    expect(within(trajectory).getByText('Decision / state')).toBeInTheDocument()
    expect(within(trajectory).getByText('Output')).toBeInTheDocument()
    expect(within(trajectory).getByText('调用 search_policies')).toBeInTheDocument()
    expect(within(trajectory).getByText(/^返回：返回 POL-003：最长 30 天/)).toBeInTheDocument()
    expect(within(trajectory).getByText('政策规定最长 30 天')).toBeInTheDocument()
    expect(within(trajectory).queryByText('请补充业务理由')).not.toBeInTheDocument()
    expect(within(trajectory).getAllByText('安全事件详情').length).toBeGreaterThan(0)
  })

  it('names other read-only tools and presents guardrail, error, and interruption events safely', () => {
    render(
      <AgentTrajectory
        events={[
          event(20, 'turn.started', 'turn-safe'),
          event(21, 'tool.summary', 'turn-safe', {
            tool: 'resolve_entitlement',
            status: 'success',
            summary: '唯一匹配客户导出权限',
          }),
          event(22, 'security.notice', 'turn-safe', {
            code: 'PROMPT_INJECTION_BLOCKED',
            message: '已忽略越权指令',
            hidden_reasoning: '不得显示的内部推理',
          }),
          event(23, 'error.recoverable', 'turn-safe', {
            code: 'MODEL_REPLY_UNAVAILABLE',
            message: '暂时无法理解，可以重试',
          }),
          event(24, 'turn.interrupted', 'turn-safe', {
            reason: 'client_cancelled',
            retryable: true,
          }),
        ]}
      />,
    )

    expect(screen.getByText('Read-only tool · resolve_entitlement')).toBeInTheDocument()
    expect(screen.getByText(/返回：唯一匹配客户导出权限/)).toBeInTheDocument()
    expect(screen.getByText('Guardrail')).toBeInTheDocument()
    expect(screen.getByText('Recoverable error')).toBeInTheDocument()
    expect(screen.getByText('Interrupted')).toBeInTheDocument()
    expect(screen.queryByText('不得显示的内部推理')).not.toBeInTheDocument()
  })
})
