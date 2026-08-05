import {
  AlertCircle,
  Check,
  CheckCircle2,
  Clock3,
  FileSearch,
  History,
  LoaderCircle,
  RefreshCw,
  RotateCcw,
  ShieldAlert,
  ShieldCheck,
  UserCheck,
  X,
} from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'

import {
  decideApproval,
  provisionRequest,
  readApprovalInbox,
  readRequestDetail,
  recoverProvisioning,
  setFaultMode,
  startApproval,
} from './api'
import type { ApprovalInbox, RequestDetail } from './types'

interface OperationsViewProps {
  roleLabel: string
  quotaRemaining: number
  inbox: ApprovalInbox | null
  detail: RequestDetail | null
  isLoading: boolean
  isBusy: boolean
  error: string | null
  onRefresh: () => void
  onSelectRequest: (requestId: string) => void
  onStartApproval: () => void
  onDecision: (decision: 'approve' | 'reject', comment: string | null) => void
  onFaultMode: (mode: 'iam_failure' | 'iam_timeout' | null) => void
  onProvision: () => void
  onRecover: () => void
  onRetryProvision: () => void
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

function LoadingState() {
  return (
    <section className="operations-state" aria-live="polite">
      <LoaderCircle className="spin" size={24} />
      <h2>正在读取审批事实</h2>
      <p>从 PostgreSQL 加载待办、审批步骤和审计时间线…</p>
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
  quotaRemaining,
  inbox,
  detail,
  isLoading,
  isBusy,
  error,
  onRefresh,
  onSelectRequest,
  onStartApproval,
  onDecision,
  onFaultMode,
  onProvision,
  onRecover,
  onRetryProvision,
}: OperationsViewProps) {
  const [comment, setComment] = useState('')

  if (isLoading) return <LoadingState />
  if (error && detail === null) return <ErrorState message={error} onRetry={onRefresh} />

  const actionable =
    detail?.approval !== null &&
    inbox?.items.some(
      (item) =>
        item.request_id === detail?.request.request_id &&
        item.approval_case_id === detail.approval?.approval_case_id &&
        item.step_status === 'pending',
    )

  return (
    <main className="operations-grid" id="main-workbench">
      <section className="inbox-panel" aria-labelledby="inbox-title">
        <div className="operations-heading">
          <div>
            <p className="eyebrow">ROLE-SCOPED INBOX</p>
            <h1 id="inbox-title">{roleLabel}的审批收件箱</h1>
            <p>后端只返回真正轮到当前演示身份处理的步骤。</p>
          </div>
          <button className="icon-button" type="button" onClick={onRefresh} aria-label="刷新审批事实">
            <RefreshCw size={17} />
          </button>
        </div>

        {quotaRemaining === 0 ? (
          <div className="readonly-banner" role="status">
            <History size={17} />
            模型额度已用尽；历史消息和审计事实仍可只读回放。
          </div>
        ) : null}
        {error ? (
          <div className="recoverable-banner" role="alert">
            <AlertCircle size={17} />
            <span>{error}</span>
          </div>
        ) : null}

        {!inbox || inbox.items.length === 0 ? (
          <div className="inbox-empty">
            <CheckCircle2 size={24} />
            <strong>当前没有待处理步骤</strong>
            <p>等待前序步骤不会出现在这里，也无法从 UI 越序审批。</p>
          </div>
        ) : (
          <ul className="inbox-list">
            {inbox.items.map((item) => (
              <li key={item.approval_step_id}>
                <button
                  className={`inbox-item${
                    item.request_id === detail?.request.request_id ? ' is-selected' : ''
                  }`}
                  type="button"
                  aria-current={item.request_id === detail?.request.request_id ? 'true' : undefined}
                  onClick={() => onSelectRequest(item.request_id)}
                >
                  <span className="inbox-item-topline">
                    <span className={`risk-pill is-${item.risk_level}`}>{item.risk_level}</span>
                    <span>第 {item.step_order} 步 · {statusLabel(item.step_status)}</span>
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
            <h2>还没有正式申请</h2>
            <p>先切回申请人完成草稿确认和正式提交。</p>
          </div>
        ) : null}

        {detail?.approval === null ? (
          <div className="action-card">
            <ShieldCheck size={20} />
            <div>
              <strong>申请已冻结，尚未生成审批路线</strong>
              <p>启动后会先进行只读风险审查，再按目录建立人工审批步骤。</p>
            </div>
            <button type="button" disabled={isBusy} onClick={onStartApproval}>
              {isBusy ? <LoaderCircle className="spin" size={16} /> : <Check size={16} />}
              启动风险审查
            </button>
          </div>
        ) : null}

        {detail?.approval !== null && actionable ? (
          <div className="decision-card">
            <label>
              <span>审批意见（可选）</span>
              <textarea
                value={comment}
                onChange={(event) => setComment(event.target.value)}
                placeholder="写下判断依据，审计记录会保留这段意见"
                rows={3}
              />
            </label>
            <div className="decision-actions">
              <button
                className="danger-action"
                type="button"
                disabled={isBusy}
                onClick={() => onDecision('reject', comment.trim() || null)}
              >
                <X size={16} />驳回
              </button>
              <button
                className="primary-action"
                type="button"
                disabled={isBusy}
                onClick={() => onDecision('approve', comment.trim() || null)}
              >
                {isBusy ? <LoaderCircle className="spin" size={16} /> : <UserCheck size={16} />}
                批准当前步骤
              </button>
            </div>
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
          <div className="justification-block">
            <span>业务理由</span>
            <p>{detail.request.justification}</p>
          </div>

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
              <p className="detail-empty">审批路线启动后才会出现风险结论和真实政策引用。</p>
            )}
          </section>

          <section className="detail-section" aria-labelledby="steps-title">
            <div className="section-heading compact">
              <div><p className="eyebrow">ORDERED GUARDS</p><h3 id="steps-title">审批步骤</h3></div>
              <UserCheck size={18} />
            </div>
            {detail.approval ? (
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

            {detail.approval?.approval_status === 'approved' && !detail.provisioning.access_granted ? (
              <div className="fault-controls">
                <label>
                  <span>演示故障模式</span>
                  <select
                    aria-label="演示故障模式"
                    value={detail.fault_mode ?? ''}
                    disabled={isBusy || detail.provisioning.provisioning_status === 'unknown'}
                    onChange={(event) => {
                      const value = event.target.value
                      onFaultMode(
                        value === 'iam_timeout' || value === 'iam_failure' ? value : null,
                      )
                    }}
                  >
                    <option value="">正常开通</option>
                    <option value="iam_timeout">模拟响应超时</option>
                    <option value="iam_failure">模拟明确失败</option>
                  </select>
                </label>
                {detail.provisioning.provisioning_status === 'unknown' ? (
                  <button type="button" disabled={isBusy} onClick={onRecover}>
                    <RefreshCw size={16} />查询原 IAM 操作
                  </button>
                ) : detail.provisioning.provisioning_status === 'failed' ? (
                  <button type="button" disabled={isBusy} onClick={onRetryProvision}>
                    <RotateCcw size={16} />清除故障并幂等重试
                  </button>
                ) : (
                  <button type="button" disabled={isBusy} onClick={onProvision}>
                    {isBusy ? <LoaderCircle className="spin" size={16} /> : <ShieldCheck size={16} />}
                    开始幂等开通
                  </button>
                )}
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
  quotaRemaining,
}: {
  roleLabel: string
  requestId: string | null
  quotaRemaining: number
}) {
  const [inbox, setInbox] = useState<ApprovalInbox | null>(null)
  const [detail, setDetail] = useState<RequestDetail | null>(null)
  const [selectedRequestId, setSelectedRequestId] = useState<string | null>(requestId)
  const [isLoading, setIsLoading] = useState(true)
  const [isBusy, setIsBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const fetchFacts = useCallback(async () => {
    const nextInbox = await readApprovalInbox()
    const nextRequestId = selectedRequestId ?? requestId ?? nextInbox.items[0]?.request_id ?? null
    const nextDetail = nextRequestId ? await readRequestDetail(nextRequestId) : null
    return { nextInbox, nextDetail }
  }, [requestId, selectedRequestId])

  const refresh = useCallback(async () => {
    setIsLoading(true)
    setError(null)
    try {
      const facts = await fetchFacts()
      setInbox(facts.nextInbox)
      setDetail(facts.nextDetail)
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : '审批事实读取失败')
    } finally {
      setIsLoading(false)
    }
  }, [fetchFacts])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const runAction = useCallback(
    async (action: () => Promise<void>) => {
      setIsBusy(true)
      setError(null)
      let actionCompleted = false
      try {
        await action()
        actionCompleted = true
        const facts = await fetchFacts()
        setInbox(facts.nextInbox)
        setDetail(facts.nextDetail)
      } catch (actionError) {
        setError(
          actionCompleted
            ? '操作已提交，但事实同步失败；请刷新确认状态，勿重复操作。'
            : actionError instanceof Error
              ? actionError.message
              : '操作失败',
        )
      } finally {
        setIsBusy(false)
      }
    },
    [fetchFacts],
  )

  const currentRequestId = detail?.request.request_id ?? requestId
  const currentCaseId = detail?.approval?.approval_case_id ?? null

  return (
    <OperationsView
      roleLabel={roleLabel}
      quotaRemaining={quotaRemaining}
      inbox={inbox}
      detail={detail}
      isLoading={isLoading}
      isBusy={isBusy}
      error={error}
      onRefresh={() => void refresh()}
      onSelectRequest={setSelectedRequestId}
      onStartApproval={() => {
        if (currentRequestId) void runAction(() => startApproval(currentRequestId))
      }}
      onDecision={(decision, comment) => {
        if (currentCaseId) {
          void runAction(() => decideApproval(currentCaseId, decision, comment))
        }
      }}
      onFaultMode={(mode) => void runAction(() => setFaultMode(mode))}
      onProvision={() => {
        if (currentRequestId) void runAction(() => provisionRequest(currentRequestId))
      }}
      onRecover={() => {
        if (currentRequestId) void runAction(() => recoverProvisioning(currentRequestId))
      }}
      onRetryProvision={() => {
        if (currentRequestId) {
          void runAction(async () => {
            await setFaultMode(null)
            await provisionRequest(currentRequestId)
          })
        }
      }}
    />
  )
}
