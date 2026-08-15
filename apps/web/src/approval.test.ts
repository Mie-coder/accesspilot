import { describe, expect, it } from 'vitest'

import { approvalRoleFor } from './approval'

describe('approvalRoleFor', () => {
  it('exposes approval workbench only for backend-provided duties', () => {
    expect(approvalRoleFor({
      employee_id: 'EMP-002',
      name: '周敏',
      department: 'product_operations',
      roles: ['manager'],
    })).toBe('直属经理')
    expect(approvalRoleFor({
      employee_id: 'EMP-003',
      name: '李强',
      department: 'data_platform',
      roles: ['data_owner'],
    })).toBe('数据负责人')
    expect(approvalRoleFor({
      employee_id: 'EMP-004',
      name: '何川',
      department: 'security_operations',
      roles: ['employee', 'permissions_admin'],
    })).toBe('权限管理员')
    expect(approvalRoleFor({
      employee_id: 'EMP-001',
      name: '林晓',
      department: 'engineering',
      roles: ['employee'],
    })).toBeNull()
  })
})
