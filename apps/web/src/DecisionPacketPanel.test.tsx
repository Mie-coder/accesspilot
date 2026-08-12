import { render, screen } from '@testing-library/react'
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

    for (const sourceLabel of ['已验证事实', '政策证据', '申请人说明', '建议']) {
      expect(screen.getAllByText(sourceLabel).length).toBeGreaterThan(0)
    }
    expect(screen.getByText('DeepSeek AI 建议')).toBeInTheDocument()
    expect(screen.getByText('provider')).toBeInTheDocument()
    expect(screen.getByText(/Packet v1/)).toBeInTheDocument()
    expect(screen.getByText(/Catalog fictional-catalog-v1/)).toBeInTheDocument()
    expect(screen.getByText(/EMP-002/)).toBeInTheDocument()
    expect(screen.getByText(/EMP-003/)).toBeInTheDocument()
  })

  it.each([
    ['provider', 'DeepSeek AI 建议'],
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
        expect(screen.queryByText('DeepSeek AI 建议')).not.toBeInTheDocument()
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
