import { FileLock2, Route } from 'lucide-react'
import type { DecisionPacket, DecisionPacketItem, DecisionPacketGenerationMode } from './types'

const modeLabels: Record<DecisionPacketGenerationMode, string> = {
  provider: '外部模型生成的建议', deterministic: '规则建议', unavailable: '建议不可用',
}
const routeRoleLabels: Record<string, string> = { manager: '直属经理', data_owner: '数据负责人' }
const scopes: Record<string, string> = {
  'policy:POL-001': '所有权限申请的字段完整性',
  'policy:POL-002': '申请资格与最小权限',
  'policy:POL-003': '高风险权限的双审批',
  'policy:POL-004': '客户数据导出权限',
  'policy:POL-005': '原始或未脱敏客户数据',
  'policy:POL-006': '所有人工审批的身份分离',
  'policy:POL-007': '临时授权的期限与回收',
  'policy:POL-008': '开通失败后的审计与重试',
}

function applies(item: DecisionPacketItem, packet: DecisionPacket): boolean {
  // Only classify the known fictional v1 contract. Unknown versions remain references.
  if (item.source_version !== 'v1' || packet.catalog_version !== 'fictional-catalog-v1') return false
  if (['policy:POL-001', 'policy:POL-006'].includes(item.source_ref)) return true
  if (item.source_ref === 'policy:POL-003') return packet.catalog.risk_level === 'high'
    && packet.catalog.approval_policy === 'manager_and_data_owner'
    && packet.fixed_route.some(step => step.approver_role === 'data_owner')
  if (item.source_ref === 'policy:POL-004') return packet.frozen_request.entitlement_code === 'insighthub.customer_export'
  return false
}

function PolicyList({ items }: { items: DecisionPacketItem[] }) {
  return <ul className="packet-policy-list">{items.map(item => <li key={item.source_ref}>
    <strong>{item.title}</strong><p>{item.content}</p>
    <small>条款范围：{item.source_version === 'v1' ? scopes[item.source_ref] ?? '需核对原文' : '版本未归类，需核对原文'}</small>
    <small>来源：{item.source_ref} · {item.source_version}</small>
  </li>)}</ul>
}

function advisoryConclusion(packet: DecisionPacket, contradicts: boolean): string {
  const advice = packet.advisory
  if (contradicts || !advice || packet.generation_mode === 'unavailable') return '暂无法给出可靠建议'
  const statements = [advice.summary, ...advice.recommendations].map(text => text.trim())
  // Assessment describes risk, not an approval recommendation. Require explicit supporting text.
  const pass = statements.some(text => /^(建议通过|建议批准)([。；，：]|$)/.test(text))
  const reject = statements.some(text => /^(建议不通过|建议拒绝|不建议通过)([。；，：]|$)/.test(text))
  const supplement = statements.some(text => /^建议补充.*(材料|证明|说明|依据|信息)/.test(text))
  const conditional = statements.some(text => /但|前提|补充|后再|尚需|仍需|需要先/.test(text))
  if (pass && !reject && !supplement && !conditional && advice.assessment === 'clear' && advice.unknowns.length === 0) return '建议通过'
  if (reject && !pass && !supplement && advice.assessment !== 'clear') return '建议不通过'
  if (supplement && !pass && !reject) return '建议补充材料后再审'
  return '暂无法给出可靠建议'
}

export function DecisionPacketPanel({ packet }: { packet: DecisionPacket }) {
  const facts = packet.frozen_request
  const policies = packet.items.filter(item => item.source_kind === 'policy_evidence')
  const applicable = policies.filter(item => applies(item, packet))
  const references = policies.filter(item => !applies(item, packet))
  const advisory = packet.advisory
  const advisoryText = [advisory?.summary, ...(advisory?.unknowns ?? []), ...(advisory?.recommendations ?? [])].join('\n')
  const contradicts = /(?:缺少|缺失|未提供|未填写).{0,18}(?:身份|申请人|具体权限|权限名称|权限编码)|(?:身份|申请人|具体权限|权限名称|权限编码).{0,12}(?:缺少|缺失|未提供|未填写)/.test(advisoryText)
    && Boolean(facts.requester_id && facts.entitlement_code)
  const conclusion = advisoryConclusion(packet, contradicts)
  const followUps = advisory ? [...new Set([...advisory.unknowns, ...advisory.recommendations])]
    .filter(text => text !== advisory.summary) : []
  const advisoryBody = advisory ? <><p className="packet-advisory-summary">{advisory.summary}</p>
        {advisory.unknowns.length ? <><h5>仍需人工核对</h5><ul>{advisory.unknowns.map(text => <li key={text}>{text}</li>)}</ul></> : null}
        {advisory.recommendations.length ? <><h5>建议核对事项</h5><ul>{advisory.recommendations.map(text => <li key={text}>{text}</li>)}</ul></> : null}
      </> : null
  return <section className="decision-packet-panel" aria-labelledby={`packet-${packet.packet_id}`}>
    <div className="decision-packet-heading"><div>
      <p className="eyebrow">审批时的冻结记录</p><h3 id={`packet-${packet.packet_id}`}>决策材料</h3>
      <p>以下内容保存自申请时，不随目录或建议模型变化而重写。</p>
    </div><FileLock2 size={19} aria-hidden="true" /></div>

    <section className="packet-section" aria-label="申请概况"><h4>申请概况</h4>
      <dl className="packet-facts">
        <div><dt>申请人 · 已验证</dt><dd>{facts.requester_name} · {facts.requester_id}</dd></div>
        <div><dt>申请权限 · 已验证</dt><dd>{facts.entitlement_name}</dd></div>
        <div><dt>申请期限</dt><dd>{facts.duration_days} 天</dd></div>
        <div><dt>目录风险等级</dt><dd>{{ low: '低风险', high: '高风险', critical: '极高风险' }[packet.catalog.risk_level] ?? packet.catalog.risk_level}</dd></div>
      </dl>
      <div className="packet-claim"><strong>业务理由</strong><span>申请人填写</span><p>{facts.justification}</p>
        <small>已记录原话；业务用途的真实性仍需人工核对。</small></div>
    </section>

    <section className="packet-section" aria-label="审批依据"><h4>审批依据</h4>
      <div className="packet-route"><div className="packet-route-title"><Route size={14} /><strong>固定人工审批路线</strong></div>
        <ol>{packet.fixed_route.map(step => <li key={`${step.step_order}-${step.approver_id}`}>
          <span>{step.step_order}</span><strong>{routeRoleLabels[step.approver_role] ?? step.approver_role}</strong><small>{step.approver_id}</small>
        </li>)}</ol>
        <p>路线由后端目录和业务规则确定，AI 建议不能更改。</p>
      </div>
      {packet.catalog.max_duration_days !== null ? <p>目录允许的最长期限：{packet.catalog.max_duration_days} 天。</p> : null}
      <h5>本次适用政策</h5>
      {applicable.length ? <PolicyList items={applicable} /> : <p>检索材料尚不能可靠归类为本次适用政策，请结合固定路线核对。</p>}
      {references.length ? <details className="packet-reference"><summary>参考政策</summary>
        <p>这些是检索命中，需核对条款适用范围，不代表本申请必须采用其中的审批流程。</p><PolicyList items={references} />
      </details> : null}
    </section>

    <section className="packet-section" aria-label="AI 审查建议"><h4>AI 审查建议</h4>
      <p className="packet-advisory-summary" aria-label="建议结论"><strong>{conclusion}</strong></p>
      <p>仅供人工参考，不构成审批结论。</p>
      <p className={`packet-mode is-${packet.generation_mode}`}><strong>{modeLabels[packet.generation_mode]}</strong></p>
      {contradicts ? <div className="packet-conflict" role="alert">
        <p><strong>旧建议对“身份或权限缺失”的判断有误。</strong>它把未提供给模型的信息当成了申请未填写。</p>
        <p>这份冻结申请已有以下记录：</p>
        <ul>
          <li>已核验申请人：{facts.requester_name || facts.requester_id}（{facts.requester_id}）。</li>
          <li>已核验权限：{facts.entitlement_name || facts.entitlement_code}。</li>
          {facts.duration_days > 0 ? <li>已记录申请期限：{facts.duration_days} 天。</li> : null}
          {packet.catalog.risk_level ? <li>目录风险等级：{{ low: '低风险', high: '高风险', critical: '极高风险' }[packet.catalog.risk_level] ?? packet.catalog.risk_level}。</li> : null}
        </ul>
        <p>无需按旧建议重复补充这些字段。业务用途是否真实，仍需人工核对。</p>
      </div> : null}
      {contradicts ? <details className="packet-reference"><summary>查看原始历史建议</summary>
        <p>以下是冻结时保存的原文，未重新生成，不作为补充材料的要求。</p>
        {advisoryBody}
      </details> : advisory ? <>
        {conclusion === '暂无法给出可靠建议' ? <p>这份建议没有给出明确、相互一致的通过或不通过意见，不能仅凭风险等级替它作结论。</p> : null}
        <h5>依据</h5><p>{advisory.summary}</p>
        {followUps.length ? <><h5>{conclusion === '建议补充材料后再审' ? '建议补充或核对' : '后续核对事项'}</h5>
          <ul>{followUps.map(text => <li key={text}>{text}</li>)}</ul></> : null}
      </> : null}
      {packet.availability_message ? <p className="packet-unavailable-note">{packet.availability_message}</p> : null}
    </section>

    <details className="packet-technical"><summary>技术详情</summary>
      <div className="packet-versions" aria-label="决策材料版本"><span>Packet {packet.packet_version}</span><span>Catalog {packet.catalog_version}</span><span>{packet.generation_mode}</span></div>
      <p>材料编号：{packet.packet_id}</p><p>冻结时间：{packet.created_at}</p>
      <p>权限编码：{facts.entitlement_code}</p><p>审批策略：{packet.catalog.approval_policy}</p>
      <p>生成来源只记录了模式，未记录具体模型提供方。</p>
      <ul>{packet.items.map(item => <li key={`${item.source_kind}-${item.source_ref}`}>{item.title} · {item.source_ref} · {item.source_version}</li>)}</ul>
      {advisory?.citations.length ? <p>建议引用：{advisory.citations.join('、')}</p> : null}
    </details>
  </section>
}
