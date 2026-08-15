export interface EntitlementNameFact {
  code: string
  name: string | null
}

export function entitlementNameForCode(
  fact: EntitlementNameFact | null,
  entitlementCode: string | null,
): string | null {
  return fact?.code === entitlementCode ? fact.name : null
}
