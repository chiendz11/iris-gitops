#!/usr/bin/env bash
set -euo pipefail

: "${AWS_REGION:?Set AWS_REGION}"
: "${EKS_CLUSTER_NAME:?Set EKS_CLUSTER_NAME}"

ARGOCD_CHART_VERSION="${ARGOCD_CHART_VERSION:-10.4.0}"

aws eks update-kubeconfig --region "${AWS_REGION}" --name "${EKS_CLUSTER_NAME}"
helm repo add argo https://argoproj.github.io/argo-helm
helm repo update
helm upgrade --install argocd argo/argo-cd \
  --version "${ARGOCD_CHART_VERSION}" \
  --namespace argocd --create-namespace \
  --set configs.params.server\.insecure=false \
  --wait --timeout 10m

kubectl apply -f bootstrap/root-application.yaml
kubectl rollout status statefulset/argocd-application-controller -n argocd --timeout=10m

echo "Argo CD bootstrapped. From now on, merge desired-state changes into iris-gitops."
