import { useAui, useAuiState } from '@assistant-ui/react'
import {
  Activity,
  AlertCircle,
  Bot,
  Database,
  LoaderCircle,
  LogOut,
  RotateCcw,
  ShieldCheck,
  Sparkles,
} from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import type { KeyboardEvent as ReactKeyboardEvent } from 'react'

import { approvalRoleFor } from './approval'
import {
  ApiError,
  bootstrapWorkspace,
  login,
  logout,
  readAccessOverview,
  resetAuthClientState,
} from './api'
import { AccessCards } from './AccessCards'
import { AgentTrajectory } from './AgentTrajectory'
import { ChatThread } from './ChatThread'
import { ConfirmationSummary } from './ConfirmationSummary'
import { DraftCard } from './DraftCard'
import { entitlementNameForCode, type EntitlementNameFact } from './entitlement-name'
import { OperationsConsole } from './OperationsConsole'
import { PolicyCard } from './PolicyCard'
import { RequestTimeline } from './RequestTimeline'
import type { WorkspaceEvent, WorkspaceSnapshot } from './types'
import { WorkbenchRuntime } from './WorkbenchRuntime'
import { useWorkbench } from './workbench-context'

function eventCopy(event: WorkspaceEvent): { title: string; detail: string } | null {
  if (event.type === 'tool.summary') {
    return {
      title: '目录校验已运行',
      detail: typeof event.payload.summary === 'string' ? event.payload.summary : '工具已完成',
    }
  }
  if (event.type === 'business.status') {
    const labels: Record<string, string> = {
      collecting: '继续收集申请字段',
      awaiting_confirmation: '申请完整，等待明确确认',
      ready_to_submit: '确认完成，可以创建正式申请',
      submitted: '正式申请已创建，等待进入审批',
      validation_failed: '目录校验未通过',
      recoverable_error: '本轮可安全重试',
      answered: '只读咨询已回答',
    }
    const status = typeof event.payload.status === 'string' ? event.payload.status : ''
    return { title: '业务状态更新', detail: labels[status] ?? status }
  }
  if (event.type === 'error.recoverable') {
    return {
      title: '发生可恢复错误',
      detail: typeof event.payload.message === 'string' ? event.payload.message : '可稍后重试',
    }
  }
  if (event.type === 'security.notice') {
    return {
      title: '安全边界已生效',
      detail: typeof event.payload.message === 'string' ? event.payload.message : '已拒绝敏感内部信息请求',
    }
  }
  return null
}

function ActivityFeed({ events }: { events: WorkspaceEvent[] }) {
  const items = events
    .map((event) => ({ event, copy: eventCopy(event) }))
    .filter((item): item is { event: WorkspaceEvent; copy: { title: string; detail: string } } =>
      item.copy !== null,
    )
    .slice(-4)
    .reverse()

  return (
    <section className="activity-card" aria-labelledby="activity-title">
      <div className="section-heading compact">
        <div>
          <p className="eyebrow">SAFE EVENT STREAM</p>
          <h2 id="activity-title">最近活动</h2>
        </div>
        <Activity size={18} />
      </div>
      {items.length === 0 ? (
        <p className="activity-empty">发出第一条消息后，这里会显示后端 SSE 回放的业务摘要。</p>
      ) : (
        <ol className="activity-list">
          {items.map(({ event, copy }) => (
            <li key={event.id}>
              <span className="activity-dot" aria-hidden="true" />
              <div>
                <strong>{copy.title}</strong>
                <p>{copy.detail}</p>
                <span>事件 #{event.id}</span>
              </div>
            </li>
          ))}
        </ol>
      )}
    </section>
  )
}

function useEntitlementName(entitlementCode: string | null): string | null {
  const [fact, setFact] = useState<EntitlementNameFact | null>(null)

  useEffect(() => {
    let active = true
    if (entitlementCode === null) return () => { active = false }

    void readAccessOverview()
      .then(({ items }) => {
        if (!active) return
        setFact({
          code: entitlementCode,
          name: items.find((item) => item.code === entitlementCode)?.name ?? null,
        })
      })
      .catch(() => {
        // The code remains a valid server-derived draft fact, so a transient
        // name lookup failure must not hide the confirmation action.
        if (active) setFact({ code: entitlementCode, name: null })
      })

    return () => { active = false }
  }, [entitlementCode])

  return entitlementNameForCode(fact, entitlementCode)
}

const assistantViews = ['conversation', 'trajectory'] as const
type AssistantView = typeof assistantViews[number]

function WorkbenchPage({ onLogout }: { onLogout: () => Promise<void> }) {
  const workbench = useWorkbench()
  const approvalRole = approvalRoleFor(workbench.identity)
  const [showOperations, setShowOperations] = useState(false)
  const [activeView, setActiveView] = useState<'assistant' | 'access' | 'policy' | 'request'>('assistant')
  const [assistantView, setAssistantView] = useState<AssistantView>('conversation')
  const [logoutBusy, setLogoutBusy] = useState(false)
  const [logoutError, setLogoutError] = useState<string | null>(null)
  const [isConfirming, setIsConfirming] = useState(false)
  const confirmationInFlightRef = useRef(false)
  const assistantTabRefs = useRef<Record<AssistantView, HTMLButtonElement | null>>({
    conversation: null,
    trajectory: null,
  })
  const aui = useAui()
  const isRunning = useAuiState((state) => state.thread.isRunning)
  const entitlementName = useEntitlementName(workbench.draft?.entitlement_id ?? null)
  const confirm = useCallback(() => {
    if (confirmationInFlightRef.current || isRunning) return
    confirmationInFlightRef.current = true
    setIsConfirming(true)
    void Promise.resolve(aui.thread.append({
      role: 'user',
      content: [{ type: 'text', text: '确认提交' }],
    })).finally(() => {
      confirmationInFlightRef.current = false
      setIsConfirming(false)
    })
  }, [aui, isRunning])
  const handleLogout = async () => {
    setLogoutBusy(true)
    setLogoutError(null)
    try {
      await onLogout()
    } catch (logoutFailure: unknown) {
      setLogoutError(
        logoutFailure instanceof ApiError || logoutFailure instanceof Error
          ? logoutFailure.message
          : '退出登录失败，请稍后重试',
      )
    } finally {
      setLogoutBusy(false)
    }
  }
  const handleAssistantTabKeyDown = (
    event: ReactKeyboardEvent<HTMLButtonElement>,
    currentView: AssistantView,
  ) => {
    const currentIndex = assistantViews.indexOf(currentView)
    let nextView: AssistantView | null = null
    if (event.key === 'ArrowRight') {
      nextView = assistantViews[(currentIndex + 1) % assistantViews.length] ?? null
    } else if (event.key === 'ArrowLeft') {
      nextView = assistantViews[(currentIndex - 1 + assistantViews.length) % assistantViews.length] ?? null
    } else if (event.key === 'Home') {
      nextView = assistantViews[0]
    } else if (event.key === 'End') {
      nextView = assistantViews[assistantViews.length - 1]
    }
    if (nextView === null) return
    event.preventDefault()
    setAssistantView(nextView)
    assistantTabRefs.current[nextView]?.focus()
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <a className="brand" href="#main-workbench" aria-label="AccessPilot 首页">
          <span className="brand-mark" aria-hidden="true">
            <ShieldCheck size={20} />
          </span>
          <span>
            <strong>AccessPilot</strong>
            <small>最小权限申请工作台</small>
          </span>
        </a>
        <div className="topbar-actions">
          <div className="identity-summary" aria-label="当前产品身份">
            <strong>{workbench.identity.name}</strong>
            <span>{workbench.identity.department} · {workbench.identity.employee_id}</span>
          </div>
          {approvalRole ? (
            <button className="operations-toggle" type="button" onClick={() => setShowOperations((value) => !value)}>
              {showOperations ? '返回产品工作台' : '打开审批工作台'}
            </button>
          ) : null}
          <div
            className={`api-health is-${workbench.connectionState}`}
            role={workbench.connectionState === 'reconnecting' ? 'status' : undefined}
            aria-live="polite"
          >
            <span aria-hidden="true" />
            {workbench.connectionState === 'reconnecting' ? '活动流重连中' : 'API 已连接'}
          </div>
          <button
            className="logout-button"
            type="button"
            disabled={logoutBusy}
            onClick={() => void handleLogout()}
          >
            <LogOut size={16} />
            {logoutBusy ? '退出中…' : '退出登录'}
          </button>
        </div>
      </header>

      {logoutError ? (
        <div className="recoverable-banner product-global-error" role="alert">
          <AlertCircle size={17} />
          <span>{logoutError}</span>
        </div>
      ) : null}

      <nav className="product-nav" aria-label="产品导航">
        {([
          ['assistant', '权限助手'],
          ['access', '我的权限'],
          ['policy', '政策中心'],
          ['request', '我的申请'],
        ] as const).map(([view, label]) => (
          <button
            key={view}
            className={activeView === view && !showOperations ? 'is-active' : ''}
            type="button"
            aria-current={activeView === view && !showOperations ? 'page' : undefined}
            onClick={() => {
              setShowOperations(false)
              setActiveView(view)
            }}
          >
            {label}
          </button>
        ))}
      </nav>

      {(activeView !== 'assistant' || showOperations) && workbench.error ? (
        <div className="recoverable-banner product-global-error" role="alert">
          <AlertCircle size={17} />
          <span>{workbench.error}</span>
        </div>
      ) : null}

      {showOperations && approvalRole ? (
        <OperationsConsole roleLabel={approvalRole} requestId={workbench.requestResult?.request_id ?? null} />
      ) : activeView === 'access' ? (
        <main className="business-view-shell" id="main-workbench"><AccessCards /></main>
      ) : activeView === 'policy' ? (
        <main className="business-view-shell" id="main-workbench"><PolicyCard /></main>
      ) : activeView === 'request' ? (
        <main className="business-view-shell" id="main-workbench"><RequestTimeline /></main>
      ) : (
        <main className="workspace-grid" id="main-workbench">
        <section className="chat-panel" aria-labelledby="chat-title">
          <div className="chat-header">
            <div className="chat-title">
              <span className="assistant-avatar" aria-hidden="true"><Bot size={19} /></span>
              <div>
                <h1 id="chat-title">权限申请助手</h1>
                <p>由 FastAPI、PostgreSQL 与安全事件回放驱动</p>
              </div>
            </div>
            <div className="assistant-view-tabs" role="tablist" aria-label="助手视图切换">
              <button
                id="conversation-tab"
                ref={(node) => { assistantTabRefs.current.conversation = node }}
                type="button"
                role="tab"
                aria-selected={assistantView === 'conversation'}
                aria-controls="conversation-panel"
                tabIndex={assistantView === 'conversation' ? 0 : -1}
                className={assistantView === 'conversation' ? 'is-active' : ''}
                onClick={() => setAssistantView('conversation')}
                onKeyDown={(event) => handleAssistantTabKeyDown(event, 'conversation')}
              >
                对话
              </button>
              <button
                id="trajectory-tab"
                ref={(node) => { assistantTabRefs.current.trajectory = node }}
                type="button"
                role="tab"
                aria-selected={assistantView === 'trajectory'}
                aria-controls="trajectory-panel"
                tabIndex={assistantView === 'trajectory' ? 0 : -1}
                className={assistantView === 'trajectory' ? 'is-active' : ''}
                onClick={() => setAssistantView('trajectory')}
                onKeyDown={(event) => handleAssistantTabKeyDown(event, 'trajectory')}
              >
                轨迹
              </button>
            </div>
          </div>

          {workbench.error ? (
            <div className="recoverable-banner" role="alert">
              <AlertCircle size={17} />
              <span>{workbench.error}</span>
            </div>
          ) : null}
          <div
            className="assistant-view-panel conversation-view-panel"
            id="conversation-panel"
            role="tabpanel"
            aria-labelledby="conversation-tab"
            hidden={assistantView !== 'conversation'}
          >
            <ChatThread
              confirmation={(
                <ConfirmationSummary
                  identity={workbench.identity}
                  draft={workbench.draft}
                  entitlementName={entitlementName}
                  isBusy={isRunning || isConfirming}
                  onConfirm={confirm}
                />
              )}
            />
          </div>
          <div
            className="assistant-view-panel trajectory-view-panel"
            id="trajectory-panel"
            role="tabpanel"
            aria-labelledby="trajectory-tab"
            hidden={assistantView !== 'trajectory'}
          >
            <AgentTrajectory
              events={workbench.events}
              connectionState={workbench.connectionState}
              isRunning={isRunning}
            />
          </div>
        </section>

        <aside className="right-rail" aria-label="申请状态">
          <DraftCard
            draft={workbench.draft}
            missingFields={workbench.missingFields}
            requestResult={workbench.requestResult}
            decisionPacket={workbench.decisionPacket}
            decisionPacketError={workbench.decisionPacketError}
            isGeneratingDecisionPacket={workbench.isGeneratingDecisionPacket}
            approvalCase={workbench.approvalCase}
            approvalError={workbench.approvalError}
            isStartingApproval={workbench.isStartingApproval}
            isBusy={isRunning || isConfirming || workbench.isSubmitting}
            entitlementName={entitlementName}
            onConfirm={confirm}
            onSubmit={() => void workbench.submit()}
            onRetryDecisionPacket={() => void workbench.retryDecisionPacket()}
            onStartApproval={() => void workbench.startApproval()}
          />
          <ActivityFeed events={workbench.events} />
          <div className="source-note">
            <Database size={16} />
            <span>Workspace / PostgreSQL 是业务事实源，assistant-ui 只管理对话交互。</span>
          </div>
        </aside>
      </main>
      )}
    </div>
  )
}

function LoadingScreen() {
  return (
    <main className="boot-screen" aria-live="polite">
      <div className="boot-mark"><Sparkles size={24} /></div>
      <LoaderCircle className="spin" size={20} />
      <h1>正在检查登录状态</h1>
      <p>恢复当前 Session 与安全事件回放…</p>
    </main>
  )
}

const MOCK_ACCOUNTS = [
  { id: 'EMP-001', role: '申请人', detail: '发起权限申请并查看自己的 Case' },
  { id: 'EMP-002', role: '直属经理', detail: '查看当前轮到或已参与的 Case' },
  { id: 'EMP-003', role: '数据负责人', detail: '查看当前轮到或已参与的 Case' },
  { id: 'EMP-004', role: '权限管理员', detail: '查看已全部批准的 Case' },
] as const

function LoginScreen({ onAuthenticated }: { onAuthenticated: () => Promise<void> }) {
  const [busyAccount, setBusyAccount] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const chooseAccount = async (accountId: string) => {
    setBusyAccount(accountId)
    setError(null)
    try {
      await login(accountId)
      await onAuthenticated()
    } catch (loginError: unknown) {
      if (loginError instanceof ApiError || loginError instanceof Error) {
        setError(loginError.message)
      } else {
        setError('登录暂时失败，请稍后重试')
      }
    } finally {
      setBusyAccount(null)
    }
  }

  return (
    <main className="login-screen">
      <section className="login-card" aria-labelledby="login-title">
        <p className="eyebrow">ACCESSPILOT · MOCK LOGIN</p>
        <h1 id="login-title">选择一个作品集账号</h1>
        <p className="login-disclaimer">
          作品集 Mock 登录，非真实身份认证。账号只用于演示 Session 隔离与角色边界，不包含密码或注册。
        </p>
        <div className="login-account-grid">
          {MOCK_ACCOUNTS.map((account) => (
            <button
              key={account.id}
              className="login-account-card"
              type="button"
              disabled={busyAccount !== null}
              onClick={() => void chooseAccount(account.id)}
            >
              <span className="login-account-id">{account.id}</span>
              <strong>{account.role}</strong>
              <small>{account.detail}</small>
              {busyAccount === account.id ? <LoaderCircle className="spin" size={16} /> : null}
            </button>
          ))}
        </div>
        {error ? <p className="login-error" role="alert">{error}</p> : null}
      </section>
    </main>
  )
}

function BootError({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <main className="boot-screen is-error">
      <div className="boot-mark"><AlertCircle size={24} /></div>
      <h1>暂时无法连接 AccessPilot API</h1>
      <p>{message}</p>
      <button type="button" onClick={onRetry}><RotateCcw size={16} />重新连接</button>
    </main>
  )
}

export function App() {
  const [snapshot, setSnapshot] = useState<WorkspaceSnapshot | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [attempt, setAttempt] = useState(0)
  const [authState, setAuthState] = useState<'checking' | 'anonymous' | 'authenticated'>('checking')

  useEffect(() => {
    let active = true
    setError(null)
    void bootstrapWorkspace()
      .then((value) => {
        if (active) {
          setSnapshot(value)
          setAuthState('authenticated')
        }
      })
      .catch((bootError: unknown) => {
        if (!active) return
        if (bootError instanceof ApiError && bootError.status === 401) {
          resetAuthClientState()
          setSnapshot(null)
          setAuthState('anonymous')
          return
        }
        setError(
          bootError instanceof ApiError || bootError instanceof Error
            ? bootError.message
            : '未知连接错误',
        )
      })
    return () => {
      active = false
    }
  }, [attempt])

  useEffect(() => {
    const onUnauthorized = () => {
      resetAuthClientState()
      setSnapshot(null)
      setAuthState('anonymous')
    }
    window.addEventListener('accesspilot:unauthorized', onUnauthorized)
    return () => window.removeEventListener('accesspilot:unauthorized', onUnauthorized)
  }, [])

  if (authState === 'checking') {
    return error
      ? <BootError message={error} onRetry={() => setAttempt((value) => value + 1)} />
      : <LoadingScreen />
  }
  if (authState === 'anonymous') {
    return (
      <LoginScreen
        onAuthenticated={async () => {
          setAuthState('checking')
          setError(null)
          setAttempt((value) => value + 1)
        }}
      />
    )
  }
  if (error) {
    return <BootError message={error} onRetry={() => setAttempt((value) => value + 1)} />
  }
  if (snapshot === null) return <LoadingScreen />
  return (
    <WorkbenchRuntime snapshot={snapshot}>
      <WorkbenchPage
        onLogout={async () => {
          await logout()
          setSnapshot(null)
          setAuthState('anonymous')
        }}
      />
    </WorkbenchRuntime>
  )
}
