# End-to-end tests (reserved)

Empty on purpose, for now. This directory is reserved for live-cluster
end-to-end tests of the operator — the kind that must run against a real
Kubernetes API server (not `envtest`'s apiserver-without-a-kubelet), the
way this project's own testing convention already requires before trusting
a claim of "this works": see
[`../../../../ROADMAP.md`](../../../../ROADMAP.md)'s verification-tag
convention (§"How to read this document") and Phase 2's acceptance
criteria (real tenant traffic on a real cluster, not just unit tests).

Manual `kubectl apply` verifications have been done repeatedly against a
real microk8s cluster (operator lifecycle, backup/restore round trips) —
real live-cluster runs, just not yet captured as repeatable automated
tests. As Phase 2 (Kubernetes cutover) proceeds, that's what belongs here.
