import { AlertCircle, Check, Clock3, LoaderCircle, RefreshCw, Search, ShieldAlert } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'

import { readAccessOverview, resolveEntitlement } from './api'
import type {
  AccessOverviewItem,
  EntitlementCandidate,
  EntitlementResolution,
  EntitlementSelectionResult,
} from './types'
import { useWorkbench } from './workbench-context'

export interface AccessCardsViewProps {
  overview: AccessOverviewItem[]
  resolution: EntitlementResolution | null
  loading: boolean
  error: string | null
  resolutionLoading?: boolean
  selectionLoading?: boolean
  selectionResult: EntitlementSelectionResult | null
  onRetry: () => void
  onResolve: (query: string) => void
  onSelect: (candidate: EntitlementCandidate) => void
}

const stateLabels: Record<AccessOverviewItem['state'], string> = {
  eligible: '可申请',
  owned: '已拥有',
  pending: '审批中',
  expiring_soon: '已拥有',
  expired: '已过期',
}

const riskLabels: Record<string, string> = {
  low: '低',
  medium: '中',
  high: '高',
  critical: '严重',
}

function safeMessage(message: string): string {
  return message
    .replace(/system_prompt|system prompt|sk-[a-z0-9_-]+/gi, '受保护内容')
    .replace(/chain[-_ ]of[-_ ]thought|raw quota|traceback/gi, '受保护内容')
}

function formatDate(value: string | null): string {
  if (!value) return '未设置'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date)
}

function AccessItemCard({ item, index }: { item: AccessOverviewItem; index: number }) {
  const risk = riskLabels[item.risk_level] ?? item.risk_level
  const headingId = `access-${index}-${item.code.replace(/[^a-zA-Z0-9_-]/g, '-')}-${item.state}`
  return (
    <article className={`access-fact-card is-${item.state}`} aria-labelledby={headingId}>
      <div className="access-fact-topline">
        <span className={`access-state-pill is-${item.state === 'expiring_soon' ? 'owned' : item.state}`}>{item.state === 'pending' && item.grant_id ? '待生效' : stateLabels[item.state]}</span>
        {item.state === 'expiring_soon' ? <span className="access-state-pill is-expiring_soon">即将过期</span> : null}
        <span className={`risk-pill is-${item.risk_level}`}><ShieldAlert size={12} />{risk}</span>
      </div>
      <h3 id={headingId}>{item.name}</h3>
      <p className="access-code">{item.code}</p>
      {['owned', 'expiring_soon'].includes(item.state) ? <p className="access-next-step">当前可使用（演示环境授权已生效）。</p> : null}
      {item.state === 'expiring_soon' ? <small>剩余有效期不超过 7 天，提醒不影响当前使用。</small> : null}
      <dl className="access-fact-meta">
        <div><dt>系统</dt><dd>{item.system_name} · {item.system_code}</dd></div>
        {item.starts_at ? <div><dt>生效时间</dt><dd>{formatDate(item.starts_at)}</dd></div> : null}
        {item.expires_at ? <div><dt>到期时间</dt><dd>{formatDate(item.expires_at)}</dd></div> : null}
        {item.max_duration_days !== null ? <div><dt>最长申请</dt><dd>{item.max_duration_days} 天</dd></div> : null}
      </dl>
      <p className="access-next-step"><strong>下一步</strong>{item.next_step}</p>
    </article>
  )
}

function CandidateButton({
  candidate,
  onSelect,
  actionLabel = '选择',
  disabled = false,
}: {
  candidate: EntitlementCandidate
  onSelect: (candidate: EntitlementCandidate) => void
  actionLabel?: string
  disabled?: boolean
}) {
  const select = () => onSelect(candidate)
  return (
    <button
      className="entitlement-candidate"
      type="button"
      disabled={disabled}
      aria-disabled={disabled}
      aria-label={`${candidate.name}（${candidate.code}，${candidate.system_name}）`}
      onClick={select}
    >
      <span>
        <strong>{candidate.name}</strong>
        <small>{candidate.code} · {candidate.system_name}</small>
      </span>
      <span className="candidate-arrow" aria-hidden="true">{actionLabel}</span>
    </button>
  )
}

export function AccessCardsView({
  overview,
  resolution,
  loading,
  error,
  resolutionLoading = false,
  selectionLoading = false,
  selectionResult,
  onRetry,
  onResolve,
  onSelect,
}: AccessCardsViewProps) {
  const [query, setQuery] = useState('')

  return (
    <section className="business-card-panel access-cards-panel" aria-labelledby="access-cards-title">
      <div className="business-card-heading">
        <div>
          <p className="eyebrow">ACCESS FACTS</p>
          <h2 id="access-cards-title">我的权限</h2>
          <p>只展示当前身份从目录、申请和授权事实源读取的状态。</p>
        </div>
        <Clock3 size={19} aria-hidden="true" />
      </div>

      <form
        className="entitlement-search"
        onSubmit={(event) => {
          event.preventDefault()
          const value = query.trim()
          if (value) onResolve(value)
        }}
      >
        <label htmlFor="entitlement-query">查找可申请权限</label>
        <div className="search-control">
          <Search size={16} aria-hidden="true" />
          <input
            id="entitlement-query"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="例如：仪表盘查看"
            autoComplete="off"
          />
          <button type="submit" aria-label="解析权限名称"><Search size={15} /></button>
        </div>
      </form>

      {selectionResult ? (
        <div className={`selection-result is-${selectionResult.status}`} role="status">
          {selectionResult.status === 'revalidated' ? <Check size={16} /> : <AlertCircle size={16} />}
          <div>
            <strong>{selectionResult.status === 'revalidated' ? '重新校验通过' : '重新校验未通过'}</strong>
            <p>{safeMessage(selectionResult.message)}</p>
            {selectionResult.previous_confirmation_invalidated ? <small>旧确认已失效，请重新确认</small> : null}
          </div>
        </div>
      ) : null}

      {resolution?.status === 'ambiguous' || resolution?.status === 'matched' ? (
        <section className="resolution-panel" aria-labelledby="resolution-title">
          <div className="resolution-heading">
            <div>
              <p className="eyebrow">MATCH REVIEW</p>
              <h3 id="resolution-title">
                {resolution.status === 'ambiguous' ? '需要选择一个匹配的权限' : '已匹配一个权限'}
              </h3>
            </div>
            <span>{resolution.candidates.length} 个候选</span>
          </div>
          <p className="resolution-note">
            {resolution.status === 'ambiguous'
              ? '候选来自当前身份可申请目录。选择后会重新校验资格并使旧确认失效。'
              : '这是当前身份目录中的唯一匹配。继续申请前仍会重新校验资格。'}
          </p>
          <div className="candidate-list">
            {resolution.candidates.map((candidate) => (
              <CandidateButton
                key={candidate.code}
                candidate={candidate}
                onSelect={onSelect}
                actionLabel={resolution.status === 'matched' ? '选择并重新校验' : '选择'}
                disabled={selectionLoading}
              />
            ))}
          </div>
        </section>
      ) : null}

      {resolution?.status === 'no_match' ? (
        <section className="resolution-panel is-no-match" aria-labelledby="resolution-title">
          <div className="resolution-heading">
            <div>
              <p className="eyebrow">MATCH REVIEW</p>
              <h3 id="resolution-title">没有匹配的权限</h3>
            </div>
            <span>未修改草稿</span>
          </div>
          <p className="resolution-note">未在当前身份可申请目录中找到这个名称。下面仅列出可申请权限事实，请选择一个明确名称后重试。</p>
          {resolution.eligible_access.length > 0 ? (
            <ul className="eligible-reference-list">
              {resolution.eligible_access.map((candidate) => (
                <li key={candidate.code}>
                  <strong>{candidate.name}</strong>
                  <small>{candidate.code} · {candidate.system_name}</small>
                </li>
              ))}
            </ul>
          ) : <p className="resolution-note">当前身份没有可申请的权限。</p>}
        </section>
      ) : null}

      {loading || resolutionLoading ? (
        <div className="business-state" role="status"><LoaderCircle className="spin" size={20} /><strong>{loading ? '正在加载权限事实' : '正在解析权限名称'}</strong></div>
      ) : error ? (
        <div className="business-state is-error" role="alert">
          <AlertCircle size={20} />
          <p>{safeMessage(error)}</p>
          <button type="button" onClick={onRetry}><RefreshCw size={14} />重试</button>
        </div>
      ) : overview.length === 0 ? (
        <div className="business-empty"><Clock3 size={20} /><strong>暂无权限事实</strong><p>当前身份还没有可展示的权限目录或申请记录。</p></div>
      ) : (
        <div className="access-fact-grid">
          {overview.map((item, index) => <AccessItemCard key={`${item.code}-${item.state}-${item.grant_id ?? item.request_id ?? 'eligible'}-${index}`} item={item} index={index} />)}
        </div>
      )}
      {selectionLoading ? <p className="selection-loading" role="status">正在重新校验所选权限，其它候选已暂时锁定。</p> : null}
    </section>
  )
}

export function AccessCards() {
  const workbench = useWorkbench()
  const [overview, setOverview] = useState<AccessOverviewItem[]>([])
  const [resolution, setResolution] = useState<EntitlementResolution | null>(null)
  const [selectionResult, setSelectionResult] = useState<EntitlementSelectionResult | null>(null)
  const [loading, setLoading] = useState(true)
  const [resolutionLoading, setResolutionLoading] = useState(false)
  const [selectionLoading, setSelectionLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const resolveControllerRef = useRef<AbortController | null>(null)
  const resolveSequenceRef = useRef(0)
  const selectionInFlightRef = useRef(false)

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true)
    setError(null)
    try {
      const result = await readAccessOverview(signal)
      setOverview(result.items)
    } catch (loadError) {
      if (loadError instanceof Error && loadError.name === 'AbortError') return
      setError(loadError instanceof Error ? loadError.message : '权限事实暂时不可用，请稍后重试。')
    } finally {
      if (!signal?.aborted) setLoading(false)
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    void load(controller.signal)
    return () => {
      controller.abort()
      resolveControllerRef.current?.abort()
    }
  }, [load])

  const handleResolve = useCallback(async (query: string) => {
    const sequence = resolveSequenceRef.current + 1
    resolveSequenceRef.current = sequence
    resolveControllerRef.current?.abort()
    const controller = new AbortController()
    resolveControllerRef.current = controller
    setResolution(null)
    setSelectionResult(null)
    setError(null)
    setResolutionLoading(true)
    try {
      const result = await resolveEntitlement(query, controller.signal)
      if (controller.signal.aborted || resolveSequenceRef.current !== sequence) return
      setResolution(result)
    } catch (resolveError) {
      if (controller.signal.aborted || resolveSequenceRef.current !== sequence) return
      setError(resolveError instanceof Error ? resolveError.message : '权限目录暂时不可用，请稍后重试。')
    } finally {
      if (!controller.signal.aborted && resolveSequenceRef.current === sequence) {
        setResolutionLoading(false)
      }
    }
  }, [])

  const handleSelect = useCallback(async (candidate: EntitlementCandidate) => {
    if (selectionInFlightRef.current) return
    selectionInFlightRef.current = true
    setSelectionLoading(true)
    setError(null)
    try {
      const result = await workbench.selectEntitlement(candidate.code)
      setSelectionResult(result)
      setResolution(null)
      if (result.status === 'revalidated') {
        await load()
      }
    } catch (selectError) {
      setSelectionResult({
        status: 'rejected',
        code: candidate.code,
        message: selectError instanceof Error ? selectError.message : '当前身份已不再具备申请资格。',
        previous_confirmation_invalidated: workbench.draft?.confirmed === true,
      })
    } finally {
      selectionInFlightRef.current = false
      setSelectionLoading(false)
    }
  }, [load, workbench])

  return (
    <AccessCardsView
      overview={overview}
      resolution={resolution}
      loading={loading}
      resolutionLoading={resolutionLoading}
      selectionLoading={selectionLoading}
      error={error}
      selectionResult={selectionResult}
      onRetry={() => void load()}
      onResolve={(query) => void handleResolve(query)}
      onSelect={(candidate) => void handleSelect(candidate)}
    />
  )
}
