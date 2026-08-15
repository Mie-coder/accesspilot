import {
  AlertCircle,
  CheckCircle2,
  Clock3,
  FileSearch,
  History,
  LoaderCircle,
  RefreshCw,
  ShieldAlert,
  ShieldCheck,
  UserCheck,
} from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'

import {
  decideApproval,
  provisionRequest,
  readAccessibleRequests,
  readApprovalInbox,
  readProvisioningTasks,
  readRequestDetail,
  recoverProvisioning,
} from './api'
import { DecisionPacketPanel } from './DecisionPacketPanel'
import type { ApprovalInbox, CaseList, ProvisioningTaskList, RequestDetail } from './types'

type ApprovalDecision = 'approve' | 'reject'

interface OperationsViewProps {
  roleLabel: string
  cases: CaseList | null
  detail: RequestDetail | null
  approvalInbox: ApprovalInbox | null
  provisioningTasks?: ProvisioningTaskList | null
  isLoading: boolean
  isDeciding: boolean
  isProvisioning?: boolean
  error: string | null
  decisionError: string | null
  provisioningError?: string | null
  onRefresh: () => void
  onSelectRequest: (requestId: string) => void
  onDecide: (
    caseId: string,
    approvalStepId: string,
    decision: ApprovalDecision,
    comment: string | null,
  ) => void
  onProvision?: (requestId: string) => void
  onRecover?: (requestId: string) => void
}

const statusLabels: Record<string, string> = {
  submitted: '已提交',
  pending_manager: '待经理审批',
  pending_data_owner: '待数据负责人审批',
  approved: '审批通过',
  rejected: '已驳回',
  pending: '待处理',
  waiting: '等待前序',
  succeeded: '已开通',
  failed: '开通失败',
  unknown: '结果未知',
  not_started: '尚未开通',
}

const eventLabels: Record<string, string> = {
  'request.submitted': '申请人提交正式申请',
  'risk_review.completed': '风险审查完成',
  'approval.started': '审批路线已创建',
  'approval.step.approved': '人工审批通过',
  'approval.step.rejected': '人工审批驳回',
  'provisioning.started': '开始调用 IAM',
  'provisioning.retry_started': '使用原幂等键重试 IAM',
  'provisioning.unknown': 'IAM 响应未知，等待查询',
  'provisioning.failed': 'IAM 明确返回失败',
  'provisioning.succeeded': 'IAM 开通成功',
  'provisioning.reconciled': '授权事实已对账恢复',
}

function statusLabel(status: string): string {
  return statusLabels[status] ?? status
}

function formatTime(value: string | null): string {
  if (!value) return '尚未发生'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date)
}

function safeErrorMessage(message: string): string {
  return message
    .replace(/system_prompt|system prompt|sk-[a-z0-9_-]+/gi, '受保护内容')
    .replace(/chain[-_ ]of[-_ ]thought|traceback/gi, '受保护内容')
}

function currentDecisionTarget(
  detail: RequestDetail | null,
  approvalInbox: ApprovalInbox | null,
) {
  const approval = detail?.approval
  if (!detail || !approval || !approvalInbox) return null
  if (!['pending_manager', 'pending_data_owner'].includes(approval.approval_status)) return null
  if (detail.request.requester_id === approvalInbox.actor.employee_id) return null

  const item = approvalInbox.items.find((candidate) =>
    candidate.request_id === detail.request.request_id
    && candidate.approval_case_id === approval.approval_case_id
    && candidate.step_status === 'pending'
    && candidate.approval_status === approval.approval_status
    && approvalInbox.actor.roles.includes(candidate.approver_role),
  )
  if (!item) return null
  const step = approval.steps.find((candidate) =>
    candidate.step_id === item.approval_step_id
    && candidate.approver_id === approvalInbox.actor.employee_id
    && candidate.approver_role === item.approver_role
    && candidate.step_status === 'pending',
  )
  return step ? { caseId: approval.approval_case_id, step } : null
}

function ApprovalDecisionCard({
  caseId,
  approvalStepId,
  isDeciding,
  onDecide,
}: {
  caseId: string
  approvalStepId: string
  isDeciding: boolean
  onDecide: OperationsViewProps['onDecide']
}) {
  const [comment, setComment] = useState('')
  const normalizedComment = comment.trim()

  return (
    <div className="decision-card" aria-label="当前审批操作">
      <label>
        审批评论
        <textarea
          aria-label="审批评论"
          rows={3}
          value={comment}
          disabled={isDeciding}
          placeholder="批准可选；驳回必须说明原因"
          onChange={(event) => setComment(event.target.value)}
        />
      </label>
      <p className="decision-guard-copy">
        只能处理当前轮到你且状态为 pending 的步骤；服务端会再次原子校验。
      </p>
      <div className="decision-actions">
        {isDeciding ? (
          <button type="button" disabled aria-label="正在提交审批决定">
            <LoaderCircle className="spin" size={15} />正在提交…
          </button>
        ) : (
          <>
            <button
              className="danger-action"
              type="button"
              disabled={normalizedComment.length === 0}
              onClick={() => onDecide(
                caseId,
                approvalStepId,
                'reject',
                normalizedComment,
              )}
            >
              驳回当前步骤
            </button>
            <button
              type="button"
              onClick={() => onDecide(
                caseId,
                approvalStepId,
                'approve',
                normalizedComment || null,
              )}
            >
              批准当前步骤
            </button>
          </>
        )}
      </div>
    </div>
  )
}

function LoadingState() {
  return (
    <section className="operations-state" role="status" aria-live="polite">
      <LoaderCircle className="spin" size={24} />
      <h2>正在读取可访问 Case</h2>
      <p>从 PostgreSQL 加载当前 Principal 有资源关系的正式事实…</p>
    </section>
  )
}

function ErrorState({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <section className="operations-state is-error" role="alert">
      <AlertCircle size={24} />
      <h2>审批工作台暂时不可用</h2>
      <p>{message}</p>
      <button type="button" onClick={onRetry}>
        <RefreshCw size={15} />重新读取
      </button>
    </section>
  )
}

export function OperationsView({
  roleLabel,
  cases,
  detail,
  approvalInbox,
  provisioningTasks = null,
  isLoading,
  isDeciding,
  isProvisioning = false,
  error,
  decisionError,
  provisioningError = null,
  onRefresh,
  onSelectRequest,
  onDecide,
  onProvision,
  onRecover,
}: OperationsViewProps) {
  const decisionTarget = currentDecisionTarget(detail, approvalInbox)
  const provisioningTask = roleLabel === '权限管理员'
    && provisioningTasks?.actor.employee_id === 'EMP-004'
    && provisioningTasks.actor.roles.includes('permissions_admin')
    ? provisioningTasks.items.find((item) => item.request_id === detail?.request.request_id) ?? null
    : null
  if (isLoading) return <LoadingState />
  if (error && detail === null) return <ErrorState message={error} onRetry={onRefresh} />

  return (
    <main className="operations-grid" id="main-workbench">
      <section className="inbox-panel" aria-labelledby="inbox-title">
        <div className="operations-heading">
          <div>
            <p className="eyebrow">ROLE-SCOPED INBOX</p>
            <h1 id="inbox-title">{roleLabel}的可访问 Case</h1>
            <p>后端在 SQL 中只返回当前或已决定的本人审批 Case，以及管理员可读的已批准 Case。</p>
          </div>
          <button className="icon-button" type="button" onClick={onRefresh} aria-label="刷新审批事实">
            <RefreshCw size={17} />
          </button>
        </div>

        {error ? (
          <div className="recoverable-banner" role="alert">
            <AlertCircle size={17} />
            <span>{error}</span>
          </div>
        ) : null}

        {!cases || cases.items.length === 0 ? (
          <div className="inbox-empty">
            <CheckCircle2 size={24} />
            <strong>当前没有可访问 Case</strong>
            <p>等待中的未轮到步骤与管理员不可读的未批准 Case 不会出现。</p>
          </div>
        ) : (
          <ul className="inbox-list">
            {cases.items.map((item) => (
              <li key={item.request_id}>
                <button
                  className={`inbox-item${
                    item.request_id === detail?.request.request_id ? ' is-selected' : ''
                  }`}
                  type="button"
                  aria-current={item.request_id === detail?.request.request_id ? 'true' : undefined}
                  onClick={() => onSelectRequest(item.request_id)}
                >
                  <span className="inbox-item-topline">
                    <span className="risk-pill">Case</span>
                    <span>{statusLabel(item.approval_status ?? item.request_status)}</span>
                  </span>
                  <strong>{item.entitlement_name}</strong>
                  <span>{item.requester_name} · {item.duration_days} 天</span>
                  <small>{item.justification}</small>
                </button>
              </li>
            ))}
          </ul>
        )}

        {detail === null ? (
          <div className="operations-state is-empty">
            <FileSearch size={24} />
            <h2>还没有可回放的正式 Case</h2>
            <p>当资源关系生效后，这里才会展示共享事实。</p>
          </div>
        ) : null}
      </section>

      {detail ? (
        <section className="detail-panel" aria-labelledby="detail-title">
          <div className="detail-heading">
            <div>
              <div className="detail-badges">
                <span className="replay-pill"><History size={13} />只读事实回放</span>
                <span className={`risk-pill is-${detail.entitlement.risk_level}`}>
                  {detail.entitlement.risk_level} risk
                </span>
              </div>
              <h2 id="detail-title">{detail.entitlement.name}</h2>
              <p>{detail.request.request_id}</p>
            </div>
            <span className="case-status">
              {statusLabel(detail.approval?.approval_status ?? detail.request.request_status)}
            </span>
          </div>

          <div className="fact-grid">
            <div><span>申请人</span><strong>{detail.request.requester_name}</strong></div>
            <div><span>权限代码</span><strong>{detail.entitlement.code}</strong></div>
            <div><span>有效期</span><strong>{detail.request.duration_days} 天</strong></div>
            <div><span>审批策略</span><strong>{detail.entitlement.approval_policy}</strong></div>
          </div>
          <p className="timeline-note case-source-note">
            共享 Case 来自 PostgreSQL，资源关系由后端 ACL 判定；当前页面只回放已持久化的业务事实，不在读取过程中执行审批或开通。
          </p>
          <div className="justification-block">
            <span>业务理由</span>
            <p>{detail.request.justification}</p>
          </div>

          {detail.decision_packet ? (
            <DecisionPacketPanel packet={detail.decision_packet} />
          ) : (
            <section className="detail-section" aria-labelledby="risk-title">
              <div className="section-heading compact">
                <div><p className="eyebrow">POLICY EVIDENCE</p><h3 id="risk-title">风险审查与政策引用</h3></div>
                <ShieldAlert size={18} />
              </div>
              {detail.risk_review ? (
                <>
                  <p className="risk-summary">{detail.risk_review.summary}</p>
                  <ul className="policy-list">
                    {detail.risk_review.citations.map((citation) => (
                      <li key={citation.policy_code}>
                        <span>{citation.policy_code}</span>
                        <div><strong>{citation.title ?? '已引用政策'}</strong><p>{citation.reason}</p></div>
                      </li>
                    ))}
                  </ul>
                </>
              ) : (
                <p className="detail-empty">决策材料尚未生成。</p>
              )}
            </section>
          )}

          <section className="detail-section" aria-labelledby="steps-title">
            <div className="section-heading compact">
              <div><p className="eyebrow">ORDERED GUARDS</p><h3 id="steps-title">审批步骤</h3></div>
              <UserCheck size={18} />
            </div>
            {detail.approval ? (
              <>
                <ol className="approval-steps">
                  {detail.approval.steps.map((step) => (
                    <li className={`is-${step.step_status}`} key={step.step_id}>
                      <span className="step-index">{step.step_order}</span>
                      <div>
                        <strong>{step.approver_role === 'manager' ? '直属经理' : '数据负责人'}</strong>
                        <p>{step.approver_id} · {statusLabel(step.step_status)}</p>
                        {step.comment ? <small>“{step.comment}”</small> : null}
                      </div>
                      <time>{formatTime(step.decided_at)}</time>
                    </li>
                  ))}
                </ol>
                {decisionError ? (
                  <div className="decision-error" role="alert">
                    <AlertCircle size={15} />
                    <span>{safeErrorMessage(decisionError)}</span>
                  </div>
                ) : null}
                {decisionTarget ? (
                  <ApprovalDecisionCard
                    key={decisionTarget.step.step_id}
                    caseId={decisionTarget.caseId}
                    approvalStepId={decisionTarget.step.step_id}
                    isDeciding={isDeciding}
                    onDecide={onDecide}
                  />
                ) : null}
              </>
            ) : (
              <p className="detail-empty">尚未创建审批步骤。</p>
            )}
          </section>

          <section className="detail-section provisioning-section" aria-labelledby="provision-title">
            <div className="section-heading compact">
              <div><p className="eyebrow">IAM FACT</p><h3 id="provision-title">权限开通</h3></div>
              <ShieldCheck size={18} />
            </div>
            <div className={`provision-status is-${detail.provisioning.provisioning_status}`}>
              <div>
                <strong>{statusLabel(detail.provisioning.provisioning_status)}</strong>
                <p>
                  {detail.provisioning.access_granted
                    ? `真实授权记录 ${detail.provisioning.grant_id}`
                    : detail.provisioning.last_error ?? '审批通过后才允许调用 IAM。'}
                </p>
              </div>
              <span>尝试 {detail.provisioning.attempt_count} 次</span>
            </div>

            <p className="timeline-note">
              边界：v1.2 不包含到期回收或撤销；页面只显示已持久化开通与 Grant 事实。
            </p>

            {provisioningError ? (
              <div className="decision-error" role="alert">
                <AlertCircle size={15} />
                <span>{safeErrorMessage(provisioningError)}</span>
              </div>
            ) : null}

            {provisioningTask?.can_provision && onProvision ? (
              <div className="action-card" aria-label="权限开通操作">
                <ShieldCheck size={17} />
                <div>
                  <strong>已通过两级审批</strong>
                  <p>服务端生成稳定幂等键，客户端不提供身份或幂等字段。</p>
                </div>
                <button
                  type="button"
                  disabled={isProvisioning}
                  onClick={() => onProvision(detail.request.request_id)}
                >
                  {isProvisioning ? <><LoaderCircle className="spin" size={15} />正在执行开通…</> : '执行权限开通'}
                </button>
              </div>
            ) : null}

            {provisioningTask?.can_recover && onRecover ? (
              <div className="action-card" aria-label="开通结果恢复操作">
                <RefreshCw size={17} />
                <div>
                  <strong>IAM 结果未知</strong>
                  <p>查询原操作，不创建新幂等键或重复 Grant。</p>
                </div>
                <button
                  type="button"
                  disabled={isProvisioning}
                  onClick={() => onRecover(detail.request.request_id)}
                >
                  {isProvisioning ? <><LoaderCircle className="spin" size={15} />正在查询原操作…</> : '查询原 IAM 操作'}
                </button>
              </div>
            ) : null}

          </section>

          <section className="detail-section" aria-labelledby="audit-title">
            <div className="section-heading compact">
              <div><p className="eyebrow">APPEND-ONLY</p><h3 id="audit-title">审计时间线</h3></div>
              <Clock3 size={18} />
            </div>
            <ol className="audit-timeline">
              {detail.audit_events.map((event) => (
                <li key={event.audit_event_id}>
                  <span className="audit-dot" aria-hidden="true" />
                  <div><strong>{eventLabels[event.event_type] ?? event.event_type}</strong><p>{event.actor_id ?? event.actor_type}</p></div>
                  <time>{formatTime(event.created_at)}</time>
                </li>
              ))}
            </ol>
          </section>
        </section>
      ) : null}
    </main>
  )
}

export function OperationsConsole({
  roleLabel,
  requestId,
}: {
  roleLabel: string
  requestId: string | null
}) {
  const [cases, setCases] = useState<CaseList | null>(null)
  const [detail, setDetail] = useState<RequestDetail | null>(null)
  const [approvalInbox, setApprovalInbox] = useState<ApprovalInbox | null>(null)
  const [provisioningTasks, setProvisioningTasks] = useState<ProvisioningTaskList | null>(null)
  const [selectedRequestId, setSelectedRequestId] = useState<string | null>(requestId)
  const [isLoading, setIsLoading] = useState(true)
  const [isDeciding, setIsDeciding] = useState(false)
  const [isProvisioning, setIsProvisioning] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [decisionError, setDecisionError] = useState<string | null>(null)
  const [provisioningError, setProvisioningError] = useState<string | null>(null)
  const canDecide = roleLabel === '直属经理' || roleLabel === '数据负责人'
  const canProvision = roleLabel === '权限管理员'

  const fetchFacts = useCallback(async () => {
    const [nextCases, nextApprovalInbox, nextProvisioningTasks] = await Promise.all([
      readAccessibleRequests(),
      canDecide ? readApprovalInbox() : Promise.resolve(null),
      canProvision ? readProvisioningTasks() : Promise.resolve(null),
    ])
    const preferredRequestId = selectedRequestId ?? requestId
    const nextRequestId = nextCases.items.some((item) => item.request_id === preferredRequestId)
      ? preferredRequestId
      : nextCases.items[0]?.request_id ?? null
    const nextDetail = nextRequestId ? await readRequestDetail(nextRequestId) : null
    return { nextCases, nextDetail, nextApprovalInbox, nextProvisioningTasks }
  }, [canDecide, canProvision, requestId, selectedRequestId])

  const applyFacts = useCallback((facts: Awaited<ReturnType<typeof fetchFacts>>) => {
    setCases(facts.nextCases)
    setDetail(facts.nextDetail)
    setApprovalInbox(facts.nextApprovalInbox)
    setProvisioningTasks(facts.nextProvisioningTasks)
  }, [])

  const refresh = useCallback(async () => {
    setIsLoading(true)
    setError(null)
    setDecisionError(null)
    setProvisioningError(null)
    try {
      const facts = await fetchFacts()
      applyFacts(facts)
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : '审批事实读取失败')
    } finally {
      setIsLoading(false)
    }
  }, [applyFacts, fetchFacts])

  const handleProvisioning = useCallback(async (
    requestIdToAct: string,
    action: 'provision' | 'recover',
  ) => {
    setIsProvisioning(true)
    setProvisioningError(null)
    try {
      if (action === 'provision') await provisionRequest(requestIdToAct)
      else await recoverProvisioning(requestIdToAct)
    } catch (provisionFailure) {
      setProvisioningError(
        provisionFailure instanceof Error ? provisionFailure.message : '开通操作失败，请重试。',
      )
      setIsProvisioning(false)
      return
    }
    try {
      applyFacts(await fetchFacts())
    } catch {
      setProvisioningError('开通操作已提交，但最新事实刷新失败；请手动刷新。')
    } finally {
      setIsProvisioning(false)
    }
  }, [applyFacts, fetchFacts])

  const handleDecision = useCallback(async (
    caseId: string,
    approvalStepId: string,
    decision: ApprovalDecision,
    comment: string | null,
  ) => {
    setIsDeciding(true)
    setDecisionError(null)
    try {
      await decideApproval(caseId, approvalStepId, decision, comment)
    } catch (decisionFailure) {
      setDecisionError(
        decisionFailure instanceof Error ? decisionFailure.message : '审批决定提交失败，请重试。',
      )
      setIsDeciding(false)
      return
    }
    try {
      applyFacts(await fetchFacts())
    } catch {
      setDecisionError('审批决定已提交，但最新事实刷新失败；请手动刷新。')
    } finally {
      setIsDeciding(false)
    }
  }, [applyFacts, fetchFacts])

  useEffect(() => {
    void refresh()
  }, [refresh])

  return (
    <OperationsView
      roleLabel={roleLabel}
      cases={cases}
      detail={detail}
      approvalInbox={approvalInbox}
      provisioningTasks={provisioningTasks}
      isLoading={isLoading}
      isDeciding={isDeciding}
      isProvisioning={isProvisioning}
      error={error}
      decisionError={decisionError}
      provisioningError={provisioningError}
      onRefresh={() => void refresh()}
      onSelectRequest={setSelectedRequestId}
      onDecide={(...args) => void handleDecision(...args)}
      onProvision={(nextRequestId) => void handleProvisioning(nextRequestId, 'provision')}
      onRecover={(nextRequestId) => void handleProvisioning(nextRequestId, 'recover')}
    />
  )
}
