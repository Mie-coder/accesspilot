import { FileLock2, Route, ShieldAlert } from 'lucide-react'

import type {
  DecisionPacket,
  DecisionPacketGenerationMode,
  DecisionPacketSourceKind,
} from './types'

const sourceLabels: Record<DecisionPacketSourceKind, string> = {
  verified_fact: '已验证事实',
  policy_evidence: '政策证据',
  user_claim: '申请人说明',
  advisory: '建议',
}

const modeLabels: Record<DecisionPacketGenerationMode, string> = {
  provider: 'DeepSeek AI 建议',
  deterministic: '规则建议',
  unavailable: '建议不可用',
}

const routeRoleLabels: Record<string, string> = {
  manager: '直属经理',
  data_owner: '数据负责人',
}

export function DecisionPacketPanel({ packet }: { packet: DecisionPacket }) {
  return (
    <section className="decision-packet-panel" aria-labelledby={`packet-${packet.packet_id}`}>
      <div className="decision-packet-heading">
        <div>
          <p className="eyebrow">IMMUTABLE DECISION PACKET</p>
          <h3 id={`packet-${packet.packet_id}`}>决策材料</h3>
          <p>事实、政策、申请人说明与建议分层展示；建议不改变固定审批路线。</p>
        </div>
        <FileLock2 size={19} aria-hidden="true" />
      </div>

      <div className={`packet-mode is-${packet.generation_mode}`}>
        <ShieldAlert size={15} aria-hidden="true" />
        <strong>{modeLabels[packet.generation_mode]}</strong>
        <span>{packet.generation_mode}</span>
      </div>

      <div className="packet-versions" aria-label="决策材料版本">
        <span>Packet {packet.packet_version}</span>
        <span>Catalog {packet.catalog_version}</span>
      </div>

      <div className="packet-route">
        <div className="packet-route-title"><Route size={14} /><strong>固定人工审批路线</strong></div>
        <ol>
          {packet.fixed_route.map((step) => (
            <li key={`${step.step_order}-${step.approver_id}`}>
              <span>{step.step_order}</span>
              <strong>{routeRoleLabels[step.approver_role] ?? step.approver_role}</strong>
              <small>{step.approver_id}</small>
            </li>
          ))}
        </ol>
      </div>

      <ul className="packet-items" aria-label="决策材料来源">
        {packet.items.map((item) => (
          <li key={`${item.source_kind}-${item.source_ref}`} className={`is-${item.source_kind}`}>
            <div className="packet-item-topline">
              <span>{sourceLabels[item.source_kind]}</span>
              <small>{item.source_version}</small>
            </div>
            <strong>{item.title}</strong>
            <p>{item.content}</p>
          </li>
        ))}
      </ul>

      {packet.availability_message ? (
        <p className="packet-unavailable-note">{packet.availability_message}</p>
      ) : null}
    </section>
  )
}
