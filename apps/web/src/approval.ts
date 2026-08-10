import type { WorkspaceIdentity } from './types'

export function approvalRoleFor(identity: WorkspaceIdentity): string | null {
  if (identity.roles.includes('manager')) return '直属经理'
  if (identity.roles.includes('data_owner')) return '数据负责人'
  if (identity.roles.includes('permissions_admin')) return '权限管理员'
  return null
}
