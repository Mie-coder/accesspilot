import type { WorkspaceEvent } from './types'

interface AgentTrajectoryProps {
  events: WorkspaceEvent[]
}

interface StepCopy {
  lane: string
  title: string
  detail: string
  tone: 'neutral' | 'input' | 'agent' | 'tool' | 'output' | 'warning' | 'error'
  safePayload: Record<string, unknown>
}

interface TurnGroup {
  turnId: string
  events: WorkspaceEvent[]
}

const supportedEventTypes = new Set([
  'turn.started',
  'message.user',
  'intent.detected',
  'tool.summary',
  'draft.updated',
  'business.status',
  'security.notice',
  'error.recoverable',
  'turn.interrupted',
  'message.assistant',
  'message.completed',
])

function textValue(value: unknown, fallback: string): string {
  return typeof value === 'string' && value.trim() ? value : fallback
}

function booleanValue(value: unknown): boolean | null {
  return typeof value === 'boolean' ? value : null
}

function stringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string')
    : []
}

function businessStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    collecting: '继续收集申请字段',
    awaiting_confirmation: '申请完整，等待用户确认',
    ready_to_submit: '用户已确认，可以创建正式申请',
    submitted: '正式申请已创建',
    answered: '只读问题已回答',
    needs_clarification: '需要补充上下文',
    validation_failed: '申请校验未通过',
    recoverable_error: '本轮可安全重试',
  }
  return labels[status] ?? status
}

function safeEventPayload(event: WorkspaceEvent): Record<string, unknown> {
  const payload = event.payload
  const base = { event_id: event.id, event_type: event.type }

  if (event.type === 'turn.started') {
    return { ...base, turn_id: payload.turn_id }
  }
  if (event.type === 'message.user' || event.type === 'message.assistant') {
    return { ...base, content: payload.content }
  }
  if (event.type === 'message.completed') {
    return {
      ...base,
      content: payload.content,
      intent: payload.intent,
      business_status: payload.business_status,
    }
  }
  if (event.type === 'intent.detected') {
    return {
      ...base,
      intent: payload.intent,
      security_flagged: payload.security_flagged,
    }
  }
  if (event.type === 'tool.summary') {
    return {
      ...base,
      tool: payload.tool,
      status: payload.status,
      summary: payload.summary,
    }
  }
  if (event.type === 'draft.updated') {
    return {
      ...base,
      draft_revision: payload.draft_revision,
      missing_fields: payload.missing_fields,
      can_enter_approval: payload.can_enter_approval,
    }
  }
  if (event.type === 'business.status') {
    return { ...base, status: payload.status, request_id: payload.request_id }
  }
  if (event.type === 'security.notice' || event.type === 'error.recoverable') {
    return { ...base, code: payload.code, message: payload.message }
  }
  if (event.type === 'turn.interrupted') {
    return { ...base, reason: payload.reason, retryable: payload.retryable }
  }
  return base
}

function stepCopy(event: WorkspaceEvent): StepCopy | null {
  const payload = event.payload
  const safePayload = safeEventPayload(event)

  if (event.type === 'turn.started') {
    return {
      lane: 'Orchestrator',
      title: 'Agent Loop 开始',
      detail: 'ConversationService 创建本轮，并串行调度后续安全步骤。',
      tone: 'agent',
      safePayload,
    }
  }
  if (event.type === 'message.user') {
    return {
      lane: 'Input',
      title: '用户输入',
      detail: textValue(payload.content, '已接收本轮输入'),
      tone: 'input',
      safePayload,
    }
  }
  if (event.type === 'intent.detected') {
    const intent = textValue(payload.intent, 'unknown')
    const securityFlagged = booleanValue(payload.security_flagged)
    return {
      lane: 'Router',
      title: '确定性意图路由',
      detail: `意图：${intent}${securityFlagged ? ' · 命中安全检查' : ''}`,
      tone: securityFlagged ? 'warning' : 'agent',
      safePayload,
    }
  }
  if (event.type === 'tool.summary') {
    const tool = textValue(payload.tool, 'read_only_tool')
    const status = textValue(payload.status, 'completed')
    const summary = textValue(payload.summary, '工具已完成')
    const isRag = tool === 'search_policies'
    return {
      lane: isRag ? 'RAG · pgvector' : `Read-only tool · ${tool}`,
      title: `调用 ${tool}`,
      detail: `返回：${summary} · 状态：${status}`,
      tone: 'tool',
      safePayload,
    }
  }
  if (event.type === 'draft.updated') {
    const revision = typeof payload.draft_revision === 'number' ? payload.draft_revision : null
    const missingFields = stringList(payload.missing_fields)
    const missingCopy = missingFields.length > 0
      ? `仍缺：${missingFields.join('、')}`
      : '申请字段已完整'
    return {
      lane: 'Agent state',
      title: '草稿状态更新',
      detail: `${revision === null ? '' : `修订 #${revision} · `}${missingCopy}`,
      tone: 'agent',
      safePayload,
    }
  }
  if (event.type === 'business.status') {
    const status = textValue(payload.status, 'unknown')
    return {
      lane: 'Decision / state',
      title: '业务状态决策',
      detail: businessStatusLabel(status),
      tone: status.includes('error') || status.includes('failed') ? 'warning' : 'agent',
      safePayload,
    }
  }
  if (event.type === 'security.notice') {
    return {
      lane: 'Guardrail',
      title: '安全边界已生效',
      detail: textValue(payload.message, '已拦截不允许的内部信息请求'),
      tone: 'warning',
      safePayload,
    }
  }
  if (event.type === 'error.recoverable') {
    return {
      lane: 'Recoverable error',
      title: '本轮未完成',
      detail: textValue(payload.message, '可以安全重试'),
      tone: 'error',
      safePayload,
    }
  }
  if (event.type === 'turn.interrupted') {
    const retryable = payload.retryable !== false
    return {
      lane: 'Interrupted',
      title: 'Agent Loop 已中断',
      detail: `${textValue(payload.reason, '客户端取消')}${retryable ? ' · 可重试' : ''}`,
      tone: 'warning',
      safePayload,
    }
  }
  if (event.type === 'message.completed' || event.type === 'message.assistant') {
    return {
      lane: 'Output',
      title: 'Agent 返回',
      detail: textValue(payload.content, '本轮回答已完成'),
      tone: 'output',
      safePayload,
    }
  }
  return null
}

function groupTurns(events: WorkspaceEvent[]): TurnGroup[] {
  const groups = new Map<string, WorkspaceEvent[]>()
  for (const event of [...events].sort((left, right) => left.id - right.id)) {
    if (!supportedEventTypes.has(event.type)) continue
    const turnId = event.payload.turn_id
    if (typeof turnId !== 'string' || !turnId.trim()) continue
    const current = groups.get(turnId) ?? []
    current.push(event)
    groups.set(turnId, current)
  }
  return [...groups.entries()]
    .map(([turnId, turnEvents]) => ({ turnId, events: turnEvents }))
    .slice(-3)
}

function visibleEvents(group: TurnGroup): WorkspaceEvent[] {
  const hasCompletedMessage = group.events.some((event) => event.type === 'message.completed')
  return group.events.filter((event) => (
    event.type !== 'message.assistant' || !hasCompletedMessage
  ))
}

function shortTurnId(turnId: string): string {
  return turnId.length <= 14 ? turnId : `${turnId.slice(0, 8)}…${turnId.slice(-4)}`
}

export function AgentTrajectory({ events }: AgentTrajectoryProps) {
  const turns = groupTurns(events)

  return (
    <section className="agent-trajectory" aria-labelledby="agent-trajectory-title">
      <header className="agent-trajectory-header">
        <div>
          <p className="agent-trajectory-eyebrow">AGENT LOOP · READ ONLY</p>
          <h2 id="agent-trajectory-title">Agent 运行轨迹</h2>
          <p>只读回放 PostgreSQL 持久化的安全事件；不包含隐藏推理、系统提示词或凭证。</p>
        </div>
        <span className="agent-trajectory-mode">当前主编排器是 ConversationService，不是 LangGraph</span>
      </header>

      {turns.length === 0 ? (
        <div className="agent-trajectory-empty" role="status">
          <strong>还没有可回放的 Agent 轨迹</strong>
          <p>发出一条消息后，这里会按真实事件展示 Router、RAG/工具、状态和输出。</p>
        </div>
      ) : (
        <div className="agent-trajectory-turns">
          {turns.map((turn, turnIndex) => {
            const steps = visibleEvents(turn)
              .map((event) => ({ event, copy: stepCopy(event) }))
              .filter((item): item is { event: WorkspaceEvent; copy: StepCopy } => item.copy !== null)
            return (
              <details
                className="agent-trajectory-turn"
                data-testid="agent-trajectory-turn"
                key={turn.turnId}
                open={turnIndex === turns.length - 1}
              >
                <summary>
                  <span>Turn {String(turnIndex + 1).padStart(2, '0')}</span>
                  <strong>{shortTurnId(turn.turnId)}</strong>
                  <small>{steps.length} 个安全步骤</small>
                </summary>
                <ol className="agent-trajectory-loop" aria-label={`${turn.turnId} Agent Loop`}>
                  {steps.map(({ event, copy }) => (
                    <li className={`agent-trajectory-step is-${copy.tone}`} key={event.id}>
                      <span className="agent-trajectory-rail" aria-hidden="true" />
                      <div className="agent-trajectory-step-card">
                        <div className="agent-trajectory-step-heading">
                          <span>{copy.lane}</span>
                          <small>事件 #{event.id}</small>
                        </div>
                        <strong>{copy.title}</strong>
                        <p>{copy.detail}</p>
                        <details className="agent-trajectory-payload">
                          <summary>安全事件详情</summary>
                          <pre>{JSON.stringify(copy.safePayload, null, 2)}</pre>
                        </details>
                      </div>
                    </li>
                  ))}
                </ol>
              </details>
            )
          })}
        </div>
      )}
    </section>
  )
}
