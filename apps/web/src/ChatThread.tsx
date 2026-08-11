import {
  AuiIf,
  ComposerPrimitive,
  ErrorPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  useAui,
  useAuiState,
} from '@assistant-ui/react'
import { useCallback, useEffect, useRef } from 'react'

import {
  AlertCircle,
  ArrowDown,
  ArrowUp,
  Bot,
  CircleStop,
  LoaderCircle,
  SendHorizontal,
  UserRound,
} from 'lucide-react'

function ChatMessage() {
  const role = useAuiState((state) => state.message.role)
  const status = useAuiState((state) => state.message.status)
  const isUser = role === 'user'
  const hasError = status?.type === 'incomplete' && status.reason === 'error'
  return (
    <MessagePrimitive.Root className={`message-row ${isUser ? 'is-user' : 'is-assistant'}`}>
      <div className="message-avatar" aria-hidden="true">
        {isUser ? <UserRound size={16} /> : <Bot size={17} />}
      </div>
      <div className="message-body">
        <span className="message-author">{isUser ? '你' : 'AccessPilot'}</span>
        <div className="message-content">
          <MessagePrimitive.Parts />
          {hasError ? (
            <ErrorPrimitive.Root className="message-error">
              <AlertCircle size={15} />
              <ErrorPrimitive.Message />
            </ErrorPrimitive.Root>
          ) : null}
        </div>
      </div>
    </MessagePrimitive.Root>
  )
}

export function StarterPrompts() {
  const aui = useAui()
  const send = (content: string) => {
    void aui.thread.append({ role: 'user', content: [{ type: 'text', text: content }] })
  }
  return (
    <div className="empty-thread">
      <div className="empty-mark" aria-hidden="true">
        <SendHorizontal size={24} />
      </div>
      <p className="eyebrow">APPLICANT WORKBENCH</p>
      <h2>从一句真实需求开始</h2>
      <p>登录身份已确认。我会逐项收集权限、期限和理由。信息完整后，仍需你明确确认。</p>
      <div className="starter-actions">
        <button type="button" onClick={() => send('我需要申请一个权限')}>
          从权限需求开始
        </button>
        <button
          type="button"
          onClick={() =>
            send(
              '我需要 insighthub.customer_export 权限 14 天，用于季度客户分析。',
            )
          }
        >
          填入完整申请示例
        </button>
      </div>
    </div>
  )
}

function Composer() {
  const aui = useAui()
  const messages = useAuiState((state) => state.thread.messages)
  const isRunning = useAuiState((state) => state.thread.isRunning)
  const pendingCancelIds = useRef(new Set<string>())
  const interruptionKeys = useRef(new Set<string>())
  const cancelSequence = useRef(0)

  const appendInterruptedState = useCallback((key: string) => {
    if (interruptionKeys.current.has(key)) return
    interruptionKeys.current.add(key)
    aui.thread.append({
      role: 'assistant',
      content: [{ type: 'text', text: '本轮已中断，可以重新发送。' }],
      startRun: false,
    })
  }, [aui])

  const deleteCancelledMessage = useCallback((messageId: string) => {
    void Promise.resolve(aui.thread.deleteMessage(messageId)).catch(() => undefined)
  }, [aui])

  useEffect(() => {
    for (const messageId of pendingCancelIds.current) {
      if (messages.some((message) => message.id === messageId)) {
        deleteCancelledMessage(messageId)
      } else if (!isRunning) {
        pendingCancelIds.current.delete(messageId)
      }
    }
  }, [deleteCancelledMessage, isRunning, messages])

  const cancel = useCallback(() => {
    const assistant = [...messages].reverse().find(
      (message) => message.role === 'assistant' && message.status?.type === 'running',
    )
    const messageId = assistant?.id
    const key = messageId ?? `cancel-${cancelSequence.current++}`
    if (messageId) pendingCancelIds.current.add(messageId)
    aui.thread.cancelRun()
    if (messageId) {
      const deletion = Promise.resolve(aui.thread.deleteMessage(messageId)).catch(() => undefined)
      void deletion.then(() => appendInterruptedState(key))
    } else {
      appendInterruptedState(key)
    }
  }, [appendInterruptedState, aui, messages])

  return (
    <div className="composer-wrap">
      <AuiIf condition={(state) => state.thread.isRunning}>
        <div className="streaming-status" role="status">
          <LoaderCircle className="spin" size={14} />
          后端正在解析并校验草稿…
        </div>
      </AuiIf>
      <ComposerPrimitive.Root className="composer-root">
        <ComposerPrimitive.Input
          className="composer-input"
          aria-label="描述权限申请"
          placeholder="例如：需要 insighthub.customer_export 权限 14 天…"
          rows={1}
          unstable_insertNewlineOnTouchEnter
        />
        <AuiIf condition={(state) => !state.thread.isRunning}>
          <ComposerPrimitive.Send className="composer-send" aria-label="发送消息">
            <ArrowUp size={18} strokeWidth={2.5} />
          </ComposerPrimitive.Send>
        </AuiIf>
        <AuiIf condition={(state) => state.thread.isRunning}>
          <button
            className="composer-send is-running"
            type="button"
            aria-label="停止生成"
            onClick={cancel}
          >
            <CircleStop size={18} />
          </button>
        </AuiIf>
      </ComposerPrimitive.Root>
      <p className="composer-hint">Enter 发送 · Shift + Enter 换行 · 所有模型密钥仅保存在后端</p>
    </div>
  )
}

export function ChatThread() {
  return (
    <ThreadPrimitive.Root className="thread-root">
      <ThreadPrimitive.Viewport className="thread-viewport" turnAnchor="top">
        <AuiIf condition={(state) => state.thread.isEmpty}>
          <StarterPrompts />
        </AuiIf>
        <ThreadPrimitive.Messages>
          {() => <ChatMessage />}
        </ThreadPrimitive.Messages>
        <ThreadPrimitive.ViewportFooter className="thread-footer">
          <ThreadPrimitive.ScrollToBottom className="scroll-bottom" aria-label="滚动到最新消息">
            <ArrowDown size={16} />
          </ThreadPrimitive.ScrollToBottom>
          <Composer />
        </ThreadPrimitive.ViewportFooter>
      </ThreadPrimitive.Viewport>
    </ThreadPrimitive.Root>
  )
}
