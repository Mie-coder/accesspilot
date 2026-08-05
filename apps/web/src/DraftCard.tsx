import { Check, Circle, FileCheck2, LoaderCircle, ShieldCheck } from 'lucide-react'

import type { RequestDraft, RequestResult } from './types'

interface DraftCardProps {
  draft: RequestDraft | null
  missingFields: string[]
  requestResult: RequestResult | null
  isBusy: boolean
  onConfirm: () => void
  onSubmit: () => void
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
  isBusy,
  onConfirm,
  onSubmit,
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
        <div className="request-created" role="status">
          <FileCheck2 size={18} />
          <div>
            <strong>正式申请已创建</strong>
            <span>{requestResult.request_id}</span>
          </div>
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
