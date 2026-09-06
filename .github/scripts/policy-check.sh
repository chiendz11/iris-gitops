#!/usr/bin/env bash
set -euo pipefail

base_sha="${1:-}"
head_sha="${2:-HEAD}"
failed=0

error() {
  echo "::error::$*"
  failed=1
}

warning() {
  echo "::warning::$*"
}

if [[ -e applications/platform-argocd.yaml || -d platform/argocd || -d bootstrap ]]; then
  error "Argo CD and its root Application are Terraform-owned; do not bootstrap or self-manage Argo CD here."
fi

if rg -n \
  '^[[:space:]]*chart:[[:space:]]*argo-cd[[:space:]]*$|^[[:space:]]*name:[[:space:]]*platform-argocd[[:space:]]*$' \
  applications; then
  error "An Argo CD self-management declaration was found under applications/."
fi

if rg -n --glob '*.yml' --glob '*.yaml' \
  '(kubectl[[:space:]]+(apply|delete|patch|replace|scale|set|rollout)|helm[[:space:]]+(install|upgrade|uninstall)|argocd[[:space:]]+app[[:space:]]+sync)' \
  .github/workflows; then
  error "GitHub Actions must validate desired state only; Argo CD is the sole production deployer."
fi

if rg -n --glob '*.yml' --glob '*.yaml' \
  'kubectl[[:space:]]+(apply|delete|patch|replace|scale|set|rollout)' \
  environments/production; then
  error "Production workflows must propose GitOps changes; direct kubectl mutations are forbidden."
fi

if rg -n '^[[:space:]]*kind:[[:space:]]*Secret[[:space:]]*$' \
  applications platform environments/production; then
  error "Plain Kubernetes Secret manifests are forbidden; use ExternalSecret references instead."
fi

if rg -n \
  '^[[:space:]]*(image|newTag):[[:space:]].*:(latest|production)([[:space:]]|$)' \
  applications platform environments/production; then
  error "A mutable workload image tag was found. Promote an immutable commit SHA or image digest."
fi

dispatcher_image="$(sed -n 's/^[[:space:]]*- {name: dispatcher-image, value: \([^}]*\)}$/\1/p' \
  environments/production/data-pipeline/workflow-template.yaml)"
if [[ "${dispatcher_image}" == DISPATCHER_IMAGE_NOT_PUBLISHED ]]; then
  if [[ -n "${base_sha}" ]] && git cat-file -e "${base_sha}^{commit}" 2>/dev/null; then
    base_dispatcher_image="$(git show "${base_sha}:environments/production/data-pipeline/workflow-template.yaml" 2>/dev/null | \
      sed -n 's/^[[:space:]]*- {name: dispatcher-image, value: \([^}]*\)}$/\1/p' || true)"
    if [[ -n "${base_dispatcher_image}" && "${base_dispatcher_image}" != DISPATCHER_IMAGE_NOT_PUBLISHED ]]; then
      error "A published dispatcher image must not be reverted to the bootstrap sentinel."
    fi
  fi
  warning "The dispatcher bootstrap sentinel remains; merge the digest PR before dataset events."
elif [[ ! "${dispatcher_image}" =~ ^[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/[A-Za-z0-9._/-]+@sha256:[0-9a-f]{64}$ ]]; then
  error "The model-release dispatcher must be pinned to an ECR sha256 digest."
fi

while IFS= read -r application; do
  grep -Eq '^[[:space:]]*automated:' "${application}" || \
    error "${application}: automated sync is required."
  grep -Eq 'prune:[[:space:]]*true' "${application}" || \
    error "${application}: automated prune is required."
  grep -Eq 'selfHeal:[[:space:]]*true' "${application}" || \
    error "${application}: automated self-heal is required."

  if grep -Eq '^[[:space:]]*chart:' "${application}"; then
    revision="$(sed -n 's/^[[:space:]]*targetRevision:[[:space:]]*//p' "${application}" | head -n 1 | tr -d '\"' | xargs)"
    case "${revision,,}" in
      '' | head | main | master | latest | '*')
        error "${application}: Helm chart targetRevision must be an explicit version."
        ;;
    esac
  fi
done < <(find applications -maxdepth 1 -type f -name '*.yaml' -exec grep -l '^kind: Application$' {} + | sort)

# Existing bootstrap placeholders are transitional. The first platform-adoption PR
# may introduce only the explicitly listed infrastructure outputs. Afterwards no PR
# may add or reintroduce one; automation must resolve them through a protected PR.
if [[ -n "${base_sha}" && "${base_sha}" != 0000000000000000000000000000000000000000 ]] && \
   git cat-file -e "${base_sha}^{commit}" 2>/dev/null && \
   git cat-file -e "${head_sha}^{commit}" 2>/dev/null; then
  initial_platform_adoption=false
  if ! git cat-file -e \
    "${base_sha}:applications/platform-aws-load-balancer-controller.yaml" \
    2>/dev/null; then
    initial_platform_adoption=true
  fi

  current_file=""
  while IFS= read -r diff_line; do
    case "${diff_line}" in
      '+++ b/'*)
        current_file="${diff_line#+++ b/}"
        ;;
      +*)
        [[ "${diff_line}" == '+++'* ]] && continue
        [[ "${diff_line}" =~ (REPLACE_[A-Z0-9_]+) ]] || continue
        placeholder="${BASH_REMATCH[1]}"

        # Example manifests are documentation and are not part of any Kustomize root.
        if [[ "${current_file}" == platform/external-secrets-config/examples/* ]]; then
          continue
        fi

        allowed_initial_placeholder=false
        if [[ "${initial_platform_adoption}" == true ]]; then
          case "${current_file}:${placeholder}" in
            applications/platform-aws-load-balancer-controller.yaml:REPLACE_EKS_CLUSTER_NAME | \
            applications/platform-aws-load-balancer-controller.yaml:REPLACE_VPC_ID | \
            applications/platform-aws-load-balancer-controller.yaml:REPLACE_AWS_LOAD_BALANCER_CONTROLLER_ROLE_ARN | \
            applications/platform-external-dns.yaml:REPLACE_PUBLIC_DOMAIN_NAME | \
            applications/platform-external-dns.yaml:REPLACE_ROUTE53_ZONE_ID | \
            applications/platform-external-dns.yaml:REPLACE_EXTERNAL_DNS_IRSA_ROLE_ARN | \
            environments/production/inference-service/domain-mapping.yaml:REPLACE_KSERVE_HOSTNAME | \
            platform/knative/kustomization.yaml:REPLACE_PUBLIC_ACM_CERTIFICATE_ARN | \
            platform/knative/kustomization.yaml:REPLACE_KSERVE_HOSTNAME)
              allowed_initial_placeholder=true
              ;;
          esac
        fi

        if [[ "${allowed_initial_placeholder}" == true ]]; then
          warning "${current_file}: allowing ${placeholder} during the one-time platform adoption."
        else
          printf '%s:%s\n' "${current_file}" "${diff_line}"
          error "This change introduces ${placeholder} into production desired state."
        fi
        ;;
    esac
  done < <(
    git diff --unified=0 "${base_sha}" "${head_sha}" -- \
      applications platform environments/production || true
  )
fi

placeholder_matches="$(rg -n \
  --glob '!platform/external-secrets-config/examples/**' \
  'REPLACE_[A-Z0-9_]+' applications platform environments/production || true)"
if [[ -n "${placeholder_matches}" ]]; then
  placeholder_count="$(printf '%s\n' "${placeholder_matches}" | wc -l | xargs)"
  warning "${placeholder_count} transitional REPLACE_* line(s) remain; infrastructure or application promotion PRs must resolve them before first production sync."
fi

if rg -n '^[[:space:]]*version:[[:space:]]*latest[[:space:]]*$' \
  applications platform environments/production; then
  warning "A controller-managed component still uses version: latest; pin it after validating the supported upstream version."
fi

if (( failed != 0 )); then
  exit 1
fi
