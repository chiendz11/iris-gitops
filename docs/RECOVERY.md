# Production recovery: what is automated and what is not

Only `production` exists. This runbook describes code to deploy later; adding these
files or pushing a feature branch does not execute recovery in AWS.

| Incident | Automation | Operator gate |
| --- | --- | --- |
| Complete candidate metrics reject the SLO | Argo dispatches rollback intent, GitOps opens baseline PR | Review/merge; Argo waits for exact baseline and runs smoke |
| Smoke fails, metrics unavailable/insufficient, rollout times out | No promote; exit report, controller failure metric and Prometheus alert | Inspect live state; retry observation or select recovery explicitly |
| Model is already stable at 100% but bad | `recovery.yml` restores numeric model from an operator-selected historical GitOps commit | Preflight, dispatch, merge, Ready/smoke, champion reconciliation |
| Image/config is bad | `recovery.yml` restores historical digest + approved non-secret config in one PR | Preflight, dispatch, verify signature, merge, smoke/monitoring |

No workflow picks the “best” version, automatically merges recovery PRs, changes
Kubernetes resources from GitHub Actions, or restores Terraform state.

## Normal model lifecycle

Training runs `iris_pipeline.training.train` from the training image. It writes
`model-result.json` and scalar Argo outputs into the shared PVC. Its captured
baseline is immutable for that lifecycle. GitOps-owned smoke, observation,
dispatch and alias steps run the separate `release-automation-image` digest.

The gate filters Prometheus by candidate model version: RPS > 0, at least 20
histogram observations estimated using `increase(...[5m])`, prediction p95 <= 0.5s
and prediction error rate <= 1%. These are application metrics, not end-to-end
DNS/TLS/ingress SLOs or online accuracy. The sample threshold is a capstone
guardrail, not statistical proof or a guaranteed five-minute soak.

The evaluator emits `pass`, `reject`, or `inconclusive`. Missing/non-finite
latency, insufficient samples and query errors cannot approve or request an
automatic rollback. It retries observation for up to 120 seconds; inconclusive
then exits nonzero. Only a successful evaluator with `decision=reject` can
dispatch the baseline rollback. A smoke failure stops evaluation and promotion.

Both first bootstrap and promotion to 100% wait for the exact model URI, traffic
configuration, observed generation and Ready condition, then smoke, before the
MLflow champion alias moves. Alias updates check the expected previous champion.
Rollback removes canary/candidate metadata and waits for the numeric baseline;
it does not leave the candidate manifest at 0%.

The lifecycle exit handler emits structured diagnostics; it never mutates traffic.
Controller metrics/ServiceMonitor and `lifecycle-alerts.yaml` provide alert rules.
Alerts appear in Prometheus/Alertmanager once installed; configure a receiver
separately for Slack/email/paging. No delivery channel is assumed. If Prometheus
or the controller is down, independently inspect Argo/Kubernetes; neither an
exit handler nor a metric is a durable external watchdog. A failed workflow can
leave canary traffic active until a recovery PR is merged.

## Preconditions for operator recovery

1. Pause dataset submissions/producers and normal release automation. Stop active
   Iris lifecycle runs using your authorized Argo operator access, and close
   obsolete release PRs. Do not just suspend a run that could later resume and
   promote. This is an explicit operator precondition, not something a GitHub
   checkbox or Git concurrency lock can prove against the cluster.
2. Compare GitOps main with the actual serving model/image/config, traffic,
   Argo state, and MLflow champion. A Git commit proves desired state, not health.
3. Select a historical **main commit SHA** whose relevant model or image/config
   you have evidence was healthy. Confirm numeric model/artifacts still exist,
   and confirm compatibility with the current inference image/config. For an
   MLflow image downgrade, check DB migration compatibility/backups; this workflow
   does not undo a database migration. Never restore compromised secret values.
4. Record preflight evidence without credentials or private data. `preflight_confirmed`
   attests these manual checks; the PR factory cannot access private MLflow or EKS.

## Dispatch a recovery proposal

After this workflow is merged into main, repository owner `chiendz11` can run:

```bash
gh workflow run recovery.yml --repo chiendz11/iris-gitops --ref main \
  -f component=model \
  -f target_git_sha=<FULL_KNOWN_GOOD_GITOPS_SHA> \
  -f expected_main_sha=<FULL_CURRENT_MAIN_SHA> \
  -f schema_version=v1 \
  -f reason='Incident: regression after promotion' \
  -f evidence='Incident record: target artifact checked; compatibility and prior health verified' \
  -F preflight_confirmed=true
```

For workload recovery use `component=inference` or `component=model-registry`
and the config schema version used by that historical release. Recovering only
model fields preserves the current image/config. Workload recovery preserves
the current model, platform endpoints/IAM and unrelated manifests. It restores
image digest and the approved config keys together, removing keys introduced by
newer schemas. Secret-reference changes require a targeted reviewed PR instead.

Guards: main-only dispatch; owner and rerun-actor authorization; complete SHA and
ancestor checks; expected main SHA; numeric/stable target model; current ECR
allow-list; immutable schema and config validation; Cosign verification for
workload images; no overlapping release/recovery PR. The App publisher receivers
retain their separate authorization. Existing main-bound OIDC and GitOps App
secret-reader/ECR-reader role are reused; no extra AWS key or GitHub secret.

The workflow opens a PR and writes `state/recovery-lock.json`. **It does not merge.**
PR CI rejects a stale expected base: if main advances, close and re-dispatch with
a fresh inspected SHA rather than blindly updating/merging an old proposal.
Keep strict up-to-date required checks enabled. The lock blocks normal model and
workload receiver renders after recovery merges. Open recovery PRs block those
receivers before merge. This is a Git proposal fence, not a live Kubernetes lock.

## After the recovery PR merges

1. Let Argo CD reconcile; check the restored version/image is actually Ready.
2. Run external smoke and review monitoring. A merged PR is not proof of recovery.
3. For **model recovery**, reconcile champion only after restored serving passes.
   Use the reviewed workflow template's operator-only entrypoint:

   ```bash
   argo submit -n argo --from workflowtemplate/iris-model-lifecycle \
     --entrypoint recovery-finalize \
     -p recovery-version=<RESTORED_NUMERIC_VERSION> \
     -p expected-champion-version=<ACTUAL_CHAMPION_VERSION_OR_none> \
     --watch
   ```

   It shares the lifecycle mutex, reads KServe until the exact stable model is
   Ready, runs smoke, then conditionally updates the MLflow alias. It does not
   retrain or mutate Kubernetes manifests. Keep new lifecycle runs stopped until
   complete. If alias reconciliation alone fails, inspect/retry that step; do
   not automatically undo healthy serving. These systems are not one transaction.
4. Open a separate PR removing `state/recovery-lock.json`, documenting verification
   and any fixes needed in the app source to avoid reintroducing the failed release.
   Merge only after reconciliation is complete, then resume producers.

For config-only MLflow changes, hashed ConfigMap names update the Deployment's
reference so pods reload environment values; image/config recovery does not depend
on a manual pod restart. Kubernetes/Argo CD remain the deployment owners.

## Limits and deployment order

Tests use local files and mocked services. EKS/MLflow/ECR/Alertmanager behavior must
still be validated after deployment. Live artifact compatibility is operator
preflight; image existence/signature is additionally checked by CI. Artifact
retention, external alert routing, automatic post-merge recovery verification,
and general database/secret/add-on recovery are not implemented here.

This GitOps refactor expects the matching data-pipeline image exporting
`iris_pipeline.*` and `model-result-v1`, plus infrastructure OIDC/App configuration.
Do not merge/deploy this PR alone against the legacy training image. Publish and
merge the immutable release-automation digest before allowing dataset events.
Pushing this branch updates the open PR and runs validation only; main is watched
by Argo CD, and image publishing is main/explicit-dispatch only.
