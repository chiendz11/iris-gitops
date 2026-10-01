# Kubernetes compatibility preflight

Reviewed 2026-09-29. The infrastructure initial-install profile selects EKS `1.34`.
GitOps CI uses kubectl `v1.34.0` and kubeconform schemas `1.34.0`. KServe is still
`v0.19.0`; Knative Serving/Kourier are still `knative-v1.20.0`.

KServe's versioned [0.19 compatibility matrix](https://kserve.github.io/website/docs/0.19/admin-guide/serverless)
includes Kubernetes 1.34 with Knative 1.20. The rollout observer's existing kubectl 1.33
client is within Kubernetes' [one-minor skew policy](https://kubernetes.io/releases/version-skew-policy/#kubectl).
The regression test ties the CI schema/client and observer skew to this profile. Review the
observer image's availability as part of deployment preflight, not just its version number.

This does not prove every chart/image/add-on works on EKS: no cluster was created or upgraded
by this change. Verify regional EKS/add-on availability, chart rendering, webhooks, storage,
readiness and the full model lifecycle before calling the installation production-ready.
Check the [EKS support calendar](https://docs.aws.amazon.com/eks/latest/userguide/kubernetes-versions.html)
again before deployment: 1.34 standard support ends 2026-12-02. A later version upgrade is a
reviewed infrastructure change plus GitOps compatibility updates, not a floating version pin.
