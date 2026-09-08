import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { DecisionPacketPanel } from './DecisionPacketPanel'
import type { DecisionPacket, DecisionPacketGenerationMode } from './types'

const packet: DecisionPacket = {
  packet_id: '22222222-2222-4222-8222-222222222222',
  request_id: '11111111-1111-4111-8111-111111111111',
  packet_version: 'v1',
  catalog_version: 'fictional-catalog-v1',
  generation_mode: 'provider',
  created_at: '2026-08-12T01:00:00Z',
  frozen_request: {
    requester_id: 'EMP-001',
    requester_name: '林晓',
    entitlement_code: 'insighthub.customer_export',
    entitlement_name: '脱敏客户数据导出',
    duration_days: 14,
    justification: '季度客户分析',
    request_status: 'submitted',
    confirmed_at: '2026-08-12T00:58:00Z',
  },
  catalog: {
    risk_level: 'high',
    approval_policy: 'manager_and_data_owner',
    max_duration_days: 30,
  },
  fixed_route: [
    { step_order: 1, approver_id: 'EMP-002', approver_role: 'manager' },
    { step_order: 2, approver_id: 'EMP-003', approver_role: 'data_owner' },
  ],
  items: [
    {
      source_kind: 'verified_fact',
      source_ref: 'request:11111111-1111-4111-8111-111111111111',
      source_version: 'v1',
      title: '已验证申请事实',
      content: '林晓申请脱敏客户数据导出，期限 14 天。',
      generation_mode: null,
    },
    {
      source_kind: 'policy_evidence',
      source_ref: 'policy:POL-003',
      source_version: '2026.1',
      title: '高风险权限双审批',
      content: '高风险权限必须依次经过直属经理和数据负责人审批。',
      generation_mode: null,
    },
    {
      source_kind: 'user_claim',
      source_ref: 'request:11111111-1111-4111-8111-111111111111:justification',
      source_version: 'v1',
      title: '申请人说明',
      content: '季度客户分析',
      generation_mode: null,
    },
    {
      source_kind: 'advisory',
      source_ref: 'advisory:decision-packet',
      source_version: 'v1',
      title: 'AI 建议',
      content: '申请需要固定人工审批。',
      generation_mode: 'provider',
    },
  ],
  advisory: {
    assessment: 'risk',
    summary: '申请需要固定人工审批。',
    unknowns: [],
    recommendations: ['核对最小权限范围'],
    citations: ['POL-003'],
  },
  availability_message: null,
}

describe('DecisionPacketPanel', () => {
  it('labels all four source classes, versions, and the frozen approval route', () => {
    render(<DecisionPacketPanel packet={packet} />)

    for (const sourceLabel of ['申请概况', '审批依据', 'AI 审查建议', '申请人填写']) {
      expect(screen.getAllByText(sourceLabel).length).toBeGreaterThan(0)
    }
    expect(screen.getByText('外部模型生成的建议')).toBeInTheDocument()
    expect(screen.queryByText('查看原始历史建议')).not.toBeInTheDocument()
    expect(screen.getByText('provider')).toBeInTheDocument()
    expect(screen.getByText(/Packet v1/)).toBeInTheDocument()
    expect(screen.getByText(/Catalog fictional-catalog-v1/)).toBeInTheDocument()
    expect(screen.getByText(/EMP-002/)).toBeInTheDocument()
    expect(screen.getByText(/EMP-003/)).toBeInTheDocument()
  })

  it.each([
    ['provider', '外部模型生成的建议'],
    ['deterministic', '规则建议'],
    ['unavailable', '建议不可用'],
  ] satisfies Array<[DecisionPacketGenerationMode, string]>) (
    'renders %s without presenting offline output as real AI',
    (generationMode, label) => {
      render(
        <DecisionPacketPanel
          packet={{
            ...packet,
            generation_mode: generationMode,
            advisory: generationMode === 'unavailable' ? null : packet.advisory,
            availability_message: generationMode === 'unavailable'
              ? 'AI 风险建议不可用；固定路线不受影响。'
              : null,
            items: packet.items.map((item) => item.source_kind === 'advisory'
              ? { ...item, generation_mode: generationMode }
              : item),
          }}
        />,
      )

      expect(screen.getByText(label)).toBeInTheDocument()
      expect(screen.getByText(generationMode)).toBeInTheDocument()
      if (generationMode !== 'provider') {
        expect(screen.queryByText('外部模型生成的建议')).not.toBeInTheDocument()
      }
    },
  )

  it('is a read-only evidence view with no future approval or provisioning actions', () => {
    render(<DecisionPacketPanel packet={packet} />)

    for (const action of ['创建审批路线', '批准', '驳回', '开通权限']) {
      expect(screen.queryByRole('button', { name: action })).not.toBeInTheDocument()
    }
  })
})


it('separates applicable evidence, references and conflicting frozen advice', () => {
  const original = JSON.stringify(packet)
  render(<DecisionPacketPanel packet={{ ...packet,
    frozen_request: { ...packet.frozen_request, entitlement_code: 'insighthub.dashboard_view', entitlement_name: '仪表盘查看' },
    catalog: { risk_level: 'low', approval_policy: 'manager', max_duration_days: 180 },
    fixed_route: [packet.fixed_route[0]!],
    advisory: { ...packet.advisory!, summary: '缺失身份和具体权限，无法核实完整性' },
    items: [...packet.items.filter(x => x.source_kind !== 'policy_evidence'),
      ...[
        ['POL-001', '申请字段完整性'], ['POL-003', '高风险权限双审批'],
        ['POL-004', '客户数据导出期限与用途'], ['POL-006', '禁止自审批'],
      ].map(([code, title]) => ({ source_kind: 'policy_evidence' as const,
        source_ref: `policy:${code}`, source_version: 'v1', title: title!, content: title!, generation_mode: null })),
    ],
  }} />)
  const reference = screen.getByText('参考政策').closest('details')!
  expect(reference).not.toHaveAttribute('open')
  expect(within(reference).getByText('高风险权限双审批', { selector: 'strong' })).toBeInTheDocument()
  expect(within(reference).getByText('客户数据导出期限与用途', { selector: 'strong' })).toBeInTheDocument()
  expect(within(reference).queryByText('禁止自审批')).not.toBeInTheDocument()
  expect(screen.getByText('技术详情').closest('details')).not.toHaveAttribute('open')
  expect(screen.getByRole('alert')).toHaveTextContent('把未提供给模型的信息当成了申请未填写')
  expect(screen.getByRole('alert')).toHaveTextContent('林晓')
  expect(screen.getByRole('alert')).toHaveTextContent('仪表盘查看')
  expect(screen.getByRole('alert')).toHaveTextContent('14 天')
  expect(screen.getByRole('alert')).toHaveTextContent('无需按旧建议重复补充')
  const historical = screen.getByText('查看原始历史建议').closest('details')!
  expect(historical).not.toHaveAttribute('open')
  expect(historical).toHaveTextContent('缺失身份和具体权限，无法核实完整性')
  expect(screen.getByText('缺失身份和具体权限，无法核实完整性')).toBeInTheDocument()
  expect(JSON.stringify(packet)).toBe(original)
})

it('uses the known policy version and frozen route, leaving unknown versions as references', () => {
  const highRisk = { ...packet, items: packet.items.map(item => item.source_kind === 'policy_evidence'
    ? { ...item, source_version: 'v1' } : item) }
  const { rerender } = render(<DecisionPacketPanel packet={highRisk} />)
  expect(screen.queryByText('参考政策')).not.toBeInTheDocument()
  expect(screen.getByText('高风险权限双审批', { selector: 'strong' })).toBeInTheDocument()
  rerender(<DecisionPacketPanel packet={packet} />)
  expect(screen.getByText('参考政策').closest('details')).toHaveTextContent('高风险权限双审批')
})

it.each([
  ['clear', '建议通过。期限合理。', [], [], '建议通过'],
  ['risk', '建议补充演示范围说明后再审。', ['演示范围待补充'], ['建议补充演示范围说明后再审。'], '建议补充材料后再审'],
  ['blocked', '建议不通过。用途超出申请范围。', [], [], '建议不通过'],
  ['clear', '目前未见风险信号。', [], [], '暂无法给出可靠建议'],
  ['clear', '建议通过，但前提是补充用途证明。', [], [], '暂无法给出可靠建议'],
  ['risk', '需要人工核对使用期限。', [], ['核对使用期限'], '暂无法给出可靠建议'],
] as const)('shows an evidence-backed conclusion first without treating assessment as approval', (assessment, summary, unknowns, recommendations, label) => {
  render(<DecisionPacketPanel packet={{ ...packet, advisory: {
    assessment, summary, unknowns: [...unknowns], recommendations: [...recommendations], citations: [],
  } }} />)
  const section = screen.getByRole('region', { name: 'AI 审查建议' })
  expect(within(section).getByLabelText('建议结论')).toHaveTextContent(label)
  expect(within(section).getByText('仅供人工参考，不构成审批结论。')).toBeInTheDocument()
  expect(within(section).queryByText('查看原始历史建议')).not.toBeInTheDocument()
  expect(within(section).getAllByText(summary, { exact: true })).toHaveLength(1)
})
