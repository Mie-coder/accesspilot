import { useAui, useAuiState } from '@assistant-ui/react'
import {
  Activity,
  AlertCircle,
  Bot,
  Database,
  LoaderCircle,
  RotateCcw,
  ShieldCheck,
  Sparkles,
} from 'lucide-react'
import { useEffect, useState } from 'react'

import { ApiError, bootstrapWorkspace } from './api'
import { ChatThread } from './ChatThread'
import { DraftCard } from './DraftCard'
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

function WorkbenchPage() {
  const workbench = useWorkbench()
  const aui = useAui()
  const isRunning = useAuiState((state) => state.thread.isRunning)
  const confirm = () => {
    void aui.thread.append({
      role: 'user',
      content: [{ type: 'text', text: '确认提交' }],
    })
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
          <label className="role-select">
            <span>演示角色</span>
            <select aria-label="演示角色" defaultValue="applicant">
              <option value="applicant">申请人 · EMP-001</option>
              <option value="manager" disabled>直属经理 · T07</option>
              <option value="owner" disabled>数据负责人 · T07</option>
            </select>
          </label>
          <div className="api-health">
            <span aria-hidden="true" />
            API 已连接
          </div>
          <button className="icon-button" type="button" onClick={() => void workbench.reset()} aria-label="新建演示空间">
            <RotateCcw size={17} />
          </button>
        </div>
      </header>

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
            <div className="quota-block" aria-label={`模型额度剩余 ${workbench.quota.remaining} 次`}>
              <span>{workbench.quota.remaining}</span>
              <small>/ {workbench.quota.limit} 次</small>
            </div>
          </div>

          {workbench.error ? (
            <div className="recoverable-banner" role="alert">
              <AlertCircle size={17} />
              <span>{workbench.error}</span>
            </div>
          ) : null}
          <ChatThread />
        </section>

        <aside className="right-rail" aria-label="申请状态">
          <DraftCard
            draft={workbench.draft}
            missingFields={workbench.missingFields}
            requestResult={workbench.requestResult}
            isBusy={isRunning || workbench.isSubmitting}
            onConfirm={confirm}
            onSubmit={() => void workbench.submit()}
          />
          <ActivityFeed events={workbench.events} />
          <div className="source-note">
            <Database size={16} />
            <span>Workspace / PostgreSQL 是业务事实源，assistant-ui 只管理对话交互。</span>
          </div>
        </aside>
      </main>
    </div>
  )
}

function LoadingScreen() {
  return (
    <main className="boot-screen" aria-live="polite">
      <div className="boot-mark"><Sparkles size={24} /></div>
      <LoaderCircle className="spin" size={20} />
      <h1>正在准备你的演示空间</h1>
      <p>连接 PostgreSQL 并回放当前 Workspace 的安全事件…</p>
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

  useEffect(() => {
    let active = true
    setError(null)
    void bootstrapWorkspace()
      .then((value) => {
        if (active) setSnapshot(value)
      })
      .catch((bootError: unknown) => {
        if (!active) return
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

  if (error) {
    return <BootError message={error} onRetry={() => setAttempt((value) => value + 1)} />
  }
  if (snapshot === null) return <LoadingScreen />
  return (
    <WorkbenchRuntime snapshot={snapshot}>
      <WorkbenchPage />
    </WorkbenchRuntime>
  )
}
