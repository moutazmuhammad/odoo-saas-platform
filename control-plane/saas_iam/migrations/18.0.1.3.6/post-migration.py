def migrate(cr, version):
    # Preserve accounts that completed onboarding before this protection existed.
    cr.execute("""
        UPDATE res_users SET iam_account_verified = TRUE
        WHERE iam_managed_owner_id IS NOT NULL
          AND COALESCE(iam_verified_phone, '') <> ''
    """)
