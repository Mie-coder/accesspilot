import {
  AssistantRuntimeProvider,
  type ChatModelAdapter,
  useLocalRuntime,
} from '@assistant-ui/react'
import { render, screen, waitFor } from '@testing-library/react'
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

function RunningThread() {
  const runtime = useLocalRuntime(idleAdapter)
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
  it('shows progress without offering a misleading cancel action', async () => {
    render(<RunningThread />)

    await waitFor(() => {
      expect(screen.getByRole('status')).toHaveTextContent('后端正在解析并校验草稿')
    })
    expect(screen.queryByRole('button', { name: '停止生成' })).not.toBeInTheDocument()
  })
})
