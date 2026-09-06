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

# Existing bootstrap placeholders are transitional. No PR may add or reintroduce one;
# once infrastructure/application automation removes them, this rule keeps them out.
if [[ -n "${base_sha}" && "${base_sha}" != 0000000000000000000000000000000000000000 ]] && \
   git cat-file -e "${base_sha}^{commit}" 2>/dev/null && \
   git cat-file -e "${head_sha}^{commit}" 2>/dev/null; then
  added_placeholders="$({
    git diff --unified=0 "${base_sha}" "${head_sha}" -- \
      applications platform environments/production || true
  } | awk '/^\+[^+]/ && /REPLACE_[A-Z0-9_]+/ { print }')"
  if [[ -n "${added_placeholders}" ]]; then
    printf '%s\n' "${added_placeholders}"
    error "This change introduces a REPLACE_* placeholder into production desired state."
  fi
fi

placeholder_matches="$(rg -n 'REPLACE_[A-Z0-9_]+' applications platform environments/production || true)"
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
