# Maintaining customer documentation

The canonical editorial source is [docs-catalog.json](../../frontend/veltnex/src/lib/docs-catalog.json). It contains 61 customer articles in 13 categories. Each text has English and editorial Arabic in the same structural node; article IDs, section anchors, related links, code blocks, and tables are shared. Do not translate product names or technical identifiers.

The information architecture follows the familiar overview → task guide → reference → troubleshooting pattern used in [Google Cloud documentation](https://docs.cloud.google.com/docs) and its [IAM overview](https://docs.cloud.google.com/iam/docs/overview). The content describes VELTNEX behavior, not Google Cloud behavior or an Odoo.sh equivalence claim.

## Review policy

Before publishing, compare the relevant controller, frontend permission gate, model, and configured default. Do not rely only on a stale comment or a success message. Each article carries source paths for maintainers; source paths are not rendered in the customer interface. Documentation does not advertise backend-only controls as portal buttons.

Retain existing public article IDs and `/help#...` anchors. Add procedures with prerequisites, the exact required roles, environment scope, confirmation requirements, data consequences, payment/credit behavior, and a recovery or support path where applicable. State configured defaults as defaults; never promise a universal price, duration, SLA, indefinite history, or retention period.

Use the [Arabic glossary](../arabic-localization-glossary.md). Rewrite Arabic for meaning and context. Keep brand names, example code, numbers, file names, URLs, role codes and permission identifiers unchanged. Review mixed Arabic/English prose on desktop and mobile.

## Generate and verify

```sh
python3 scripts/generate-customer-docs.py
python3 scripts/generate-customer-docs.py --check
npm --prefix frontend/veltnex test
npm --prefix frontend/veltnex run build
```

The generator validates unique IDs, real source paths, related links, section IDs, complete bilingual text, table dimensions, technical names, numbers, inline code and links. It exports customer Markdown under `en/` and `ar/`, the downloadable ZIP, and help excerpts used by React and native QWeb. Do not edit generated files independently.

The documentation routes load the full catalog in a separate frontend chunk. Other pages use only the smaller generated help excerpts. Print styling hides navigation and preserves code direction. Runtime logs and terminals are not translated.

QWeb tooltip changes require upgrading only `saas_website` and restarting its workers to refresh cached help content. Customer Odoo modules do not need an upgrade for documentation changes.

## Corrected legacy claims

- Production hosting permits one customer database; Staging/Development are separate.
- Python packages come from `requirements.txt`, not a manual portal package list.
- Selective module upgrades depend on upgrade inputs, not every modified file.
- Creation does not promise an automatic Production clone, demo data, or integration neutralization.
- Server deletion frees a slot; releasing an unused reservation reduces recurring slot capacity and can issue wallet credit.
- Merge changes code, not data. Runtime rollback does not undo database migrations and currently needs support.
- Upgrades apply after the required invoice is settled; downgrades use measured headroom and scheduled plan changes.
- Daily snapshots are separately billed and subject to successful capture and retention.
- Cancellation attempts a fresh retained snapshot; an old snapshot is not a guaranteed fallback.
- IAM project administration does not grant billing or unrestricted Database Manager access.
- A response target, larger replica count, or rolling deployment is not an unconditional availability guarantee.

Review date: 2026-10-05. This is an implementation-based documentation review, not a claim that every integration has been exercised with a live provider.
