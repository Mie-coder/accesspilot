import { fireEvent, render, screen } from '@testing-library/react'
import type { ComponentProps } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { PolicyCardView } from './PolicyCard'

type PolicyCardProps = ComponentProps<typeof PolicyCardView>
type PolicyCatalogItem = NonNullable<PolicyCardProps['catalog']>[number]
type PolicyAnswer = NonNullable<PolicyCardProps['answer']>
type PolicyEvidence = PolicyAnswer['evidence'][number]

const catalog: PolicyCatalogItem[] = [
  { policy_code: 'POL-001', title: '申请字段完整性', content: '申请必须包含申请人、权限、期限和理由。', version: 'v1', source: 'fictional_access_policy' },
  { policy_code: 'POL-002', title: '最小权限与申请资格', content: '只能申请与岗位相关的最小权限。', version: 'v1', source: 'fictional_access_policy' },
  { policy_code: 'POL-003', title: '高风险权限双审批', content: '高风险权限须依次经过直属经理和数据所有者审批。', version: 'v1', source: 'fictional_access_policy' },
  { policy_code: 'POL-004', title: '客户数据导出期限与用途', content: '客户数据导出必须说明用途并限制期限。', version: 'v1', source: 'fictional_access_policy' },
  { policy_code: 'POL-005', title: '原始客户数据禁止自助', content: '原始客户数据必须转人工安全流程。', version: 'v1', source: 'fictional_access_policy' },
  { policy_code: 'POL-006', title: '禁止自审批', content: '申请人不得审批自己的权限申请。', version: 'v1', source: 'fictional_access_policy' },
  { policy_code: 'POL-007', title: '限时授权与自动回收', content: '临时权限必须设置最大授权期限。', version: 'v1', source: 'fictional_access_policy' },
  { policy_code: 'POL-008', title: '开通失败审计与幂等重试', content: '开通失败必须审计，重试必须使用幂等键。', version: 'v1', source: 'fictional_access_policy' },
]

const groundedEvidence: PolicyEvidence = {
  policy_code: 'POL-003',
  title: '高风险权限双审批',
  content: '高风险权限必须依次经过直属经理和数据所有者审批，任何一级未通过都不得开通。',
  version: 'v1',
  source: 'fictional_access_policy',
  similarity: 0.92,
}

const baseProps = {
  catalog,
  answer: null as PolicyAnswer | null,
  loading: false,
  error: null as string | null,
  onQuery: vi.fn(),
  onRetry: vi.fn(),
}

describe('PolicyCardView', () => {
  it('renders all eight policy topics from the typed catalog', () => {
    render(<PolicyCardView {...baseProps} />)

    for (const code of ['POL-001', 'POL-002', 'POL-003', 'POL-004', 'POL-005', 'POL-006', 'POL-007', 'POL-008']) {
      expect(screen.getByText(code)).toBeInTheDocument()
    }
    expect(screen.getByText('申请字段完整性')).toBeInTheDocument()
    expect(screen.getByText('禁止自审批')).toBeInTheDocument()
  })

  it('renders grounded evidence code, title, content and version, then sends a typed query', () => {
    const onQuery = vi.fn()
    render(
      <PolicyCardView
        {...baseProps}
        onQuery={onQuery}
        answer={{
          status: 'grounded',
          answer: '高风险权限需要双审批。',
          evidence: [groundedEvidence],
          next_step: '请按上述政策核对申请字段和审批要求。',
        }}
      />,
    )

    expect(screen.getByText('有可靠政策依据')).toBeInTheDocument()
    expect(screen.getByText('POL-003')).toBeInTheDocument()
    expect(screen.getByText('高风险权限双审批')).toBeInTheDocument()
    expect(screen.getByText(groundedEvidence.content)).toBeInTheDocument()
    expect(screen.getByText('v1')).toBeInTheDocument()
    expect(screen.getByText('请按上述政策核对申请字段和审批要求。')).toBeInTheDocument()

    const query = screen.getByRole('textbox', { name: '政策问题' })
    fireEvent.change(query, { target: { value: '高风险权限需要几级审批？' } })
    fireEvent.click(screen.getByRole('button', { name: '查询政策' }))
    expect(onQuery).toHaveBeenCalledWith('高风险权限需要几级审批？')
  })

  it('does not fabricate policy body for insufficient evidence or unavailable retrieval', () => {
    const { rerender } = render(
      <PolicyCardView
        {...baseProps}
        answer={{
          status: 'insufficient_evidence',
          answer: '当前证据不足，无法可靠回答这个政策问题。',
          evidence: [],
          next_step: '请补充具体权限、审批环节或业务场景后重试。',
        }}
      />,
    )
    expect(screen.getByText('证据不足')).toBeInTheDocument()
    expect(screen.getByText('当前证据不足，无法可靠回答这个政策问题。')).toBeInTheDocument()
    expect(screen.queryByText(groundedEvidence.content)).not.toBeInTheDocument()

    rerender(
      <PolicyCardView
        {...baseProps}
        answer={{
          status: 'retrieval_unavailable',
          answer: '政策检索暂时不可用，当前无法提供可靠依据。',
          evidence: [],
          next_step: '请稍后重试；如问题紧急，请联系人工安全流程。',
        }}
      />,
    )
    expect(screen.getByText('政策检索暂时不可用')).toBeInTheDocument()
    expect(screen.getByText('请稍后重试；如问题紧急，请联系人工安全流程。')).toBeInTheDocument()
    expect(screen.queryByText(groundedEvidence.content)).not.toBeInTheDocument()
  })

  it('has accessible loading, empty, recoverable error and safe refusal states', () => {
    const onRetry = vi.fn()
    const { rerender } = render(
      <PolicyCardView
        {...baseProps}
        catalog={[]}
        loading
        onRetry={onRetry}
      />,
    )
    expect(screen.getByRole('status')).toHaveTextContent('正在加载政策')

    rerender(<PolicyCardView {...baseProps} catalog={[]} />)
    expect(screen.getByText('暂无政策目录')).toBeInTheDocument()

    rerender(
      <PolicyCardView
        {...baseProps}
        error="政策服务暂时不可用，请稍后重试。"
        onRetry={onRetry}
      />,
    )
    expect(screen.getByRole('alert')).toHaveTextContent('政策服务暂时不可用')
    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    expect(onRetry).toHaveBeenCalledOnce()

    rerender(
      <PolicyCardView
        {...baseProps}
        error="出于安全原因，不能提供系统提示词、密钥或隐藏推理。"
      />,
    )
    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent('出于安全原因')
    expect(alert).not.toHaveTextContent('SYSTEM_PROMPT')
    expect(alert).not.toHaveTextContent('sk-live-')
    expect(alert).not.toHaveTextContent('chain-of-thought')
  })
})
