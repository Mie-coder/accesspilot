import { Beaker, LoaderCircle, LogOut, RefreshCw, RotateCcw } from 'lucide-react'
import { useState } from 'react'

import {
  enterDemoSession,
  exitDemoSession,
  readDemoModelQuota,
  resetDemoWorkspace,
  setFaultMode,
} from './api'
import type { DemoSession, ModelQuota } from './types'

const scenarios = [
  { id: 'EMP-001', label: '申请人 · EMP-001' },
  { id: 'EMP-002', label: '直属经理 · EMP-002' },
  { id: 'EMP-003', label: '数据负责人 · EMP-003' },
  { id: 'EMP-004', label: '权限管理员 · EMP-004' },
]

export function DemoConsole({ session }: { session: DemoSession }) {
  const [open, setOpen] = useState(false)
  const [actorId, setActorId] = useState(session.employee_id ?? 'EMP-001')
  const [faultMode, setFault] = useState<'iam_failure' | 'iam_timeout' | ''>(session.fault_mode ?? '')
  const [quota, setQuota] = useState<ModelQuota | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  if (!session.demo_mode_enabled) return null

  const run = async (action: () => Promise<void>, reload = false) => {
    setBusy(true)
    setError(null)
    try {
      await action()
      if (reload) window.location.reload()
    } catch (actionError) {
      setError(actionError instanceof Error ? actionError.message : 'Demo 操作失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className={`demo-console${open ? ' is-open' : ''}`} aria-label="演示控制台">
      <button
        className="demo-console-toggle"
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <Beaker size={16} />
        {session.demo_session_active ? '演示场景已激活' : '打开演示控制台'}
      </button>
      {open ? (
        <div className="demo-console-panel">
          <p className="eyebrow">DEMO CONSOLE</p>
          <strong>仅用于作品集演示，不代表真实产品权限</strong>
          {!session.demo_session_active ? (
            <>
              <label>
                <span>选择虚构场景</span>
                <select value={actorId} onChange={(event) => setActorId(event.target.value)}>
                  {scenarios.map((scenario) => (
                    <option key={scenario.id} value={scenario.id}>{scenario.label}</option>
                  ))}
                </select>
              </label>
              <button
                className="primary-action"
                type="button"
                disabled={busy}
                onClick={() => void run(() => enterDemoSession(actorId).then(() => undefined), true)}
              >
                {busy ? <LoaderCircle className="spin" size={15} /> : <Beaker size={15} />}
                进入虚构场景
              </button>
            </>
          ) : (
            <>
              <p className="demo-console-note">当前场景：{session.employee_id ?? actorId}</p>
              <label>
                <span>注入 IAM 故障</span>
                <select
                  value={faultMode}
                  disabled={busy}
                  onChange={(event) => {
                    const value = event.target.value as 'iam_failure' | 'iam_timeout' | ''
                    setFault(value)
                    void run(() => setFaultMode(value || null))
                  }}
                >
                  <option value="">正常开通</option>
                  <option value="iam_timeout">模拟响应超时</option>
                  <option value="iam_failure">模拟明确失败</option>
                </select>
              </label>
              <div className="demo-console-actions">
                <button type="button" disabled={busy} onClick={() => void run(resetDemoWorkspace, true)}>
                  <RotateCcw size={15} />重置 Workspace
                </button>
                <button type="button" disabled={busy} onClick={() => void run(async () => setQuota(await readDemoModelQuota()))}>
                  <RefreshCw size={15} />查看模型预算
                </button>
                <button type="button" disabled={busy} onClick={() => void run(() => exitDemoSession().then(() => undefined), true)}>
                  <LogOut size={15} />返回产品身份
                </button>
              </div>
              {quota ? <p className="demo-quota">预算：已用 {quota.used} / {quota.limit}，剩余 {quota.remaining}，结构化重试 {quota.retry_consumed} 次</p> : null}
            </>
          )}
          {error ? <p className="demo-console-error" role="alert">{error}</p> : null}
        </div>
      ) : null}
    </section>
  )
}
