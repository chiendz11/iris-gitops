## Change

- Change owner:
- Desired-state paths changed:
- Workload/platform affected:

## Reason and expected effect

<!-- Explain why production desired state must change and what Argo CD should reconcile. -->

## Evidence

- [ ] Required check `validate` passes.
- [ ] Image references are immutable commit SHAs or digests, where applicable.
- [ ] No credential, secret value or plain Kubernetes `Secret` is committed.
- [ ] I reviewed the rendered manifests and Argo CD diff.

## Rollout and rollback

- Expected health signal:
- Rollback commit/image:
- Any ordering, migration or maintenance-window requirement:

## Risk

- [ ] Workload-only change
- [ ] Platform/controller/CRD change
- [ ] Destructive or stateful change
