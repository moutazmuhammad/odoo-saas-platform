from odoo import api, models


class ResGroups(models.Model):
    _inherit = 'res.groups'

    @api.model
    def _saas_grandfather_pod_shell_group(self):
        """SEC-005: give every current SaaS Manager the new, narrower Pod
        Shell group too, so introducing it (see saas_security.xml) doesn't
        silently revoke pod-exec access for whoever already had it via
        Manager alone before this change. Invoked via a <function> call in
        saas_security.xml, outside the noupdate block, so it re-runs on
        every module upgrade — including the one that ships this change,
        without needing direct access to whatever the real database looks
        like. Idempotent: only ADDS the group to users who don't already
        have it, never removes it from anyone, and does nothing on repeat
        runs once every manager already has it. New Manager grants from
        this point on do NOT automatically include Pod Shell — that is
        the point of splitting the group out."""
        manager_group = self.env.ref(
            'saas_core.group_saas_manager', raise_if_not_found=False)
        pod_shell_group = self.env.ref(
            'saas_core.group_saas_pod_shell', raise_if_not_found=False)
        if not manager_group or not pod_shell_group:
            return
        missing = manager_group.users - pod_shell_group.users
        if missing:
            pod_shell_group.write({'users': [(4, u.id) for u in missing]})
