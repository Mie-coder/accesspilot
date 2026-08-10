import {
  AssistantRuntimeProvider,
  type ChatModelAdapter,
  type ThreadHistoryAdapter,
  useLocalRuntime,
} from '@assistant-ui/react'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useEffect } from 'react'
import { describe, expect, it } from 'vitest'

import { ChatThread } from './ChatThread'

const idleAdapter: ChatModelAdapter = {
  async *run() {
    // 保持请求未完成，用于验证后端不可撤销阶段的 UI。
    await new Promise<never>(() => undefined)
    yield { content: [] }
  },
}

const localHistory: ThreadHistoryAdapter = {
  load: async () => ({ messages: [] }),
  append: async () => undefined,
  delete: async () => undefined,
}

const partialAdapter: ChatModelAdapter = {
  async *run({ abortSignal }) {
    yield { content: [{ type: 'text' as const, text: '半截回答' }] }
    await new Promise<void>((resolve) => {
      if (abortSignal.aborted) {
        resolve()
        return
      }
      abortSignal.addEventListener('abort', () => {
        window.setTimeout(resolve, 25)
      }, { once: true })
    })
  },
}

function RunningThread() {
  const runtime = useLocalRuntime(idleAdapter, { adapters: { history: localHistory } })
  useEffect(() => {
    void runtime.thread.append({
      role: 'user',
      content: [{ type: 'text', text: '我是 EMP-001' }],
    })
  }, [runtime])
  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <ChatThread />
    </AssistantRuntimeProvider>
  )
}



function PartialRunningThread() {
  const runtime = useLocalRuntime(partialAdapter, { adapters: { history: localHistory } })
  useEffect(() => {
    void runtime.thread.append({
      role: 'user',
      content: [{ type: 'text', text: '我是 EMP-001' }],
    })
  }, [runtime])
  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <ChatThread />
    </AssistantRuntimeProvider>
  )
}
describe('ChatThread', () => {
  it('shows progress with an accessible cancel action', async () => {
    render(<RunningThread />)

    await waitFor(() => {
      expect(screen.getByRole('status')).toHaveTextContent('后端正在解析并校验草稿')
    })
    expect(screen.getByRole('button', { name: '停止生成' })).toBeInTheDocument()
  })
})


describe('ChatThread cancellation cleanup', () => {
  it('deletes the partial assistant text and shows a safe retry state', async () => {
    render(<PartialRunningThread />)

    await waitFor(() => expect(screen.getByText('半截回答')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: '停止生成' }))

    await waitFor(() => {
      expect(screen.queryByText('半截回答')).not.toBeInTheDocument()
      expect(screen.getByText('本轮已中断，可以重新发送。')).toBeInTheDocument()
    })
    await new Promise((resolve) => window.setTimeout(resolve, 50))
    expect(screen.queryByText('半截回答')).not.toBeInTheDocument()
  })
})
