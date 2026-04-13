"""User provisioning / access grants / offboarding / license audit.

Every write path in this package goes through `shared.mcp.salesforce_mcp.create_record`
or `update_record`, which enforces the `require_approved_gate` contract. The
approval gate action_types used here are declared in `shared.governance.APPROVAL_TIERS`:

  user_provisioning     — create User + attach ProfileId/UserRoleId
  permission_grant      — assign PermissionSet, add to Group, grant FLS
  license_deactivation  — deactivate User (offboarding) + reclaim license

`license_audit` is read-only and does not require a gate; it surfaces a task
for every inactive paid user found.
"""
