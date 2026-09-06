# Iris production GitOps

Repo này là **nguồn sự thật duy nhất** cho desired state của production EKS. Các repo ứng dụng chỉ
test/build/push image và phát release intent hoặc mở pull request; chúng không giữ manifest
production, Kubernetes credential hay chạy `kubectl apply` vào production.

## Luồng triển khai

```text
application repo -> test/build -> ECR immutable SHA -> PR vào iris-gitops
                                                       |
                                                       v
                                                review + merge
                                                       |
                                                       v
                                              Argo CD reconcile EKS
```

## Cấu trúc và ownership

- `applications/`: AppProject và các child Application theo mô hình App of Apps; không chứa
  Application quản lý chính Argo CD.
- `automation/model-release-dispatcher/`: source, Dockerfile và test của release-intent adapter do
  DevOps sở hữu; component này tách khỏi training image.
- `platform/external-secrets-config/`: `ClusterSecretStore` dùng chung; không thuộc workload MLflow.
- `platform/argocd-monitoring/`: ServiceMonitor cho controller, server, repo-server và ApplicationSet.
- `environments/production/`: desired state của MLflow, training lifecycle và inference.

Các platform chart bên thứ ba được pin version trong Application; riêng Argo CD chart và root
Application thuộc Terraform ở `iris-infrastructure`. Workload tự viết dùng Kustomize; sync waves
bảo đảm CRD/controller có trước custom resource.

## Boundary với infrastructure

```text
Terraform owns                         Argo CD owns
-------------------------------        --------------------------------
AWS foundation                        AppProject + child Applications
EKS                                   platform controllers
helm_release.argocd                   monitoring configuration
root Application                      MLflow/training/inference workloads
```

Không có `applications/platform-argocd.yaml`, Argo CD không self-manage và repo này không chứa
bootstrap script. Upgrade Argo CD phải là infrastructure PR; upgrade KServe, monitoring hoặc
workload phải là GitOps PR.

## Argo CD production profile

Production values nằm ở `iris-infrastructure/terraform/platform/argocd-values-production.yaml`.
Repo này chỉ giữ ServiceMonitor cho các metrics service mà Terraform-owned chart tạo ra.

Khi chưa cấp một IdP thực, operator dùng `argocd login --core` dựa trên EKS RBAC. Không dựng SSO
giả bằng client ID placeholder. SSO values nằm ở infrastructure; ExternalSecret opt-in nằm trong
`platform/external-secrets-config/examples/`:

1. Tạo GitHub OAuth App/IdP client và secret JSON trong AWS Secrets Manager.
2. Thêm secret ARN vào Terraform variable `additional_external_secret_arns`.
3. Enable `argocd-sso.example.yaml` để merge client ID/secret vào `argocd-secret`.
4. Merge `argocd-values-sso.example.yaml` vào Terraform-owned values, thay URL/domain và
   organization thực.

Repo hiện public nên Argo CD không cần repository credential. Nếu chuyển private, dùng
`argocd-repository-credentials.example.yaml`: token nằm ở Secrets Manager, External
Secrets tạo Kubernetes Secret loại `repo-creds`; không commit token và không đưa token runtime qua
GitHub Actions.

## GitHub governance

`main` phải có branch protection: required check `validate`, một approval, CODEOWNER review, stale
review dismissal, last-push approval, linear history, resolved conversation, không force-push/xóa.
Repo infrastructure chứa script idempotent để áp cấu hình này. Với repo công ty, CODEOWNERS và
người tạo PR phải là các chủ thể độc lập; `@chiendz11` chỉ là owner phù hợp cho capstone cá nhân.

Workflow `.github/workflows/validate.yml` chạy với pull request, merge queue, push vào `main` và khi
operator chủ động yêu cầu kiểm tra lại. Nó có hai nhánh kiểm tra song song:

- `render-schema`: tự tìm mọi Kustomize root, render cả remote base đã pin và dùng `kubeconform`
  kiểm tra các Kubernetes resource có schema chuẩn. CRD bên thứ ba chưa có schema upstream được
  bỏ qua ở bước schema nhưng vẫn phải render thành công.
- `policy`: giữ boundary Terraform/Argo CD, cấm lệnh mutate cluster trong Actions, cấm plain
  Kubernetes Secret, mutable image tag, Helm chart version trôi nổi và placeholder mới.
- `validate`: check tổng hợp ổn định duy nhất dùng cho branch protection.

Workflow validation chỉ có quyền `contents: read`, không có AWS role, kubeconfig hoặc Kubernetes
secret. Workflow `model-release.yml` riêng nhận release intent, dùng GitHub OIDC để assume đúng role
chỉ đọc model-promoter secret, rồi mở protected PR; nó không truy cập EKS. Sau reviewer merge,
Argo CD đang chạy trong EKS tự phát hiện commit `main` và reconcile. `workflow_dispatch` của
validation chỉ chạy lại kiểm tra, không deploy hoặc sync Argo CD bằng tay.

Nếu bật GitHub merge queue, trigger `merge_group` đã có sẵn; vẫn giữ `validate` làm required
check. Dependabot gom cập nhật GitHub Actions hằng tuần; action dùng trong workflow được pin bằng
commit SHA để tránh dependency tag bị thay đổi ngoài ý muốn.

## Day-2 change lifecycle

Mọi thay đổi platform, cấu hình hoặc image production đều theo cùng một flow:

```text
feature/application pipeline
          |
          v
PR thay desired state trong iris-gitops
          |
          v
render-schema + policy -> validate -> CODEOWNER review
          |
          v
merge main -> Argo CD auto-sync/self-heal -> EKS
```

Repo ứng dụng build/scan một lần, push image immutable rồi mở PR chỉ thay image reference tương ứng.
Không commit `latest`, không dùng GitHub Actions của repo này để `kubectl apply`, và không vận hành
song song Argo Image Updater nếu đã chọn source-repo-driven PR để tránh hai controller cùng sửa một
field.

Model rollout cũng theo boundary này. Argo chỉ gửi contract
`{action, model_version, change_id}` bằng dispatcher image riêng; script hiểu cấu trúc manifest và
workflow tạo branch/PR đều nằm trong repo này. Dispatcher CI push image vào ECR bằng role riêng,
lấy digest rồi mở protected PR pin digest đó vào WorkflowTemplate. Branch rules giữ CI cùng
CODEOWNER review. Sau reviewer merge, Argo CD reconcile còn lifecycle chỉ dùng RBAC read-only để
đợi KServe Ready trước khi chạy gate kế tiếp.

Ownership của DAG được tách theo component:

| Task | Image owner | Credential |
|---|---|---|
| Fetch/train, smoke/evaluate | Dev/ML: `iris-data-pipeline` | Không có GitHub App key |
| Dispatch release | DevOps: `automation/model-release-dispatcher` | Model-promoter key, chỉ task này |
| Wait rollout | DevOps: kubectl read-only | Kubernetes `get/list/watch` |

## Metadata GitHub liên repo

- `iris-model-registry` và `iris-inference-service` nhận `GITOPS_REPOSITORY`,
  `GITOPS_APP_CLIENT_ID` cùng Environment secret `GITOPS_APP_PRIVATE_KEY` để mở image-update PR.
- `iris-data-pipeline` không nhận đường dẫn manifest hoặc GitOps PR credential. Training container
  chỉ nhận runtime App từ External Secrets và phát contract `model_release` vào repo này.
- Repo này nhận `AWS_REGION`, `MODEL_PROMOTION_AWS_ROLE_ARN` và
  `MODEL_PROMOTION_SECRET_ARN` từ `terraform/github-config`; workflow dùng chúng để đọc đúng secret
  bằng OIDC và mở model-rollout PR.
- `DISPATCHER_ECR_REPOSITORY` và `DISPATCHER_PUBLISH_AWS_ROLE_ARN` cũng do
  `terraform/github-config` quản lý; publisher role chỉ ghi được dispatcher repository.

Không lưu PAT, AWS access key tĩnh hoặc Kubernetes credential trong GitHub Secrets.
