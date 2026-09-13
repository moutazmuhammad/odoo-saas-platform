# End-to-end tests (reserved)

Empty on purpose, for now. This directory is reserved for live-cluster
end-to-end tests of the operator — the kind that must run against a real
Kubernetes API server (not `envtest`'s apiserver-without-a-kubelet), the
way this project's own testing convention already requires before trusting
a claim of "this works": see
[`../../../../docs/PRODUCTION-READINESS-PLAN.md`](../../../../docs/PRODUCTION-READINESS-PLAN.md)'s
Production-First Principle, and
[`../../../../control-plane/docs/architecture/MICROSERVICES-PLAN.md`](../../../../control-plane/docs/architecture/MICROSERVICES-PLAN.md)
Phase 1's acceptance criteria ("demonstrated on a real cluster, not just
unit tests").

Nothing lives here yet because Phase 1 of that plan hasn't started — the
manual `kubectl apply` verifications done so far (both for the operator
itself and for the restore feature) were real live-cluster runs, just not
yet captured as repeatable automated tests. When Phase 1 begins, that's
what belongs here.
