import { AlertCircle, CheckCircle2, Clock3, FileCheck2, History, LoaderCircle, RefreshCw } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'

import { readMyRequests, readRequestDetail } from './api'
import { DecisionPacketPanel } from './DecisionPacketPanel'
import type { CaseSummary, RequestDetail, RequestDraft } from './types'

export interface RequestTimelineViewProps {
  detail: RequestDetail | null
  draft: RequestDraft | null
  loading: boolean
  error: string | null
  onRetry: () => void
  cases?: CaseSummary[]
  selectedRequestId?: string | null
  onSelectRequest?: (requestId: string) => void
}

interface TimelineEntry {
  id: string
  label: string
  time: string | null
  owner: string | null
  nextStep: string | null
  status: string | null
  tone: 'done' | 'pending' | 'unknown' | 'danger'
  order?: number
}

const roleLabels: Record<string, string> = {
  manager: '直属经理',
  data_owner: '数据所有者',
  security: '安全负责人',
  risk_agent: '风险审查',
}

const statusLabels: Record<string, string> = {
  draft: '草稿已保存',
  submitted: '已提交',
  approved: '已通过',
  rejected: '已驳回',
  denied: '已拒绝',
  cancelled: '已取消',
  canceled: '已取消',
  pending: '待审批',
  pending_manager: '待经理审批',
  pending_data_owner: '待数据负责人审批',
  waiting: '等待前序',
  manager_approved: '已通过',
  data_owner_approved: '已通过',
  risk_review: '已完成',
  requires_human_review: '待人工审查',
  provisioning: '开通中',
  processing: '处理中',
  retrying: '重试中',
  unknown: '状态未知',
  succeeded: '已成功',
  failed: '失败',
}

const eventRanks: Record<string, number> = {
  'draft.updated': 0,
  'request.submitted': 1,
  'risk_review.completed': 2,
  'approval.step.approved': 3,
  'approval.step.rejected': 3,
  'provisioning.started': 5,
  'provisioning.unknown': 6,
  'provisioning.reconciled': 7,
  'provisioning.retry_started': 7,
  'provisioning.failed': 8,
  'provisioning.succeeded': 9,
}

function safeMessage(message: string): string {
  return message
    .replace(/system_prompt|system prompt|sk-[a-z0-9_-]+/gi, '受保护内容')
    .replace(/chain[-_ ]of[-_ ]thought|traceback/gi, '受保护内容')
}

function formatTime(value: string | null): string {
  if (!value) return '尚未发生'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  }).format(date)
}

function statusTone(status: string | null, eventType = ''): TimelineEntry['tone'] {
  if (eventType === 'provisioning.unknown') return 'unknown'
  if (eventType === 'provisioning.failed' || eventType === 'approval.step.rejected') return 'danger'
  if (eventType === 'provisioning.succeeded' || eventType === 'provisioning.reconciled') return 'done'
  if (eventType === 'provisioning.started' || eventType === 'provisioning.retry_started') return 'pending'
  if (!status) return 'pending'
  const value = status.toLowerCase()
  if (value.includes('reject') || value.includes('fail') || value.includes('denied') || value.includes('cancel')) return 'danger'
  if (value.includes('unknown')) return 'unknown'
  if (value.includes('pending') || value.includes('waiting') || value === 'not_started') return 'pending'
  return 'done'
}

function statusLabel(status: string | null): string | null {
  if (!status) return null
  return statusLabels[status.toLowerCase()] ?? status
}

function eventEntry(event: RequestDetail['audit_events'][number], order = 0): TimelineEntry | null {
  const rank = eventRanks[event.event_type]
  if (rank === undefined) return null
  const details = event.details
  const nextStep = typeof details.next_step === 'string' ? details.next_step : null
  const status = typeof details.status === 'string' ? details.status : null
  let label: string
  let tone = statusTone(status, event.event_type)
  let resolvedNextStep = nextStep
  switch (event.event_type) {
    case 'draft.updated':
      label = '草稿'
      break
    case 'request.submitted':
      label = '已提交'
      break
    case 'risk_review.completed':
      label = '风险审查'
      break
    case 'approval.step.approved':
    case 'approval.step.rejected': {
      const role = typeof details.approver_role === 'string' ? details.approver_role : event.actor_type
      label = roleLabels[role] ?? role
      break
    }
    case 'provisioning.started':
    case 'provisioning.retry_started':
      label = '开通中'
      resolvedNextStep ??= event.event_type === 'provisioning.retry_started' ? '等待 IAM 重试' : '等待 IAM'
      break
    case 'provisioning.unknown':
      label = '状态未知'
      tone = 'unknown'
      resolvedNextStep ??= '查询原 IAM 操作'
      break
    case 'provisioning.reconciled':
      label = '恢复'
      resolvedNextStep ??= '确认授权事实'
      break
    case 'provisioning.failed':
      label = '开通失败'
      tone = 'danger'
      resolvedNextStep ??= '联系人工流程后重试'
      break
    case 'provisioning.succeeded':
      label = '已授权'
      resolvedNextStep ??= '权限已授权'
      break
    default:
      return null
  }
  return {
    id: event.audit_event_id,
    label,
    time: event.created_at,
    owner: event.actor_id ?? (event.actor_type === 'risk_agent' ? 'risk_agent' : null),
    nextStep: resolvedNextStep,
    status,
    tone,
    order,
  }
}

function buildEntries(detail: RequestDetail | null): TimelineEntry[] {
  // A private Workspace draft has no stable relation to an older formal Case.
  // Only audit facts already attached to this Request may appear in replay.
  if (!detail) return []
  const entries: TimelineEntry[] = []
  const auditEntries = detail?.audit_events
    .map((event, index) => eventEntry(event, index))
    .filter((entry): entry is TimelineEntry => entry !== null) ?? []
  const eventByType = new Map(detail?.audit_events.map((event) => [event.event_type, event]) ?? [])

  const draftEvent = eventByType.get('draft.updated')
  const includeDraft = draftEvent !== undefined
  const requester = detail?.request.requester_name
    ? `${detail.request.requester_name} · ${detail.request.requester_id}`
    : detail?.request.requester_id
  if (includeDraft) {
    entries.push({
      id: 'draft',
      label: '草稿',
      time: draftEvent?.created_at ?? detail?.request.created_at ?? null,
      owner: requester ?? null,
      nextStep: draftEvent && typeof draftEvent.details.next_step === 'string'
        ? draftEvent.details.next_step
        : detail ? '继续核对申请字段和确认状态' : '补充权限、期限和业务理由',
      status: draftEvent && typeof draftEvent.details.status === 'string' ? draftEvent.details.status : 'draft',
      tone: 'done',
      order: -1,
    })
  }

  if (detail) {
    const submittedEvent = eventByType.get('request.submitted')
    if (submittedEvent || detail.request.request_status !== 'draft') {
      entries.push({
        id: 'submitted',
        label: '已提交',
        time: submittedEvent?.created_at ?? detail.request.created_at,
        owner: requester ?? null,
        nextStep: ['rejected', 'denied'].includes(detail.request.request_status.toLowerCase())
          ? '申请已驳回'
          : ['cancelled', 'canceled'].includes(detail.request.request_status.toLowerCase())
            ? '申请已取消'
            : submittedEvent && typeof submittedEvent.details.next_step === 'string'
              ? submittedEvent.details.next_step
              : '等待风险审查',
        status: detail.request.request_status,
        tone: statusTone(detail.request.request_status),
        order: -1,
      })
    }

    const riskEvent = eventByType.get('risk_review.completed')
    if (detail.risk_review || riskEvent) {
      entries.push({
        id: 'risk-review',
        label: '风险审查',
        time: riskEvent?.created_at ?? null,
        owner: riskEvent?.actor_id ?? 'risk_agent',
        nextStep: riskEvent && typeof riskEvent.details.next_step === 'string'
          ? riskEvent.details.next_step
          : detail.risk_review?.summary ?? '等待人工审批',
        status: detail.risk_review?.outcome ?? null,
        tone: statusTone(detail.risk_review?.outcome ?? null),
        order: -1,
      })
    }

    if (detail.approval?.steps) {
      const orderedSteps = [...detail.approval.steps].sort((left, right) => left.step_order - right.step_order)
      for (const [stepIndex, step] of orderedSteps.entries()) {
        const role = roleLabels[step.approver_role] ?? step.approver_role
        const matchingEvent = detail.audit_events.find(
          (event) => event.event_type.startsWith('approval.step.') && event.actor_id === step.approver_id,
        )
        const stepStatus = step.step_status.toLowerCase()
        const stepIsRejected = ['rejected', 'denied'].includes(stepStatus)
        const stepIsCancelled = ['cancelled', 'canceled'].includes(stepStatus)
        const isLastStep = stepIndex === orderedSteps.length - 1
        entries.push({
          id: step.step_id,
          label: role,
          time: step.decided_at ?? matchingEvent?.created_at ?? null,
          owner: step.approver_id,
          nextStep: stepIsRejected
            ? '申请已驳回'
            : stepIsCancelled
              ? '前序驳回，本步骤已取消'
              : matchingEvent && typeof matchingEvent.details.next_step === 'string'
                ? matchingEvent.details.next_step
                : step.step_status === 'approved'
                  ? isLastStep ? '审批已通过，等待权限开通' : '等待下一审批步骤'
                  : '等待当前审批决定',
          status: step.step_status,
          tone: statusTone(step.step_status),
          order: -1,
        })
      }
    }

    const provisioning = detail.provisioning
    const provisioningEntries = auditEntries.filter((entry) =>
      ['开通中', '状态未知', '恢复', '开通失败', '已授权'].includes(entry.label),
    )
    if (provisioningEntries.length > 0) {
      // The audit stream is authoritative. A succeeded event without an access
      // grant must not be promoted to an "已授权" card.
      let uncertain = false
      let recoveryShown = false
      for (const entry of provisioningEntries.sort((left, right) => (left.order ?? 0) - (right.order ?? 0))) {
        if (entry.label === '状态未知' || entry.label === '开通失败') {
          uncertain = true
          recoveryShown = false
        }
        if (entry.label === '恢复') {
          recoveryShown = true
          uncertain = false
          entries.push(entry)
          continue
        }
        if (entry.label === '开通中' && uncertain) {
          // A retry_started event is a real recovery transition even when the
          // backend does not emit a separate provisioning.reconciled event.
          entries.push({
            ...entry,
            id: `${entry.id}-recovery`,
            label: '恢复',
            nextStep: entry.nextStep ?? '重试开通',
            tone: 'done',
            order: (entry.order ?? 0) - 0.1,
          })
          recoveryShown = true
          uncertain = false
        }
        if (entry.label === '已授权' && uncertain && !recoveryShown) {
          entries.push({
            ...entry,
            id: `${entry.id}-recovery`,
            label: '恢复',
            nextStep: '授权事实已恢复，继续核对授权状态',
            tone: 'done',
            order: (entry.order ?? 0) - 0.1,
          })
          recoveryShown = true
          uncertain = false
        }
        if (entry.label !== '已授权' || provisioning.access_granted) entries.push(entry)
        if (entry.label === '已授权' && provisioning.access_granted) uncertain = false
      }
      const hasAuthorizedTerminal = entries.some((entry) => entry.label === '已授权')
      if (provisioning.access_granted && !hasAuthorizedTerminal) {
        const recovery = [...provisioningEntries].reverse().find((entry) => entry.label === '恢复')
        entries.push({
          id: `${recovery?.id ?? 'provisioning'}-authorized`,
          label: '已授权',
          time: provisioning.updated_at ?? recovery?.time ?? null,
          owner: null,
          nextStep: '权限已授权',
          status: 'succeeded',
          tone: 'done',
          order: (recovery?.order ?? provisioningEntries.at(-1)?.order ?? 0) + 0.1,
        })
      }
    } else if (provisioning.provisioning_status === 'unknown') {
      entries.push({
        id: 'provisioning-unknown',
        label: '状态未知',
        time: provisioning.updated_at,
        owner: null,
        nextStep: provisioning.last_error ?? '查询原 IAM 操作',
        status: 'unknown',
        tone: 'unknown',
      })
    } else if (provisioning.provisioning_status === 'succeeded' && provisioning.access_granted) {
      entries.push({
        id: 'provisioning-succeeded',
        label: '已授权',
        time: provisioning.updated_at,
        owner: null,
        nextStep: '权限已授权',
        status: 'succeeded',
        tone: 'done',
      })
    } else if (['pending', 'not_started', 'processing', 'started'].includes(provisioning.provisioning_status)) {
      entries.push({
        id: 'provisioning-started',
        label: '开通中',
        time: provisioning.updated_at,
        owner: null,
        nextStep: '等待 IAM',
        status: provisioning.provisioning_status,
        tone: 'pending',
      })
    } else if (provisioning.provisioning_status === 'failed') {
      entries.push({
        id: 'provisioning-failed',
        label: '开通失败',
        time: provisioning.updated_at,
        owner: null,
        nextStep: provisioning.last_error ?? '联系人工流程后重试',
        status: 'failed',
        tone: 'danger',
      })
    }
  }

  const rankForEntry = (entry: TimelineEntry): number => {
    if (entry.label === '草稿') return 0
    if (entry.label === '已提交') return 1
    if (entry.label === '风险审查') return 2
    if (entry.label === '直属经理') return 3
    if (entry.label === '数据所有者') return 4
    if (entry.label === '开通中') return 5
    if (entry.label === '状态未知') return 6
    if (entry.label === '恢复') return 7
    if (entry.label === '开通失败') return 8
    return 9
  }
  const isProvisioningEntry = (entry: TimelineEntry) =>
    ['开通中', '状态未知', '恢复', '开通失败', '已授权'].includes(entry.label)
  return entries.sort((left, right) => {
    if (isProvisioningEntry(left) && isProvisioningEntry(right)) {
      return (left.order ?? 0) - (right.order ?? 0)
    }
    return rankForEntry(left) - rankForEntry(right)
  })
}

function grantStatus(grant: { grant_id?: string | null; starts_at?: string | null; expires_at?: string | null }): string {
  if (grant.grant_id === undefined) return '开通状态待核实'
  if (grant.grant_id === null) return '尚未开通'
  const start = Date.parse(grant.starts_at ?? '')
  const end = Date.parse(grant.expires_at ?? '')
  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) return '授权记录待核实'
  const now = Date.now()
  if (start > now) return '授权待生效'
  if (end <= now) return '授权已过期'
  return '权限已开通'
}

export function RequestTimelineView({
  detail,
  loading,
  error,
  onRetry,
  cases,
  selectedRequestId,
  onSelectRequest,
}: RequestTimelineViewProps) {
  const entries = useMemo(() => buildEntries(detail), [detail])

  return (
    <section className="business-card-panel request-timeline-panel" aria-labelledby="request-timeline-title">
      <div className="business-card-heading">
        <div>
          <p className="eyebrow">REQUEST HISTORY</p>
          <h2 id="request-timeline-title">我的申请</h2>
          <p>按时间和责任人回放已持久化的申请、审批与授权事实。</p>
        </div>
        <History size={19} aria-hidden="true" />
      </div>

      <p className="timeline-note case-source-note">
        审批通过不等于权限已开通。开通状态依据授权记录及有效期；选择一行的“查看详情”可查看完整过程。
      </p>

      {cases && cases.length > 0 ? (
        <table className="request-list" aria-label="我的申请列表">
          <thead><tr><th>申请权限</th><th>申请 / 审批状态</th><th>开通状态</th><th>申请时间</th><th>操作</th></tr></thead>
          <tbody>{cases.map(item => <tr key={item.request_id} className={item.request_id === selectedRequestId ? 'is-selected' : ''}>
            <td data-label="申请权限"><strong>{item.entitlement_name}</strong></td>
            <td data-label="申请 / 审批状态">{item.approval_status === 'approved' ? '审批已通过' : statusLabel(item.approval_status ?? item.request_status)}</td>
            <td data-label="开通状态">{grantStatus(item)}</td>
            <td data-label="申请时间"><time>{formatTime(item.created_at)}</time></td>
            <td><button type="button" aria-label={`查看${item.entitlement_name}详情`}
              aria-current={item.request_id === selectedRequestId ? 'true' : undefined}
              onClick={() => onSelectRequest?.(item.request_id)}>查看详情</button></td>
          </tr>)}</tbody>
        </table>
      ) : null}

      {detail ? (
        <div className="request-fact-summary">
          <div><span>申请权限</span><strong>{detail.entitlement.name}</strong><small>{detail.entitlement.code}</small></div>
          <div><span>申请理由</span><strong>{safeMessage(detail.request.justification)}</strong><small>{detail.request.duration_days} 天</small></div>
        </div>
      ) : null}

      {detail?.provisioning.access_granted && detail.provisioning.grant_id ? (
        <section className="grant-fact" aria-labelledby="grant-fact-title">
          <div>
            <p className="eyebrow">PERSISTED GRANT</p>
            <h3 id="grant-fact-title">{grantStatus(detail.provisioning) === '权限已开通' ? '权限已开通' : '授权记录'}</h3>
          </div>
          <p>{grantStatus(detail.provisioning)} · 本地演示环境</p>
          <details className="packet-technical"><summary>技术详情</summary><p>授权记录编号：{detail.provisioning.grant_id}</p></details>
          <p>
            生效 {formatTime(detail.provisioning.starts_at)}，到期 {formatTime(detail.provisioning.expires_at)}
          </p>
        </section>
      ) : null}

      {detail ? (
        <p className="timeline-note">
          本地演示使用模拟权限系统，未接入真实企业资源。尚未实现到期回收或撤销；此处展示已持久化的授权记录与有效期。
        </p>
      ) : null}

      {detail?.decision_packet ? (
        <DecisionPacketPanel packet={detail.decision_packet} />
      ) : null}

      {loading ? (
        <div className="business-state" role="status"><LoaderCircle className="spin" size={20} /><strong>正在加载申请事实</strong></div>
      ) : error ? (
        <div className="business-state is-error" role="alert">
          <AlertCircle size={20} />
          <p>{safeMessage(error)}</p>
          <button type="button" onClick={onRetry}><RefreshCw size={14} />重试</button>
        </div>
      ) : entries.length === 0 ? (
        <div className="business-empty"><FileCheck2 size={20} /><strong>暂无申请事实</strong><p>完成草稿后，申请状态会出现在这里。</p></div>
      ) : (
        <ol className="request-timeline" aria-label="申请生命周期">
          {entries.map((entry) => (
            <li key={entry.id} className={`request-timeline-item is-${entry.tone}`}>
              <span className="request-timeline-marker" aria-hidden="true">
                {entry.tone === 'done' ? <CheckCircle2 size={14} /> : entry.tone === 'unknown' ? <AlertCircle size={14} /> : <Clock3 size={14} />}
              </span>
              <div className="request-timeline-content">
                <div className="request-timeline-topline"><strong>{entry.label}</strong><time>{formatTime(entry.time)}</time></div>
                <p>{entry.owner ? `责任人：${entry.owner}` : '责任人：系统事实'}</p>
                {entry.status ? <small className="timeline-status">状态：{statusLabel(entry.status)}</small> : null}
                {entry.nextStep ? <small><span>下一步</span>{safeMessage(entry.nextStep)}</small> : null}
              </div>
            </li>
          ))}
        </ol>
      )}

      {detail && detail.approval === null ? (
        <p className="timeline-note"><strong>尚未创建审批路线</strong>，决策材料冻结后才能从固定目录路线创建审批步骤。</p>
      ) : null}
    </section>
  )
}

export function RequestTimeline() {
  const [cases, setCases] = useState<CaseSummary[]>([])
  const [selectedRequestId, setSelectedRequestId] = useState<string | null>(null)
  const [detail, setDetail] = useState<RequestDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true)
    setError(null)
    try {
      const nextCases = await readMyRequests(signal)
      const nextRequestId = nextCases.items[0]?.request_id ?? null
      const nextDetail = nextRequestId
        ? await readRequestDetail(nextRequestId, signal)
        : null
      setCases(nextCases.items)
      setSelectedRequestId(nextRequestId)
      setDetail(nextDetail)
    } catch (loadError) {
      if (loadError instanceof Error && loadError.name === 'AbortError') return
      setError(loadError instanceof Error ? loadError.message : '申请事实暂时不可用，请稍后重试。')
    } finally {
      if (!signal?.aborted) setLoading(false)
    }
  }, [])

  const selectRequest = useCallback(async (requestId: string) => {
    setSelectedRequestId(requestId)
    setLoading(true)
    setError(null)
    try {
      setDetail(await readRequestDetail(requestId))
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : '申请事实暂时不可用，请稍后重试。')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    void load(controller.signal)
    return () => controller.abort()
  }, [load])

  return (
    <RequestTimelineView
      detail={detail}
      draft={null}
      loading={loading}
      error={error}
      onRetry={() => void load()}
      cases={cases}
      selectedRequestId={selectedRequestId}
      onSelectRequest={(requestId) => void selectRequest(requestId)}
    />
  )
}
