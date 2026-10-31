"""Validate structured claims against actual receipts; never infer delivery from prose."""


def validate_claims(claims, receipts):
    if not isinstance(claims, list):
        return False
    for claim in claims:
        if not isinstance(claim, dict) or set(claim) != {'action_id', 'kind', 'target'}:
            return False
        if not any(receipt.action_id == claim['action_id'] and receipt.supports(claim['kind'], claim['target'])
                   for receipt in receipts):
            return False
    return True
