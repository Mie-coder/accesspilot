import { AlertCircle, Check, Circle, FileCheck2, LoaderCircle, RefreshCw, ShieldCheck } from 'lucide-react'

import type { DecisionPacket, RequestDetail, RequestDraft, RequestResult } from './types'

interface DraftCardProps {
  draft: RequestDraft | null
  missingFields: string[]
  requestResult: RequestResult | null
  decisionPacket: DecisionPacket | null
  decisionPacketError: string | null
  isGeneratingDecisionPacket: boolean
  approvalCase: RequestDetail['approval']
  approvalError: string | null
  isStartingApproval: boolean
  isBusy: boolean
  onConfirm: () => void
  onSubmit: () => void
  onRetryDecisionPacket: () => void
  onStartApproval: () => void
}

const generationModeLabels: Record<DecisionPacket['generation_mode'], string> = {
  provider: 'DeepSeek AI 建议',
  deterministic: '规则建议',
  unavailable: '建议不可用',
}

const fields: Array<{ key: keyof RequestDraft; label: string }> = [
  { key: 'employee_id', label: '申请人' },
  { key: 'entitlement_id', label: '目标权限' },
  { key: 'duration_days', label: '有效期' },
  { key: 'justification', label: '业务理由' },
]

function displayValue(key: keyof RequestDraft, value: RequestDraft[keyof RequestDraft]): string {
  if (value === null || value === false) return '等待补充'
  if (key === 'duration_days') return `${String(value)} 天`
  return String(value)
}

export function DraftCard({
  draft,
  missingFields,
  requestResult,
  decisionPacket,
  decisionPacketError,
  isGeneratingDecisionPacket,
  approvalCase,
  approvalError,
  isStartingApproval,
  isBusy,
  onConfirm,
  onSubmit,
  onRetryDecisionPacket,
  onStartApproval,
}: DraftCardProps) {
  const confirmed = draft?.confirmed === true && missingFields.length === 0
  const complete = draft !== null && missingFields.length === 0
  const status = confirmed
    ? '已确认，可送审'
    : complete
      ? '完整，等待确认'
      : `还差 ${missingFields.length || 4} 项`

  return (
    <section className="draft-card" aria-labelledby="draft-title">
      <div className="section-heading">
        <div>
          <p className="eyebrow">LIVE DRAFT</p>
          <h2 id="draft-title">申请草稿</h2>
        </div>
        <span className={`draft-status ${confirmed ? 'is-ready' : ''}`}>{status}</span>
      </div>

      <div className="draft-fields">
        {fields.map(({ key, label }) => {
          const value = draft?.[key] ?? null
          const isPresent = value !== null
          return (
            <div className="draft-field" key={key}>
              <span className={`field-state ${isPresent ? 'is-present' : ''}`} aria-hidden="true">
                {isPresent ? <Check size={13} strokeWidth={3} /> : <Circle size={10} />}
              </span>
              <div>
                <span className="field-label">{label}</span>
                <strong className={isPresent ? '' : 'is-empty'}>{displayValue(key, value)}</strong>
              </div>
            </div>
          )
        })}
      </div>

      <div className="guard-note">
        <ShieldCheck size={16} />
        <p>确认只代表允许创建正式申请，不代表审批通过，也不会直接开通权限。</p>
      </div>

      {requestResult ? (
        <div className="submitted-result">
          <div className="request-created" role="status">
            <FileCheck2 size={18} />
            <div>
              <strong>正式申请已创建</strong>
              <span>{requestResult.request_id}</span>
            </div>
          </div>

          {isGeneratingDecisionPacket ? (
            <div className="packet-generation-state" role="status">
              <LoaderCircle className="spin" size={16} />
              <span>正在冻结决策材料…</span>
            </div>
          ) : decisionPacket ? (
            <>
              <div className="packet-generation-state is-ready" role="status">
                <ShieldCheck size={16} />
                <span>
                  <strong>决策材料已冻结</strong>
                  {generationModeLabels[decisionPacket.generation_mode]} · {decisionPacket.generation_mode}
                </span>
              </div>
              {approvalCase ? (
                <div className="approval-start-state is-ready" role="status">
                  <Check size={16} />
                  <span><strong>审批已启动</strong>{statusLabels[approvalCase.approval_status] ?? approvalCase.approval_status}</span>
                </div>
              ) : (
                <div className={`approval-start-state${approvalError ? ' is-error' : ''}`}>
                  {approvalError ? <p role="alert">{approvalError}</p> : <p>决策材料已就绪，由申请人明确启动审批路线。</p>}
                  <button
                    type="button"
                    disabled={isStartingApproval}
                    aria-label={isStartingApproval ? '正在启动审批' : approvalError ? '重试启动审批' : '启动审批'}
                    onClick={onStartApproval}
                  >
                    {isStartingApproval ? <><LoaderCircle className="spin" size={14} />正在启动…</> : approvalError ? <><RefreshCw size={14} />重试启动审批</> : '启动审批'}
                  </button>
                </div>
              )}
            </>
          ) : (
            <div className={`packet-generation-state is-error${decisionPacketError ? '' : ' is-pending'}`}>
              <div role={decisionPacketError ? 'alert' : 'status'}>
                <AlertCircle size={16} />
                <span>{decisionPacketError ?? '决策材料尚未生成，可在当前申请上安全重试。'}</span>
              </div>
              <button type="button" onClick={onRetryDecisionPacket}>
                <RefreshCw size={14} />重试生成决策材料
              </button>
            </div>
          )}
        </div>
      ) : (
        <button
          className="primary-action"
          type="button"
          disabled={isBusy || !complete}
          onClick={confirmed ? onSubmit : onConfirm}
        >
          {isBusy ? <LoaderCircle className="spin" size={17} /> : null}
          {!complete
            ? '补全后才能确认'
            : confirmed
              ? '提交正式申请'
              : '确认申请内容'}
        </button>
      )}
    </section>
  )
}

const statusLabels: Record<string, string> = {
  pending_manager: '待直属经理审批',
  pending_data_owner: '待数据负责人审批',
  approved: '审批通过',
  rejected: '已驳回',
}
