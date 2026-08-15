import {
  AssistantRuntimeProvider,
  type ChatModelAdapter,
  type ThreadHistoryAdapter,
  useLocalRuntime,
} from '@assistant-ui/react'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { type ReactNode, useEffect } from 'react'
import { describe, expect, it } from 'vitest'

import { ChatThread, StarterPrompts } from './ChatThread'

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

function EmptyThread() {
  const runtime = useLocalRuntime(idleAdapter)
  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <StarterPrompts />
    </AssistantRuntimeProvider>
  )
}

function ThreadWithConfirmation({ confirmation }: { confirmation: ReactNode }) {
  const runtime = useLocalRuntime(idleAdapter)
  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <ChatThread confirmation={confirmation} />
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
  it('starts from a confirmed login identity instead of asking the user to self-identify', () => {
    render(<EmptyThread />)

    expect(screen.getByText(/登录身份已确认/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '从权限需求开始' })).toBeInTheDocument()
    expect(screen.queryByText(/从员工编号开始/)).not.toBeInTheDocument()
    expect(screen.queryByText(/我是 EMP-001/)).not.toBeInTheDocument()
  })

  it('shows progress with an accessible cancel action', async () => {
    render(<RunningThread />)

    await waitFor(() => {
      expect(screen.getByRole('status')).toHaveTextContent('后端正在解析并校验草稿')
    })
    expect(screen.getByRole('button', { name: '停止生成' })).toBeInTheDocument()
  })

  it('places a confirmation slot in the scrollable conversation before the sticky composer', () => {
    render(
      <ThreadWithConfirmation confirmation={<section data-testid="confirmation-slot">确认申请信息</section>} />,
    )

    const slot = screen.getByTestId('confirmation-slot')
    const viewport = document.querySelector('.thread-viewport')
    const footer = document.querySelector('.thread-footer')

    expect(viewport).toContainElement(slot)
    expect(footer).toBeInTheDocument()
    expect(slot.compareDocumentPosition(footer!)).toBe(Node.DOCUMENT_POSITION_FOLLOWING)
    expect(footer).toContainElement(screen.getByRole('textbox', { name: '描述权限申请' }))
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
