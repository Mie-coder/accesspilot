import { Check, ShieldCheck } from 'lucide-react'

import type { RequestDraft, WorkspaceIdentity } from './types'

interface ConfirmationSummaryProps {
  identity: WorkspaceIdentity
  draft: RequestDraft | null
  /** Name read from the server-owned entitlement facts; null is a safe fallback. */
  entitlementName: string | null
  isBusy: boolean
  onConfirm: () => void
}

function entitlementDisplay(name: string | null, code: string): string {
  return name ? `${name}（${code}）` : code
}

export function ConfirmationSummary({
  identity,
  draft,
  entitlementName,
  isBusy,
  onConfirm,
}: ConfirmationSummaryProps) {
  if (
    draft === null
    || draft.confirmed
    || draft.entitlement_id === null
    || draft.duration_days === null
    || draft.justification === null
  ) return null

  const fields = [
    { label: '申请人', value: `${identity.name} · ${identity.employee_id}` },
    { label: '目标权限', value: entitlementDisplay(entitlementName, draft.entitlement_id) },
    { label: '有效期', value: `${draft.duration_days} 天` },
    { label: '业务理由', value: draft.justification },
  ]

  return (
    <section className="confirmation-summary" aria-labelledby="confirmation-summary-title">
      <div className="confirmation-heading">
        <div>
          <p className="eyebrow">CONFIRMATION SHEET</p>
          <h2 id="confirmation-summary-title">请确认申请信息</h2>
        </div>
        <Check aria-hidden="true" size={18} />
      </div>
      <dl className="confirmation-fields">
        {fields.map(({ label, value }) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
      <p className="confirmation-boundary">
        <ShieldCheck aria-hidden="true" size={15} />
        确认仅记录你同意创建正式申请，不会提交、审批或直接开通权限。
      </p>
      <button className="confirmation-action" type="button" disabled={isBusy} onClick={onConfirm}>
        确认申请内容
      </button>
    </section>
  )
}
