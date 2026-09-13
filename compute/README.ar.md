# منصة Odoo SaaS — مستوى التحكم في Kubernetes

منصة توفير (provisioning) تصريحية (declarative) وأصيلة في Kubernetes لتشغيل
نسخ Odoo متعددة المستأجرين (multi-tenant SaaS). واجهة برمجة تطبيقات
(API) الخاصة بمستوى تحكم الـ SaaS تقوم بإنشاء وتحديث وحذف مورد Kubernetes
مخصص واحد فقط — `OdooInstance` — وتقوم وحدة تحكم (controller) بتحويله
(reconcile) إلى مستأجر معزول بالكامل: مساحة اسم (namespace)، قاعدة بيانات
PostgreSQL، مخزن ملفات (filestore)، عبء عمل Odoo (workload)، التوجيه
(routing)، شهادات TLS، النسخ الاحتياطي، وسياسات الأمان. الـ API لا تقوم
أبدًا بإنشاء Deployment أو Service أو PVC أو Secret أو Job بشكل مباشر.

راجع **[docs/architecture.ar.md](docs/architecture.ar.md)** للاطلاع على
الشرح الكامل للتصميم والمخططات وكل مفاضلة (trade-off) وراء هذه القرارات —
كما يوثّق عدة أخطاء حقيقية تم اكتشافها وإصلاحها أثناء اختبار هذا المشغّل
(operator) على عنقود (cluster) حي أثناء بنائه (خطأ في مخطط CRD، تفاعل دقيق
بين قيم Kubernetes الافتراضية وخاصية `omitempty` في Go، تسجيل مخطط
(scheme) مفقود، خطأ أمان في الـ pod خاص بصورة Odoo الرسمية، خطوة تهيئة
قاعدة بيانات مفقودة، وحالة جمود (deadlock) في التخزين من نوع
`WaitForFirstConsumer`) — بدلاً من تصميم من الصفر تم تجميعه (compile) فقط
دون اختباره فعليًا.

## هيكل المستودع (Repository layout)

```text
.
├── operator/                  وحدة Go: CRD الخاص بـ OdooInstance + وحدة التحكم
│   ├── api/v1alpha1/          أنواع Go الخاصة بـ CRD، القيم الافتراضية، deepcopy
│   ├── cmd/                   نقطة دخول المدير (cmd/main.go)
│   ├── internal/resources/    دوال بناء الحالة المرغوبة (desired state)، قابلة للاختبار دون عنقود
│   ├── internal/controller/   حلقة التوفيق (reconciliation loop) (مُختبرة بوحدات + envtest)
│   ├── config/                CRD/RBAC المُولَّدة + مسار تثبيت مبني على kustomize
│   ├── Dockerfile             صورة المشغّل بدون امتيازات جذر (non-root)، distroless
│   └── Makefile                بناء / اختبار / تدقيق / docker-build / تثبيت / نشر / ...
├── charts/
│   ├── odoo-operator/         تثبيت المشغّل نفسه (CRD + RBAC + Deployment)
│   └── odoo-instance/         غلاف GitOps اختياري لإنشاء OdooInstance واحد
├── examples/                  نماذج بيانات OdooInstance (أساسي، إنتاج مع TLS،
│                              قاعدة بيانات خارجية، تكرارات متعددة)
└── docs/architecture.ar.md    الشرح الكامل للتصميم، المخططات، ومبررات القرارات
```

## البدء السريع (Quick start)

المتطلبات المسبقة: عنقود Kubernetes، وHelm 3، و(اختياريًا) موارد Gateway
API المخصصة (CRDs) مع تطبيق لها مثل Traefik أو Envoy Gateway — أو يمكنك
ضبط `networking.provider=ingress` إذا كنت تفضل استخدام Ingress controller
تقليدي.

```bash
# 1. ابنِ وادفع صورة المشغّل (أو استخدم صورة منشورة مسبقًا).
cd operator
make docker-build IMG=registry.example.com/odoo-saas/operator:v0.1.0
make docker-push  IMG=registry.example.com/odoo-saas/operator:v0.1.0

# 2. ثبّت المشغّل (CRD + RBAC + Deployment) عبر Helm.
helm upgrade --install odoo-operator ../charts/odoo-operator \
  --namespace odoo-system --create-namespace \
  --set image.repository=registry.example.com/odoo-saas/operator \
  --set image.tag=v0.1.0

# 3. وفّر (provision) مستأجرًا جديدًا.
kubectl apply -f ../examples/odoo-instance-acme.yaml
kubectl get odooinstance customer-acme -w
```

```text
NAME             PHASE    VERSION   URL                                 READY
customer-acme    Ready    18.0      https://customer-acme.example.com   True
```

```bash
kubectl describe odooinstance customer-acme
```

## دورة التوفير / التحديث / الحذف

```bash
kubectl apply -f examples/odoo-instance-acme.yaml       # إنشاء
kubectl get odooinstance customer-acme -o yaml           # status.phase, .conditions, .url
kubectl patch odooinstance customer-acme --type=merge \
  -p '{"spec":{"workers":{"count":4}}}'                  # تحديث: يُطلق إعادة تشغيل تدريجية (rolling restart)
kubectl delete odooinstance customer-acme                # حذف: يقوم finalizer بتنفيذ نسخة احتياطية
                                                           # أخيرة (إن كانت مفعّلة)، ثم يفكك
                                                           # مساحة اسم المستأجر
```

## ترقية المشغّل (operator)

```bash
helm upgrade odoo-operator charts/odoo-operator \
  --namespace odoo-system \
  --set image.tag=v0.2.0
```

يمكن تطبيق تغييرات مخطط CRD (حقول اختيارية جديدة) بنفس الطريقة؛ مجلد
`crds/` الخاص بـ Helm لا يُرقّى تلقائيًا بواسطة `helm upgrade` على
التثبيتات الحالية (هذا قيد في Helm نفسه، وليس خاصًا بهذا الـ chart) — نفّذ
`kubectl apply -f charts/odoo-operator/crds/` جنبًا إلى جنب مع ترقية
الـ chart إذا غيّر إصدار ما مخطط CRD.

## إلغاء التثبيت

```bash
helm uninstall odoo-operator --namespace odoo-system
# لا تُحذف موارد CRD وأي كائنات OdooInstance متبقية (وكل ما وفّره
# المشغّل لأجلها) عند إلغاء تثبيت الـ chart. احذف المستأجرين أولاً إذا
# أردت زوال بياناتهم:
kubectl get odooinstance -A
kubectl delete odooinstance --all
# فقط بعد ذلك، إذا أردت إزالة الـ CRD نفسه أيضًا:
kubectl delete -f charts/odoo-operator/crds/
```

## التطوير المحلي

```bash
cd operator
make manifests generate fmt vet   # إعادة توليد CRD/RBAC/deepcopy بعد تعديل api/v1alpha1
make test                         # اختبارات وحدات + اختبارات وحدة التحكم القائمة على envtest
make run                          # تشغيل المدير محليًا مقابل kubeconfig الحالي لديك
```

يقوم `make test` بتنزيل ثنائيات (binaries) حقيقية لـ
`kube-apiserver`/`etcd` عبر `setup-envtest` ويشغّل وحدة التحكم مقابلها —
وليس محاكاة (mock). راجع `docs/architecture.ar.md` لمعرفة ما يمكن لـ
envtest اختباره وما لا يمكنه (لا يوجد kubelet/scheduler، لذا يجب محاكاة
حالة الـ Pod/Deployment في تلك الاختبارات) مقابل ما تم التحقق منه إضافيًا
على عنقود حي كامل.

## فلسفة الاختبار

- `internal/resources` — اختبارات وحدات Go عادية، لا تحتاج إلى عنقود
  (`go test ./internal/resources/...`).
- `internal/controller` — مدعومة بـ `envtest`: `kube-apiserver` +
  `etcd` حقيقيان، يقودان دالة `Reconcile` الفعلية.
- بالإضافة إلى ما سبق، تم تشغيل هذا المشغّل على عنقود حقيقي (إنشاء ←
  توفير ← تقديم الحركة ← حذف) كجزء من بنائه، وهذا ما أدى إلى اكتشاف
  الأخطاء المذكورة في الفقرة الافتتاحية لـ `docs/architecture.ar.md`.
