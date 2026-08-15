import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { queryPolicy, readPolicyCatalog } from './api'
import { PolicyCard } from './PolicyCard'
import type { PolicyAnswer } from './types'

vi.mock('./api', () => ({
  queryPolicy: vi.fn(),
  readPolicyCatalog: vi.fn(),
}))

const firstAnswer: PolicyAnswer = {
  status: 'grounded',
  answer: '第一条回答',
  evidence: [{
    policy_code: 'POL-001',
    title: '第一条依据',
    content: '第一条政策正文',
    version: 'v1',
    source: 'fictional_access_policy',
    similarity: 0.9,
  }],
  next_step: '第一条下一步',
}

const secondAnswer: PolicyAnswer = {
  status: 'grounded',
  answer: '第二条回答',
  evidence: [{
    policy_code: 'POL-002',
    title: '第二条依据',
    content: '第二条政策正文',
    version: 'v1',
    source: 'fictional_access_policy',
    similarity: 0.88,
  }],
  next_step: '第二条下一步',
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

describe('PolicyCard container query lifecycle', () => {
  beforeEach(() => {
    vi.mocked(queryPolicy).mockReset()
    vi.mocked(readPolicyCatalog).mockReset()
  })

  it('clears a previous answer while loading and after a new query fails', async () => {
    vi.mocked(readPolicyCatalog).mockResolvedValue([])
    const failed = deferred<PolicyAnswer>()
    vi.mocked(queryPolicy)
      .mockResolvedValueOnce(firstAnswer)
      .mockReturnValueOnce(failed.promise)

    render(<PolicyCard />)
    const query = await screen.findByRole('textbox', { name: '政策问题' })
    fireEvent.change(query, { target: { value: '第一问' } })
    fireEvent.click(screen.getByRole('button', { name: '查询政策' }))
    await screen.findByText('第一条回答')

    fireEvent.change(query, { target: { value: '失败的问题' } })
    fireEvent.click(screen.getByRole('button', { name: '查询政策' }))
    expect(screen.queryByText('第一条回答')).not.toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('正在加载政策')

    failed.reject(new Error('政策服务暂时不可用，请稍后重试。'))
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('政策服务暂时不可用'))
    expect(screen.queryByText('第一条政策正文')).not.toBeInTheDocument()
  })

  it('ignores an older out-of-order response after a newer query wins', async () => {
    vi.mocked(readPolicyCatalog).mockResolvedValue([])
    const first = deferred<PolicyAnswer>()
    const second = deferred<PolicyAnswer>()
    vi.mocked(queryPolicy)
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise)

    render(<PolicyCard />)
    const query = await screen.findByRole('textbox', { name: '政策问题' })
    fireEvent.change(query, { target: { value: '旧问题' } })
    fireEvent.click(screen.getByRole('button', { name: '查询政策' }))
    fireEvent.change(query, { target: { value: '新问题' } })
    fireEvent.click(screen.getByRole('button', { name: '查询政策' }))

    second.resolve(secondAnswer)
    await screen.findByText('第二条回答')
    first.resolve(firstAnswer)
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(screen.queryByText('第一条回答')).not.toBeInTheDocument()
    expect(screen.getByText('第二条政策正文')).toBeInTheDocument()
  })
})
