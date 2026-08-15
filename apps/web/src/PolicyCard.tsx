import { AlertCircle, BookOpen, CheckCircle2, LoaderCircle, RefreshCw, Search, ShieldAlert } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'

import { queryPolicy, readPolicyCatalog } from './api'
import type { PolicyAnswer, PolicyCatalogItem, PolicyEvidence } from './types'

export interface PolicyCardViewProps {
  catalog: PolicyCatalogItem[]
  answer: PolicyAnswer | null
  loading: boolean
  error: string | null
  onQuery: (query: string) => void
  onRetry: () => void
}

function safeMessage(message: string): string {
  return message
    .replace(/system_prompt|system prompt|sk-[a-z0-9_-]+/gi, '受保护内容')
    .replace(/chain[-_ ]of[-_ ]thought|raw quota|traceback/gi, '受保护内容')
}

const statusCopy: Record<PolicyAnswer['status'], { label: string; className: string }> = {
  grounded: { label: '有可靠政策依据', className: 'is-grounded' },
  insufficient_evidence: { label: '证据不足', className: 'is-insufficient' },
  retrieval_unavailable: { label: '政策检索暂时不可用', className: 'is-unavailable' },
}

function EvidenceList({ evidence }: { evidence: PolicyEvidence[] }) {
  if (evidence.length === 0) return null
  return (
    <div className="policy-evidence-list">
      {evidence.map((item) => (
        <article className="policy-evidence" key={`${item.policy_code}-${item.version}`}>
          <div className="policy-evidence-topline">
            <span>{item.policy_code}</span>
            <span>{item.version}</span>
          </div>
          <h4>{item.title}</h4>
          <p>{item.content}</p>
          <small>来源：{item.source}</small>
        </article>
      ))}
    </div>
  )
}

export function PolicyCardView({
  catalog,
  answer,
  loading,
  error,
  onQuery,
  onRetry,
}: PolicyCardViewProps) {
  const [query, setQuery] = useState('')
  const answerCopy = answer ? statusCopy[answer.status] : null

  return (
    <section className="business-card-panel policy-card-panel" aria-labelledby="policy-card-title">
      <div className="business-card-heading">
        <div>
          <p className="eyebrow">POLICY CENTER</p>
          <h2 id="policy-card-title">政策中心</h2>
          <p>每个具体回答都标记事实状态和可追溯政策依据。</p>
        </div>
        <BookOpen size={19} aria-hidden="true" />
      </div>

      <form
        className="policy-search"
        onSubmit={(event) => {
          event.preventDefault()
          const value = query.trim()
          if (value) onQuery(value)
        }}
      >
        <label htmlFor="policy-query">政策问题</label>
        <div className="search-control">
          <Search size={16} aria-hidden="true" />
          <input
            id="policy-query"
            aria-label="政策问题"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="例如：高风险权限需要几级审批？"
          />
          <button type="submit" aria-label="查询政策"><Search size={15} /></button>
        </div>
      </form>

      {answer && answerCopy ? (
        <section className={`policy-answer ${answerCopy.className}`} aria-labelledby="policy-answer-title">
          <div className="policy-answer-heading">
            {answer.status === 'grounded' ? <CheckCircle2 size={17} /> : <ShieldAlert size={17} />}
            <h3 id="policy-answer-title">{answerCopy.label}</h3>
            <span className="policy-answer-status">{answer.status}</span>
          </div>
          <p className="policy-answer-body">{safeMessage(answer.answer)}</p>
          <EvidenceList evidence={answer.status === 'grounded' ? answer.evidence : []} />
          <p className="policy-next-step"><strong>下一步</strong>{safeMessage(answer.next_step)}</p>
        </section>
      ) : null}

      {loading ? (
        <div className="business-state" role="status"><LoaderCircle className="spin" size={20} /><strong>正在加载政策</strong></div>
      ) : error ? (
        <div className="business-state is-error" role="alert">
          <AlertCircle size={20} />
          <p>{safeMessage(error)}</p>
          <button type="button" onClick={onRetry}><RefreshCw size={14} />重试</button>
        </div>
      ) : answer ? null : catalog.length === 0 ? (
        <div className="business-empty"><BookOpen size={20} /><strong>暂无政策目录</strong><p>政策事实暂时没有可展示的主题。</p></div>
      ) : (
        <div className="policy-catalog" aria-label="政策主题目录">
          {catalog.map((item) => (
            <article className="policy-topic-card" key={item.policy_code}>
              <div className="policy-topic-code">{item.policy_code}</div>
              <h3>{item.title}</h3>
              <p>{item.content}</p>
              <small>{item.version} · {item.source}</small>
            </article>
          ))}
        </div>
      )}
    </section>
  )
}

export function PolicyCard() {
  const [catalog, setCatalog] = useState<PolicyCatalogItem[]>([])
  const [answer, setAnswer] = useState<PolicyAnswer | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const queryControllerRef = useRef<AbortController | null>(null)
  const querySequenceRef = useRef(0)

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true)
    setError(null)
    try {
      setCatalog(await readPolicyCatalog(signal))
    } catch (loadError) {
      if (loadError instanceof Error && loadError.name === 'AbortError') return
      setError(loadError instanceof Error ? loadError.message : '政策服务暂时不可用，请稍后重试。')
    } finally {
      if (!signal?.aborted) setLoading(false)
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    void load(controller.signal)
    return () => {
      controller.abort()
      queryControllerRef.current?.abort()
    }
  }, [load])

  const handleQuery = useCallback(async (query: string) => {
    const sequence = querySequenceRef.current + 1
    querySequenceRef.current = sequence
    queryControllerRef.current?.abort()
    const controller = new AbortController()
    queryControllerRef.current = controller
    // A new query owns the answer area. Never leave an older grounded answer
    // visible while the new evidence is loading or if it fails.
    setAnswer(null)
    setLoading(true)
    setError(null)
    try {
      const nextAnswer = await queryPolicy(query, controller.signal)
      if (controller.signal.aborted || querySequenceRef.current !== sequence) return
      setAnswer(nextAnswer)
    } catch (queryError) {
      if (controller.signal.aborted || querySequenceRef.current !== sequence) return
      setAnswer(null)
      setError(queryError instanceof Error ? queryError.message : '政策服务暂时不可用，请稍后重试。')
    } finally {
      if (!controller.signal.aborted && querySequenceRef.current === sequence) {
        setLoading(false)
      }
    }
  }, [])

  return (
    <PolicyCardView
      catalog={catalog}
      answer={answer}
      loading={loading}
      error={error}
      onQuery={(query) => void handleQuery(query)}
      onRetry={() => void load()}
    />
  )
}
