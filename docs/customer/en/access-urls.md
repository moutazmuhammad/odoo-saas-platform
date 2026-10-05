# Instance addresses, HTTPS, and website access

Understand subdomains, secure transport, public pages, and application login.

Reviewed: 2026-10-05

## Your instance address

Choose an available subdomain and a platform-provided Domain during configuration. The resulting address identifies the instance. Environment names can receive a project-derived unique subdomain. Names and database prefixes are technical identifiers; avoid renaming assumptions after creation.

## HTTPS and public access

Provisioning configures HTTPS through the platform infrastructure. A certificate or routing failure requires infrastructure diagnosis, not a database password reset. With an available customer database, visitors reach the Odoo website/application; with no customer database they see the no-database page. Whether an Odoo page is public is controlled by your application configuration.

## Custom addresses and access

Use Open on the instance or database page for the correct address. VELTNEX IAM controls infrastructure tools, while Odoo controls application users and public website content. A custom Domain or region move is not exposed as a general self-service editor in the current portal; contact support to check availability and migration requirements.

## Related guides

- [Launch your first hosting project](launch-instance.md)
- [Database Manager and administrator passwords](database-access.md)
- [Account settings, language, and sessions](account-settings.md)
