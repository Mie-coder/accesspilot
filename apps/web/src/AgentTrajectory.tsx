import { useState } from 'react'
import type { ConnectionState, WorkspaceEvent } from './types'

interface AgentTrajectoryProps {
  events: WorkspaceEvent[]
  connectionState?: ConnectionState
  isRunning?: boolean
}

type StepTone = 'neutral' | 'input' | 'agent' | 'model' | 'rag' | 'tool' | 'output' | 'warning' | 'error'

interface StepCopy {
  lane: string
  title: string
  detail: string
  tone: StepTone
  safePayload: Record<string, unknown>
}

interface TurnGroup {
  turnId: string
  events: WorkspaceEvent[]
  ignoredUnknownCount: number
  maxEventId: number
}

interface EngineCopy {
  label: string
  tone: 'langgraph' | 'legacy' | 'unknown'
}

interface StatusCopy {
  label: string
  tone: 'running' | 'waiting' | 'completed' | 'error' | 'interrupted' | 'unknown'
}

const supportedEventTypes = new Set([
  'turn.started',
  'message.user',
  'intent.detected',
  'agent.node.started',
  'agent.node.completed',
  'agent.route.selected',
  'model.started',
  'model.completed',
  'retrieval.started',
  'retrieval.completed',
  'tool.started',
  'tool.completed',
  'tool.summary',
  'draft.updated',
  'business.status',
  'agent.input.required',
  'agent.input.resumed',
  'security.notice',
  'error.recoverable',
  'turn.interrupted',
  'message.assistant',
  'message.completed',
])

function textValue(value: unknown, fallback: string): string {
  return typeof value === 'string' && value.trim() ? value : fallback
}

function optionalText(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value : null
}

function optionalNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
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
    awaiting_confirmation: '申请完整，等待申请人确认',
    ready_to_submit: '申请人已确认，可以创建正式申请',
    submitted: '正式申请已创建',
    answered: '只读问题已回答',
    needs_clarification: '需要补充上下文',
    validation_failed: '申请校验未通过',
    recoverable_error: '本轮可安全重试',
  }
  return labels[status] ?? status
}

function modelPurpose(operation: unknown): string {
  if (operation === 'route_intent') return '意图理解'
  if (operation === 'parse_input') return '申请字段提取'
  return '模型处理'
}

function recordedModelUsage(turn: TurnGroup): Map<string, number> | null {
  if (!turn.events.some((event) => event.type === 'turn.started'
    && event.payload.model_usage_recorded === true)) return null
  const attempts = new Set<string>()
  const counts = new Map<string, number>()
  for (const event of turn.events) {
    if (!['model.started', 'model.completed'].includes(event.type)
      || event.payload.provider_mode !== 'api') continue
    const step = optionalText(event.payload.step_id)
    const attempt = optionalNumber(event.payload.attempt)
    if (step === null || attempt === null) return null
    const identity = `${step}:${attempt}`
    if (attempts.has(identity)) continue
    attempts.add(identity)
    const purpose = modelPurpose(event.payload.operation)
    counts.set(purpose, (counts.get(purpose) ?? 0) + 1)
  }
  return counts
}

function toolPurpose(tool: string): string {
  const names: Record<string, string> = {
    list_eligible_access: '查询可申请权限',
    list_active_access: '查询已有权限',
    get_latest_request_status: '查询申请进度',
    resolve_entitlement: '匹配权限名称',
    search_policies: '检索政策',
    list_policy_catalog: '查询政策目录',
    get_self_approval_policy: '查询审批规则',
    validate_access_request: '校验申请条件',
  }
  return names[tool] ?? '执行后端工具'
}

function safeEventPayload(event: WorkspaceEvent): Record<string, unknown> {
  const payload = event.payload
  const safe: Record<string, unknown> = {
    event_id: event.id,
    event_type: event.type,
  }
  const addText = (key: string) => {
    const value = optionalText(payload[key])
    if (value !== null) safe[key] = value
  }
  const addNumber = (key: string) => {
    const value = optionalNumber(payload[key])
    if (value !== null) safe[key] = value
  }
  const addBoolean = (key: string) => {
    const value = booleanValue(payload[key])
    if (value !== null) safe[key] = value
  }
  const addStringList = (key: string) => {
    if (Array.isArray(payload[key])) safe[key] = stringList(payload[key])
  }

  switch (event.type) {
    case 'turn.started':
      addText('orchestrator')
      addNumber('flow_version')
      addText('graph_version')
      addBoolean('model_usage_recorded')
      break
    case 'message.user':
    case 'message.assistant':
      addText('content')
      break
    case 'message.completed':
      addText('content')
      addText('business_status')
      addNumber('draft_revision')
      addText('error_code')
      break
    case 'intent.detected':
      addText('intent')
      addBoolean('security_flagged')
      break
    case 'agent.node.started':
    case 'agent.node.completed':
      addText('node_code')
      addText('public_label')
      addText('status')
      break
    case 'agent.route.selected':
      addText('route_code')
      break
    case 'model.started':
    case 'model.completed':
      addText('operation')
      addText('provider_mode')
      addNumber('attempt')
      if (event.type === 'model.completed') {
        addText('status')
        addStringList('extracted_fields')
      }
      break
    case 'retrieval.started':
    case 'retrieval.completed':
      addText('retriever')
      if (event.type === 'retrieval.completed') {
        addText('status')
        addNumber('match_count')
        addStringList('evidence_codes')
      }
      break
    case 'tool.started':
      addText('tool')
      break
    case 'tool.completed':
    case 'tool.summary':
      addText('tool')
      addText('status')
      addText('summary')
      break
    case 'draft.updated':
      addNumber('draft_revision')
      addStringList('missing_fields')
      addBoolean('can_enter_approval')
      break
    case 'business.status':
      addText('status')
      addText('request_id')
      break
    case 'agent.input.required':
      addText('kind')
      addNumber('draft_revision')
      break
    case 'agent.input.resumed':
      addText('kind')
      addText('decision')
      break
    case 'security.notice':
      addText('code')
      addText('message')
      break
    case 'error.recoverable':
      addText('code')
      addText('message')
      addText('business_status')
      addNumber('draft_revision')
      addText('error_code')
      break
    case 'turn.interrupted':
      addText('reason')
      addBoolean('retryable')
      break
  }
  return safe
}

function modelLabel(providerMode: unknown): string {
  if (providerMode === 'api') return 'DeepSeek Parser'
  if (providerMode === 'mock') return 'Offline Parser'
  return 'Unknown Parser'
}

function stepCopy(event: WorkspaceEvent): StepCopy | null {
  const payload = event.payload
  const safePayload = safeEventPayload(event)

  if (event.type === 'turn.started') {
    const orchestrator = optionalText(payload.orchestrator)
    const detail = orchestrator === 'langgraph'
      ? `LangGraph 启动 Flow ${optionalNumber(payload.flow_version) ?? '未知'} · ${textValue(payload.graph_version, 'graph version 未知')}`
      : orchestrator === null || orchestrator === 'legacy'
        ? 'ConversationService 启动 Legacy 对话轮'
        : `已记录未识别的编排器 ${orchestrator}`
    return {
      lane: 'Orchestrator',
      title: 'Agent Loop 开始',
      detail,
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
      title: '意图识别结果',
      detail: `意图：${intent}${securityFlagged ? ' · 命中安全检查' : ''}`,
      tone: securityFlagged ? 'warning' : 'agent',
      safePayload,
    }
  }
  if (event.type === 'agent.node.started' || event.type === 'agent.node.completed') {
    const label = textValue(payload.public_label, 'Agent 节点')
    const completed = event.type === 'agent.node.completed'
    const status = textValue(payload.status, completed ? 'unknown' : 'running')
    return {
      lane: 'Node',
      title: `${label}${completed ? '完成' : '开始'}`,
      detail: completed ? `执行状态：${status}` : '真实节点边界已进入',
      tone: status === 'error' ? 'error' : 'agent',
      safePayload,
    }
  }
  if (event.type === 'agent.route.selected') {
    const route = textValue(payload.route_code, 'unknown')
    return {
      lane: 'Router',
      title: '选择执行分支',
      detail: `已选择分支 ${route}`,
      tone: 'agent',
      safePayload,
    }
  }
  if (event.type === 'model.started' || event.type === 'model.completed') {
    const completed = event.type === 'model.completed'
    const parser = modelLabel(payload.provider_mode)
    const attempt = optionalNumber(payload.attempt)
    const status = textValue(payload.status, completed ? 'unknown' : 'running')
    const fields = stringList(payload.extracted_fields)
    const fieldCopy = fields.length > 0 ? ` · 识别字段：${fields.join('、')}` : ''
    return {
      lane: `Model · ${parser === 'DeepSeek Parser' ? 'DeepSeek' : parser === 'Offline Parser' ? 'Offline' : 'Unknown'}`,
      title: payload.operation === 'route_intent'
        ? `意图理解${completed ? '完成' : '开始'}`
        : completed ? '结构化解析完成' : '结构化解析开始',
      detail: `${parser} · 第 ${attempt ?? '未知'} 次 · ${status}${fieldCopy}`,
      tone: status === 'malformed' || status === 'unavailable' ? 'warning' : 'model',
      safePayload,
    }
  }
  if (event.type === 'retrieval.started' || event.type === 'retrieval.completed') {
    if (payload.retriever !== 'pgvector') return null
    const completed = event.type === 'retrieval.completed'
    const status = textValue(payload.status, completed ? 'unknown' : 'running')
    const matchCount = optionalNumber(payload.match_count)
    const codes = stringList(payload.evidence_codes)
    const result = completed
      ? `状态：${status} · 命中 ${matchCount ?? 0} 条${codes.length > 0 ? ` · ${codes.join('、')}` : ''}`
      : '真实 search_policies pgvector 路径已开始'
    return {
      lane: 'RAG · pgvector',
      title: completed ? '政策检索完成' : '政策检索开始',
      detail: result,
      tone: status === 'unavailable' ? 'warning' : 'rag',
      safePayload,
    }
  }
  if (event.type === 'tool.started' || event.type === 'tool.completed') {
    const tool = textValue(payload.tool, 'unknown_tool')
    const completed = event.type === 'tool.completed'
    const status = textValue(payload.status, completed ? 'unknown' : 'running')
    const summary = optionalText(payload.summary)
    return {
      lane: `Read-only tool · ${tool}`,
      title: `${tool} ${completed ? '完成' : '开始'}`,
      detail: `${toolPurpose(tool)} · 后端工具执行 · ${completed
        ? `${summary ?? '工具已完成'} · 状态：${status}` : '执行中'}`,
      tone: status === 'error' ? 'warning' : 'tool',
      safePayload,
    }
  }
  if (event.type === 'tool.summary') {
    const tool = textValue(payload.tool, 'unknown_tool')
    const status = textValue(payload.status, 'unknown')
    return {
      lane: `Legacy tool · ${tool}`,
      title: `${tool} Legacy 摘要`,
      detail: `${toolPurpose(tool)} · 后端工具执行 · ${textValue(payload.summary, '工具已完成')} · 状态：${status}`,
      tone: 'tool',
      safePayload,
    }
  }
  if (event.type === 'draft.updated') {
    const revision = optionalNumber(payload.draft_revision)
    const missingFields = stringList(payload.missing_fields)
    const missingCopy = missingFields.length > 0
      ? `仍缺：${missingFields.join('、')}`
      : '申请字段已完整'
    return {
      lane: 'State',
      title: '草稿状态更新',
      detail: `${revision === null ? '' : `修订 #${revision} · `}${missingCopy}`,
      tone: 'agent',
      safePayload,
    }
  }
  if (event.type === 'business.status') {
    const status = textValue(payload.status, 'unknown')
    return {
      lane: 'State',
      title: '业务状态更新',
      detail: businessStatusLabel(status),
      tone: status.includes('error') || status.includes('failed') ? 'warning' : 'agent',
      safePayload,
    }
  }
  if (event.type === 'agent.input.required') {
    const revision = optionalNumber(payload.draft_revision)
    return {
      lane: 'HITL',
      title: '等待申请人确认',
      detail: `${revision === null ? '' : `草稿修订 #${revision} · `}已持久化确认等待事实`,
      tone: 'warning',
      safePayload,
    }
  }
  if (event.type === 'agent.input.resumed') {
    const decision = textValue(payload.decision, 'unknown')
    return {
      lane: 'HITL',
      title: '申请人输入已恢复',
      detail: `已按 ${decision} 恢复执行`,
      tone: 'agent',
      safePayload,
    }
  }
  if (event.type === 'security.notice') {
    return {
      lane: 'Guardrail',
      title: '安全边界已生效',
      detail: textValue(payload.message, '已拒绝不允许的内部信息请求'),
      tone: 'warning',
      safePayload,
    }
  }
  if (event.type === 'error.recoverable') {
    return {
      lane: 'Recoverable error',
      title: '本轮遇到可恢复错误',
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
      detail: `${textValue(payload.reason, '客户端取消')}${retryable ? ' · 可重新发送' : ''}`,
      tone: 'warning',
      safePayload,
    }
  }
  if (event.type === 'message.completed' || event.type === 'message.assistant') {
    return {
      lane: 'Output',
      title: event.type === 'message.completed' ? '本轮输出完成' : '助手输出',
      detail: textValue(payload.content, '本轮回答已完成'),
      tone: 'output',
      safePayload,
    }
  }
  return null
}

function groupTurns(events: WorkspaceEvent[]): TurnGroup[] {
  const groups = new Map<string, WorkspaceEvent[]>()
  const unknownCounts = new Map<string, number>()
  const ordered = [...new Map(events.map((event) => [event.id, event])).values()]
    .filter((event) => Number.isSafeInteger(event.id) && event.id >= 0)
    .sort((left, right) => left.id - right.id)

  for (const event of ordered) {
    const turnId = optionalText(event.payload.turn_id)
    if (turnId === null) continue
    if (!supportedEventTypes.has(event.type)) {
      unknownCounts.set(turnId, (unknownCounts.get(turnId) ?? 0) + 1)
      continue
    }
    const current = groups.get(turnId) ?? []
    current.push(event)
    groups.set(turnId, current)
  }

  return [...groups.entries()]
    .map(([turnId, turnEvents]) => ({
      turnId,
      events: turnEvents,
      ignoredUnknownCount: unknownCounts.get(turnId) ?? 0,
      maxEventId: turnEvents.at(-1)?.id ?? -1,
    }))
    .sort((left, right) => left.maxEventId - right.maxEventId)

}

function visibleEvents(group: TurnGroup): WorkspaceEvent[] {
  const hasCompletedMessage = group.events.some((event) => event.type === 'message.completed')
  return group.events.filter((event) => (
    event.type !== 'message.assistant' || !hasCompletedMessage
  ))
}

function engineCopy(group: TurnGroup): EngineCopy {
  const started = group.events.find((event) => event.type === 'turn.started')
  if (started === undefined) return { label: 'Unknown · 缺少编排器事实', tone: 'unknown' }
  const orchestrator = optionalText(started.payload.orchestrator)
  if (orchestrator === null) return { label: 'Legacy · ConversationService', tone: 'legacy' }
  if (orchestrator.toLowerCase() === 'langgraph') {
    const flow = optionalNumber(started.payload.flow_version)
    const graph = optionalText(started.payload.graph_version)
    return {
      label: `LangGraph · Flow ${flow ?? '未知'} · ${graph ?? 'graph version 未知'}`,
      tone: 'langgraph',
    }
  }
  if (['conversationservice', 'conversation_service', 'legacy'].includes(orchestrator.toLowerCase())) {
    return { label: 'Legacy · ConversationService', tone: 'legacy' }
  }
  return { label: `Unknown · ${orchestrator}`, tone: 'unknown' }
}

function turnStatus(group: TurnGroup, isCurrentRunning: boolean): StatusCopy {
  const lastTerminal = [...group.events].reverse().find((event) => (
    event.type === 'message.completed'
    || event.type === 'error.recoverable'
    || event.type === 'turn.interrupted'
  ))
  if (lastTerminal?.type === 'message.completed') return { label: '完成', tone: 'completed' }
  if (lastTerminal?.type === 'error.recoverable') return { label: '可恢复错误', tone: 'error' }
  if (lastTerminal?.type === 'turn.interrupted') return { label: '已中断', tone: 'interrupted' }

  let lastRequired: WorkspaceEvent | undefined
  let lastResumed: WorkspaceEvent | undefined
  for (const event of group.events) {
    if (event.type === 'agent.input.required') lastRequired = event
    if (event.type === 'agent.input.resumed') lastResumed = event
  }
  if (lastRequired !== undefined && (lastResumed === undefined || lastRequired.id > lastResumed.id)) {
    return { label: '等待 HITL', tone: 'waiting' }
  }
  const hasRunningFact = group.events.some((event) => (
    event.type === 'turn.started'
    || event.type === 'agent.node.started'
    || event.type === 'model.started'
    || event.type === 'retrieval.started'
    || event.type === 'tool.started'
  ))
  if (isCurrentRunning || hasRunningFact) return { label: '运行中', tone: 'running' }
  return { label: '状态未知', tone: 'unknown' }
}

function turnOverview(turn: TurnGroup, previous?: TurnGroup) {
  const input = turn.events.find((event) => event.type === 'message.user')?.payload.content
  const terminal = [...turn.events].reverse().find((event) => (
    event.type === 'message.completed' || event.type === 'message.assistant'
  ))
  const output = terminal?.payload.assistant_message ?? terminal?.payload.content
  const intent = terminal?.payload.intent ?? turn.events.find((event) => event.type === 'intent.detected')?.payload.intent
  const labels: Record<string, string> = {
    discover_eligible_access: '查询可申请权限', list_active_access: '查询已有权限',
    policy_question: '咨询权限政策', request_status: '查询申请进度',
  }
  let title = typeof intent === 'string' ? labels[intent] : undefined
  if (intent === 'request_access') {
    const updated = turn.events.find((event) => event.type === 'draft.updated')
    const previousUpdate = [...(previous?.events ?? [])].reverse().find((event) => event.type === 'draft.updated')
    const previousMissing = stringList(previousUpdate?.payload.missing_fields)
    const missing = stringList(updated?.payload.missing_fields)
    if (terminal?.payload.business_status === 'ready_to_submit') title = '确认申请信息'
    else if (turn.events.some((event) => ['tool.summary', 'tool.completed'].includes(event.type)
      && event.payload.tool === 'resolve_entitlement'
      && ['matched', 'success'].includes(String(event.payload.status)))) title = '选择申请权限'
    else if (updated && previousMissing[0] === 'duration_days' && !missing.includes('duration_days')) title = '补充申请期限'
    else if (updated && previousMissing[0] === 'justification' && !missing.includes('justification')) title = '补充申请理由'
  }
  const userText = textValue(input, '用户原话未记录')
  return {
    title: title ?? (typeof input === 'string' ? `对话：${input.replace(/\s+/g, ' ').slice(0, 36)}` : '对话记录'),
    input: userText,
    output: textValue(output, '处理结果尚未记录'),
  }
}

export function AgentTrajectory({
  events,
  connectionState = 'connected',
  isRunning = false,
}: AgentTrajectoryProps) {
  const [visibleCount, setVisibleCount] = useState(3)
  const allTurns = groupTurns(events)
  const turns = allTurns.slice(-visibleCount)

  return (
    <section
      className="agent-trajectory"
      aria-label="Agent 运行轨迹"
      aria-labelledby="agent-trajectory-title"
    >
      <header className="agent-trajectory-header">
        <div>
          <p className="agent-trajectory-eyebrow">AGENT LOOP · READ ONLY</p>
          <h2 id="agent-trajectory-title">Agent 运行轨迹</h2>
          <p>同一会话的聊天轮次，按事件顺序回看；每轮对话不代表一张工单。</p>
        </div>
        <span className="agent-trajectory-mode">只读 · 不提供运行操作</span>
      </header>

      <p className="agent-trajectory-boundary">这是执行事实的只读投影，不是模型思维链。
      </p>

      {connectionState === 'reconnecting' ? (
        <div className="agent-trajectory-connection" role="status" aria-live="polite">
          <strong>轨迹加载中</strong>
          <span>活动流正在重连；已展示的持久化事件保持只读。</span>
        </div>
      ) : null}

      {turns.length === 0 ? (
        <div
          className="agent-trajectory-empty"
          role={connectionState === 'reconnecting' ? undefined : 'status'}
        >
          <strong>还没有可展示的运行轨迹</strong>
          <p>发出一条消息后，这里会按真实事件展示节点、RAG/工具、State、HITL 和终态。</p>
        </div>
      ) : (
        <div className="agent-trajectory-turns">
          {allTurns.length > turns.length ? <button className="agent-trajectory-history" type="button"
            onClick={() => setVisibleCount((count) => count + 3)}>
            查看更早轮次（还有 {allTurns.length - turns.length} 轮）
          </button> : null}
          {turns.map((turn, turnIndex) => {
            const steps = visibleEvents(turn)
              .map((currentEvent) => ({ event: currentEvent, copy: stepCopy(currentEvent) }))
              .filter((item): item is { event: WorkspaceEvent; copy: StepCopy } => item.copy !== null)
            const isNewest = turnIndex === turns.length - 1
            const overview = turnOverview(turn, allTurns[allTurns.length - turns.length + turnIndex - 1])
            const engine = engineCopy(turn)
            const status = turnStatus(turn, isNewest && isRunning)
            const usage = recordedModelUsage(turn)
            const modelCalls = usage === null ? null : [...usage.values()].reduce((a, b) => a + b, 0)
            return (
              <details
                className="agent-trajectory-turn"
                data-testid="agent-trajectory-turn"
                data-turn-id={turn.turnId}
                key={turn.turnId}
                open={isNewest}
              >
                <summary>
                  <span className="agent-trajectory-turn-index">
                    {isNewest ? '最新 · 聊天' : '聊天'}
                  </span>
                  <strong>{overview.title}</strong>
                  <span className={`agent-trajectory-status is-${status.tone}`}>{status.label}</span>
                  <small>{steps.length} 个安全步骤</small>
                </summary>
                <div className="agent-trajectory-overview">
                  <p>你说：{overview.input}</p>
                  <p>处理结果：{overview.output}</p>
                </div>
                <div className="agent-trajectory-usage" role="note" aria-label="本轮调用记录">
                  <strong>{modelCalls === null ? '模型调用次数未记录' : `本轮模型调用 ${modelCalls} 次`}</strong>
                  {modelCalls === 0 ? <p>规则处理：未调用模型</p> : null}
                  {usage === null ? <p>此轮没有完整计量标记，不能据此认定为零调用。</p>
                    : [...usage].map(([purpose, count]) => <p key={purpose}>{purpose}：调用模型 {count} 次</p>)}
                  <small>按已记录的调用尝试计数，包含失败和重试；恢复重放不重复计数。工具由后端执行。</small>
                </div>
                <details className="agent-trajectory-technical">
                  <summary>技术详情 · {steps.length} 个安全步骤</summary>
                  <p>轮次编号：{turn.turnId}</p>
                  <span className={`agent-trajectory-engine is-${engine.tone}`}>{engine.label}</span>
                {turn.ignoredUnknownCount > 0 ? (
                  <p className="agent-trajectory-unknown" role="note">
                    {turn.ignoredUnknownCount} 个未知事件已安全忽略
                  </p>
                ) : null}
                <ol className="agent-trajectory-loop" aria-label={`${turn.turnId} Agent Loop`}>
                  {steps.map(({ event: currentEvent, copy }) => (
                    <li
                      className={`agent-trajectory-step is-${copy.tone}`}
                      data-event-id={currentEvent.id}
                      key={currentEvent.id}
                    >
                      <span className="agent-trajectory-rail" aria-hidden="true" />
                      <div className="agent-trajectory-step-card">
                        <div className="agent-trajectory-step-heading">
                          <span>{copy.lane}</span>
                          <small>事件 #{currentEvent.id}</small>
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
              </details>
            )
          })}
        </div>
      )}
    </section>
  )
}
