def migrate(cr, version):
    # Earlier owner-created accounts recorded their actual creator even under sudo.
    # Existing/imported logins keep independent ownership and cannot be reset by a customer.
    cr.execute("""
        UPDATE res_users AS u SET iam_managed_owner_id = m.owner_id
        FROM saas_iam_member AS m, res_users AS creator
        WHERE m.user_id = u.id AND creator.id = u.create_uid
          AND creator.partner_id = m.owner_id AND u.share = true
          AND u.iam_managed_owner_id IS NULL
          AND NOT EXISTS (
            SELECT 1 FROM saas_iam_member AS other
            WHERE other.user_id = u.id AND other.owner_id <> m.owner_id
          )
    """)
