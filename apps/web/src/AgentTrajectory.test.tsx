import { fireEvent, render, screen, within } from '@testing-library/react'
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

function langGraphStart(id: number, turnId: string): WorkspaceEvent {
  return event(id, 'turn.started', turnId, {
    orchestrator: 'langgraph',
    flow_version: 2,
    graph_version: 'accesspilot-langgraph-v1.3',
  })
}

describe('AgentTrajectory', () => {
  it('explains the read-only boundary and renders empty and reconnecting states', () => {
    const { rerender } = render(<AgentTrajectory events={[]} />)

    const trajectory = screen.getByLabelText('Agent 运行轨迹')
    expect(within(trajectory).getByRole('heading', { name: 'Agent 运行轨迹' })).toBeInTheDocument()
    expect(within(trajectory).getByText('这是执行事实的只读投影，不是模型思维链。')).toBeInTheDocument()
    expect(within(trajectory).getByRole('status')).toHaveTextContent('还没有可展示的运行轨迹')

    rerender(<AgentTrajectory events={[]} connectionState="reconnecting" />)

    expect(within(trajectory).getByRole('status')).toHaveTextContent('轨迹加载中')
    expect(within(trajectory).getByRole('status')).toHaveTextContent('活动流正在重连')
  })

  it('selects the latest three turns by each group maximum DB id and sorts events by id', () => {
    render(
      <AgentTrajectory
        events={[
          event(40, 'message.completed', 'turn-four', { content: '第四轮终态', message_id: 'm-4' }),
          event(3, 'turn.started', 'turn-three'),
          event(100, 'message.completed', 'turn-one', { content: '旧轮晚到终态', message_id: 'm-1' }),
          event(1, 'turn.started', 'turn-one'),
          event(30, 'message.completed', 'turn-three', { content: '第三轮终态', message_id: 'm-3' }),
          event(2, 'turn.started', 'turn-two'),
          event(20, 'message.completed', 'turn-two', { content: '应被截掉的第二轮', message_id: 'm-2' }),
          event(4, 'turn.started', 'turn-four'),
        ]}
      />,
    )

    expect(screen.queryByText('应被截掉的第二轮')).not.toBeInTheDocument()
    expect(screen.getByText('第三轮终态')).toBeInTheDocument()
    expect(screen.getByText('第四轮终态')).toBeInTheDocument()
    expect(screen.getByText('旧轮晚到终态')).toBeInTheDocument()

    const turns = screen.getAllByTestId('agent-trajectory-turn')
    expect(turns).toHaveLength(3)
    expect(turns[0]).toHaveAttribute('data-turn-id', 'turn-three')
    expect(turns[1]).toHaveAttribute('data-turn-id', 'turn-four')
    expect(turns[2]).toHaveAttribute('data-turn-id', 'turn-one')
    expect(turns[0]).not.toHaveAttribute('open')
    expect(turns[1]).not.toHaveAttribute('open')
    expect(turns[2]).toHaveAttribute('open')

    const newestSteps = within(turns[2] as HTMLElement).getAllByRole('listitem')
    expect(newestSteps[0]).toHaveAttribute('data-event-id', '1')
    expect(newestSteps[1]).toHaveAttribute('data-event-id', '100')
  })

  it('labels each turn from its own orchestrator fact without pretending unknown history is LangGraph', () => {
    render(
      <AgentTrajectory
        events={[
          langGraphStart(1, 'turn-graph'),
          event(2, 'message.completed', 'turn-graph', { content: '图轮完成', message_id: 'm-1' }),
          event(3, 'turn.started', 'turn-legacy'),
          event(4, 'message.completed', 'turn-legacy', { content: '旧轮完成', message_id: 'm-2' }),
          event(5, 'message.user', 'turn-unknown', { content: '缺少编排器事实' }),
        ]}
      />,
    )

    expect(screen.getByText('LangGraph · Flow 2 · accesspilot-langgraph-v1.3')).toBeInTheDocument()
    expect(screen.getByText('Legacy · ConversationService')).toBeInTheDocument()
    expect(screen.getByText('Unknown · 缺少编排器事实')).toBeInTheDocument()
  })

  it('maps every T36 lifecycle category and never calls non-retrieval tools RAG', () => {
    render(
      <AgentTrajectory
        events={[
          langGraphStart(10, 'turn-all'),
          event(11, 'message.user', 'turn-all', { content: '申请客户导出权限' }),
          event(12, 'intent.detected', 'turn-all', { intent: 'request_access', security_flagged: false }),
          event(13, 'agent.node.started', 'turn-all', {
            step_id: 'stp-route', node_code: 'route_intent', public_label: '识别意图与路由', status: 'running',
          }),
          event(14, 'agent.node.completed', 'turn-all', {
            step_id: 'stp-route', node_code: 'route_intent', public_label: '识别意图与路由', status: 'success',
          }),
          event(15, 'agent.route.selected', 'turn-all', { step_id: 'stp-route', route_code: 'request_access' }),
          event(16, 'model.started', 'turn-all', {
            step_id: 'stp-model', operation: 'parse_input', provider_mode: 'api', attempt: 1,
          }),
          event(17, 'model.completed', 'turn-all', {
            step_id: 'stp-model', operation: 'parse_input', provider_mode: 'api', attempt: 1,
            status: 'parsed', extracted_fields: ['entitlement_id', 'duration_days'],
          }),
          event(18, 'retrieval.started', 'turn-all', { step_id: 'stp-rag', retriever: 'pgvector' }),
          event(19, 'retrieval.completed', 'turn-all', {
            step_id: 'stp-rag', retriever: 'pgvector', status: 'grounded', match_count: 2,
            evidence_codes: ['POL-003', 'POL-004'],
          }),
          event(20, 'tool.started', 'turn-all', {
            step_id: 'stp-tool', tool: 'resolve_entitlement', tool_call_id: 'tool-1',
          }),
          event(21, 'tool.completed', 'turn-all', {
            step_id: 'stp-tool', tool: 'resolve_entitlement', tool_call_id: 'tool-1',
            status: 'success', summary: '唯一匹配客户导出权限',
          }),
          event(22, 'tool.summary', 'turn-all', {
            tool: 'list_eligible_access', status: 'success', summary: '共 2 项可申请权限',
          }),
          event(23, 'draft.updated', 'turn-all', {
            draft: { entitlement_id: 'insighthub.customer_export' }, missing_fields: ['justification'],
            can_enter_approval: false, draft_revision: 3,
          }),
          event(24, 'business.status', 'turn-all', { status: 'awaiting_confirmation' }),
          event(25, 'agent.input.required', 'turn-all', {
            step_id: 'stp-hitl', pending_input_id: 'pending-1', kind: 'confirmation', draft_revision: 3,
          }),
          event(26, 'agent.input.resumed', 'turn-all', {
            step_id: 'stp-hitl', pending_input_id: 'pending-1', kind: 'confirmation', decision: 'confirm',
          }),
          event(27, 'security.notice', 'turn-all', { code: 'SAFE_BOUNDARY', message: '安全边界已生效' }),
          event(28, 'message.completed', 'turn-all', {
            message_id: 'm-all', content: '申请信息已确认', business_status: 'ready_to_submit',
          }),
        ]}
      />,
    )

    const trajectory = screen.getByLabelText('turn-all Agent Loop')
    for (const label of [
      'Orchestrator', 'Input', 'Router', 'Node', 'Model · DeepSeek', 'RAG · pgvector',
      'Read-only tool · resolve_entitlement', 'Legacy tool · list_eligible_access',
      'State', 'HITL', 'Guardrail', 'Output',
    ]) {
      expect(within(trajectory).getAllByText(label).length).toBeGreaterThan(0)
    }
    expect(within(trajectory).getByText('识别意图与路由开始')).toBeInTheDocument()
    expect(within(trajectory).getByText('识别意图与路由完成')).toBeInTheDocument()
    expect(within(trajectory).getByText('已选择分支 request_access')).toBeInTheDocument()
    expect(within(trajectory).getAllByText(/DeepSeek Parser · 第 1 次/)).toHaveLength(2)
    expect(within(trajectory).getByText(/命中 2 条 · POL-003、POL-004/)).toBeInTheDocument()
    expect(within(trajectory).getByText('等待申请人确认')).toBeInTheDocument()
    expect(within(trajectory).getByText('已按 confirm 恢复执行')).toBeInTheDocument()
    expect(screen.getByText('完成', { selector: '.agent-trajectory-status' })).toBeInTheDocument()
    expect(within(trajectory).getAllByText('RAG · pgvector')).toHaveLength(2)
  })

  it('renders running, waiting HITL, recoverable error, completed, and interrupted states', () => {
    const { rerender } = render(
      <AgentTrajectory
        events={[
          langGraphStart(1, 'turn-running'),
          event(2, 'agent.node.started', 'turn-running', {
            step_id: 'stp-1', node_code: 'route_intent', public_label: '识别意图与路由', status: 'running',
          }),
        ]}
        isRunning
      />,
    )
    expect(screen.getByText('运行中', { selector: '.agent-trajectory-status' })).toBeInTheDocument()

    rerender(<AgentTrajectory events={[
      langGraphStart(1, 'turn-waiting'),
      event(2, 'agent.input.required', 'turn-waiting', {
        step_id: 'stp-1', pending_input_id: 'pending-1', kind: 'confirmation', draft_revision: 2,
      }),
    ]} />)
    expect(screen.getByText('等待 HITL', { selector: '.agent-trajectory-status' })).toBeInTheDocument()

    rerender(<AgentTrajectory events={[
      langGraphStart(1, 'turn-error'),
      event(2, 'error.recoverable', 'turn-error', { code: 'MODEL_UNAVAILABLE', message: '模型暂时不可用' }),
    ]} />)
    expect(screen.getByText('可恢复错误', { selector: '.agent-trajectory-status' })).toBeInTheDocument()

    rerender(<AgentTrajectory events={[
      langGraphStart(1, 'turn-complete'),
      event(2, 'message.completed', 'turn-complete', { message_id: 'm-1', content: '已完成' }),
    ]} />)
    expect(screen.getByText('完成', { selector: '.agent-trajectory-status' })).toBeInTheDocument()

    rerender(<AgentTrajectory events={[
      langGraphStart(1, 'turn-interrupted'),
      event(2, 'turn.interrupted', 'turn-interrupted', { reason: 'client_cancelled', retryable: true }),
    ]} />)
    expect(screen.getByText('已中断', { selector: '.agent-trajectory-status' })).toBeInTheDocument()
  })

  it('ignores unknown event contents and projects safe details from a per-type whitelist', () => {
    const forbiddenValues = [
      '原始 checkpoint 值', '隐藏思维链', '系统提示词', 'token-secret-value',
      'csrf-secret-value', 'authorization-secret-value', '内部预算 99', '内部异常栈', '未投影的草稿事实',
    ]
    render(
      <AgentTrajectory
        events={[
          langGraphStart(1, 'turn-safe'),
          event(2, 'model.completed', 'turn-safe', {
            step_id: 'stp-1', operation: 'parse_input', provider_mode: 'mock', attempt: 1,
            status: 'parsed', extracted_fields: ['duration_days'],
            checkpoint: forbiddenValues[0], hidden_reasoning: forbiddenValues[1], system_prompt: forbiddenValues[2],
          }),
          event(3, 'draft.updated', 'turn-safe', {
            draft_revision: 4, missing_fields: [], can_enter_approval: true,
            draft: { justification: forbiddenValues[8] },
          }),
          event(4, 'error.recoverable', 'turn-safe', {
            code: 'SAFE_ERROR', message: '请稍后重试', token: forbiddenValues[3], csrf: forbiddenValues[4],
            authorization: forbiddenValues[5], quota: forbiddenValues[6], stack: forbiddenValues[7],
          }),
          event(5, 'mystery.debug', 'turn-safe', { raw: '未知事件原始内容' }),
        ]}
      />,
    )

    const trajectory = screen.getByLabelText('Agent 运行轨迹')
    expect(within(trajectory).getByText('1 个未知事件已安全忽略')).toBeInTheDocument()
    expect(trajectory).not.toHaveTextContent('未知事件原始内容')
    for (const forbidden of forbiddenValues) expect(trajectory).not.toHaveTextContent(forbidden)
    expect(within(trajectory).getAllByText('安全事件详情').length).toBeGreaterThan(0)
    expect(within(trajectory).queryAllByRole('button')).toHaveLength(0)
    for (const action of ['恢复', '重放', '编辑', '提交', '执行工具']) {
      expect(within(trajectory).queryByRole('button', { name: action })).not.toBeInTheDocument()
    }
  })

  it('does not label resolve, list, or legacy search tool summaries as RAG without retrieval facts', () => {
    render(
      <AgentTrajectory
        events={[
          langGraphStart(1, 'turn-tools'),
          event(2, 'tool.completed', 'turn-tools', {
            step_id: 'stp-1', tool: 'resolve_entitlement', tool_call_id: 'tool-1', status: 'success', summary: '已匹配',
          }),
          event(3, 'tool.completed', 'turn-tools', {
            step_id: 'stp-2', tool: 'list_policy_catalog', tool_call_id: 'tool-2', status: 'success', summary: '已列出',
          }),
          event(4, 'tool.summary', 'turn-tools', {
            tool: 'search_policies', status: 'grounded', summary: 'Legacy 摘要',
          }),
          event(5, 'message.completed', 'turn-tools', { message_id: 'm-1', content: '已完成' }),
        ]}
      />,
    )

    expect(screen.queryByText('RAG · pgvector')).not.toBeInTheDocument()
    expect(screen.getByText('Read-only tool · resolve_entitlement')).toBeInTheDocument()
    expect(screen.getByText('Read-only tool · list_policy_catalog')).toBeInTheDocument()
    expect(screen.getByText('Legacy tool · search_policies')).toBeInTheDocument()
  })
})

it('counts recorded provider attempts by logical step and distinguishes missing history', () => {
  const { rerender } = render(<AgentTrajectory events={[
    event(1, 'turn.started', 'zero', { model_usage_recorded: true }),
    event(2, 'message.completed', 'zero'),
  ]} />)
  expect(screen.getByText('本轮模型调用 0 次')).toBeInTheDocument()
  expect(screen.getByText('规则处理：未调用模型')).toBeInTheDocument()
  rerender(<AgentTrajectory events={[
    event(1, 'turn.started', 'two', { model_usage_recorded: true }),
    event(2, 'model.started', 'two', {
      step_id: 'intent', operation: 'route_intent', provider_mode: 'api', attempt: 1,
    }),
    event(3, 'model.completed', 'two', {
      step_id: 'intent', operation: 'route_intent', provider_mode: 'api', attempt: 1, status: 'parsed',
    }),
    event(4, 'model.started', 'two', {
      step_id: 'parse', operation: 'parse_input', provider_mode: 'api', attempt: 1,
    }),
    event(5, 'model.completed', 'two', {
      step_id: 'parse', operation: 'parse_input', provider_mode: 'api', attempt: 1, status: 'malformed',
    }),
    event(6, 'model.started', 'two', {
      step_id: 'parse', operation: 'parse_input', provider_mode: 'api', attempt: 2,
    }),
    event(7, 'model.started', 'two', {
      step_id: 'parse', operation: 'parse_input', provider_mode: 'api', attempt: 2,
    }),
    event(8, 'tool.completed', 'two', { tool: 'list_eligible_access', status: 'success' }),
  ]} />)
  expect(screen.getByText('本轮模型调用 3 次')).toBeInTheDocument()
  expect(screen.getByText('意图理解：调用模型 1 次')).toBeInTheDocument()
  expect(screen.getByText('申请字段提取：调用模型 2 次')).toBeInTheDocument()
  expect(screen.getByText(/查询可申请权限/)).toBeInTheDocument()
  rerender(<AgentTrajectory events={[event(1, 'turn.started', 'old')]} />)
  expect(screen.getByText('模型调用次数未记录')).toBeInTheDocument()
  expect(screen.queryByText('本轮模型调用 0 次')).not.toBeInTheDocument()
})

it('names chat turns, reveals older history and keeps request submission outside chat counts', () => {
  const history = [
    event(1, 'turn.started', 'query', { model_usage_recorded: true }),
    event(2, 'message.user', 'query', { content: '我能申请什么权限' }),
    event(3, 'message.completed', 'query', { intent: 'discover_eligible_access', content: '有三项权限' }),
    event(4, 'turn.started', 'choose'),
    event(5, 'message.user', 'choose', { content: '申请仪表盘' }),
    event(6, 'tool.summary', 'choose', { tool: 'resolve_entitlement', status: 'matched' }),
    event(7, 'draft.updated', 'choose', { missing_fields: ['duration_days', 'justification'] }),
    event(8, 'message.completed', 'choose', { intent: 'request_access', content: '请输入期限' }),
    event(9, 'turn.started', 'duration', { model_usage_recorded: true }),
    event(10, 'message.user', 'duration', { content: '7天' }),
    event(11, 'draft.updated', 'duration', { missing_fields: ['justification'] }),
    event(12, 'message.completed', 'duration', { intent: 'request_access', content: '请输入理由' }),
    event(13, 'turn.started', 'reason', { model_usage_recorded: true }),
    event(14, 'message.user', 'reason', { content: '用于给客户演示' }),
    event(15, 'draft.updated', 'reason', { missing_fields: [] }),
    event(16, 'message.completed', 'reason', { intent: 'request_access', content: '请确认' }),
    event(17, 'turn.started', 'confirm', { model_usage_recorded: true }),
    event(18, 'message.user', 'confirm', { content: '确认提交' }),
    event(19, 'message.completed', 'confirm', { intent: 'request_access', business_status: 'ready_to_submit', content: '已确认，可创建申请' }),
  ]
  const { rerender } = render(<AgentTrajectory events={[...history, history[18]!]} />)
  expect(screen.getByText('确认申请信息')).toBeInTheDocument()
  expect(screen.getByText('补充申请理由')).toBeInTheDocument()
  expect(screen.getByText('补充申请期限')).toBeInTheDocument()
  expect(screen.getByText('你说：确认提交')).toBeInTheDocument()
  expect(screen.getByText('处理结果：已确认，可创建申请')).toBeInTheDocument()
  expect(screen.getAllByTestId('agent-trajectory-turn')).toHaveLength(3)
  fireEvent.click(screen.getByRole('button', { name: /查看更早轮次/ }))
  rerender(<AgentTrajectory events={history} />)
  expect(screen.getAllByTestId('agent-trajectory-turn')).toHaveLength(5)
  expect(screen.getByText('查询可申请权限')).toBeInTheDocument()
  expect(screen.getByText('选择申请权限')).toBeInTheDocument()
  const newest = screen.getAllByTestId('agent-trajectory-turn').at(-1)!
  expect(newest.querySelector('summary')).not.toHaveTextContent('confirm')
  expect(within(newest).getAllByText('确认提交', { exact: true })).toHaveLength(1)
})
