# Arabic editorial glossary

Use clear Modern Standard Arabic. Write for customers managing a SaaS product,
not for readers of a literal translation. Translate the purpose of a message;
review the complete paragraph or dialog before choosing its wording.

## Shared terminology

| English | Arabic UI wording | Context |
| --- | --- | --- |
| User | المستخدم | Account holder |
| Customer | العميل | Customer account, consistently |
| Project | المشروع | Customer project |
| My projects | مشاريعي | Projects owned by the current user |
| Shared projects | المشاريع المشتركة | Projects shared with the current user |
| Owner | المالك | Account or project owner |
| Teammate | عضو الفريق | Avoid زميله or changing the member's gender |
| Team & permissions | الفريق والصلاحيات | Main team-management page |
| Role | الدور | Predefined IAM role; role codes stay unchanged |
| Permission | الصلاحية | A specific allowed action; permission codes stay unchanged |
| Grant access | منح الوصول | Assign permissions |
| Remove access | إلغاء الوصول | Revoke permissions |
| Instance | Instance / Instances | Keep distinct from the underlying server; use feminine agreement |
| Server | الخادم | The underlying server or environment server |
| Environment | البيئة | Generic environment; named types stay in English |
| Production / Staging / Development | Production / Staging / Development | Exact environment names |
| Deployment | Deployment | Deployment history and operations; use natural verbs around it |
| Build | البناء / عملية البناء | Building code or a Docker image; not إنشاء for every context |
| Workers / Worker | Workers / Worker | Request-handling processes; never العاملين or العمال |
| CPU / RAM / NVMe | CPU / RAM / NVMe | Technical resource labels |
| Database | قاعدة البيانات | Familiar, natural Arabic; identifiers are untouched |
| Database manager | مدير قواعد البيانات | Odoo database-management interface |
| Backup | نسخة احتياطية / النسخ الاحتياطي | Stored copy / operation |
| Snapshot | لقطة | Full-instance point-in-time copy; distinguish from per-database backup |
| Restore | استعادة | Restore data from backup or snapshot |
| Duplicate | إنشاء نسخة مطابقة | Database copy operation |
| Storage | التخزين | Storage capacity or usage |
| Domain | Domain | Technical domain selector; subdomain is النطاق الفرعي |
| Cluster / Pod / Container | Cluster / Pod / Container | Kubernetes and Docker resources |
| Terminal / Shell / Backend / Frontend | Terminal / Shell / Backend / Frontend | Technical tools and components |
| Token / Access token / Secret | Token / Access token / Secret | Technical credentials; verification code is رمز التحقق |
| Authentication / Authorization / API | Authentication / Authorization / API | Technical concepts and API names |
| Logs | السجلات | Application and deployment logs |
| Billing | الفوترة | Billing section and cycle, distinct from الفواتير |
| Invoice | الفاتورة | Invoice document |
| Credit | الرصيد | Wallet, promotional or upgrade credit |
| Credit note | إشعار دائن | Accounting document |
| Save | حفظ | Save a change |
| Save money | وفّر / التوفير | Price savings, never حفظ |
| Upgrade plan | ترقية الخطة | More resources |
| Downgrade | الانتقال إلى خطة أقل | Plan reduction, not an older software release |
| Provisioning | جارٍ التجهيز | Preparing infrastructure |
| Login / Logout | تسجيل الدخول / تسجيل الخروج | Authentication actions |
| Documentation | الوثائق | Guides and reference pages |

## Names and technical invariants

Copy names exactly, including capitalization: VELTNEX, Veltnex, Odoo,
Kubernetes, GitHub, GitLab, Gitea, Bitbucket, AWS, Google Cloud Storage,
DigitalOcean, Hetzner, Docker, PostgreSQL, Python, WhatsApp and LinkedIn.
Do not transliterate these names. Preserve product edition and tier names
such as Community, Enterprise, Standard, HA and Scale.

Keep URLs, email addresses, filenames, paths, command output, code, API names,
configuration keys, module names, versions, numbers and unit symbols intact.
Examples: `/web/login`, `requirements.txt`, `pip install`, `sale`,
`stock_account`, `pandas openpyxl phonenumbers`, `web.base.url`,
`s3:PutBucketCORS`, `GB`, `MB`, `12s`, `38ms`, `[INFO]`.
Never translate customer-entered names or values submitted to APIs.

## Editorial checks

- Read the entire rendered sentence, including interpolated values and inline
  emphasis. Replace English fragments with a complete localizable message
  when Arabic word order or negation requires it.
- Use concise action labels: إنشاء، حفظ، متابعة، إيقاف، استئناف، استعادة.
- Distinguish context: billing decline is رفض; closing a notification is إغلاق.
- Preserve every warning, technical condition, retention period and fee detail.
- Keep interpolation placeholders exactly, including repeated placeholders.
- Preserve native HTML tags and attributes; change only their visible text.
- Use the same translation for equivalent React and native Odoo messages.
- Review the visible page on desktop and mobile with RTL enabled.

The catalogs are maintained locally. Do not regenerate them with a translation
service. Run frontend tests and build after editing; check native translations
against the shared catalog and validate native pages when their content changes.
