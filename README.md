# Iris production GitOps (repo 4/5)

Profile hiện tại là solo: không required CODEOWNER/PR approval, nhưng PR và CI vẫn bắt buộc.
Infrastructure/app publish jobs dùng Environment `prod` với owner self-approval. Các GitOps receiver
chỉ tạo PR, không thêm Environment gate: bạn xem diff rồi merge để Argo CD triển khai.
Receiver chỉ mở PR; operator xem diff rồi merge, không có auto-merge. Xem runbook liên repo tại
`../iris-infrastructure/docs/SOLO_OPERATION.md` và `../iris-infrastructure/docs/ROLLBACK_RUNBOOK.md`.
Rollback canary SLO thất bại có nhánh mở PR; smoke lỗi/timeout/thiếu metric chặn promote
và phát diagnostics/alert, không tự suy đoán model lỗi. `recovery.yml` cho owner chọn
historical GitOps commit để mở PR phục hồi model hoặc image/config. Đọc
[docs/RECOVERY.md](docs/RECOVERY.md) về preflight, recovery lock, xác minh serving và champion.

Repo này là source of truth cho **desired state Kubernetes của duy nhất môi trường
`production`**. Application và infrastructure repo chỉ phát contract trung lập; chúng không biết
đường dẫn manifest, không mở GitOps PR và không có kubeconfig production. GitOps receiver validate
contract, render đúng layout của repo này rồi mở pull request được bảo vệ. Chỉ sau khi PR được review
và merge, Argo CD mới reconcile EKS.

## Ownership

```text
iris-gitops
├── applications/                    AppProject và App-of-Apps children
├── platform/                        namespaces, Knative, KServe, monitoring, External Secrets
├── environments/production/         MLflow, training DAG và InferenceService
├── contracts/
│   ├── workload-release-v1.schema.json
│   ├── model-result-v1.schema.json
│   ├── model-release-v1.schema.json
│   ├── platform-contract-v1.schema.json
│   └── workload-config/              schema runtime được GitOps phê duyệt
├── automation/
│   ├── workload_release/             image/config/secret-ref renderer
│   ├── model_release/                dispatch, render, smoke, SLO, promote
│   ├── recovery/                     operator-selected recovery PR + stale-base guard
│   └── platform_reconcile/           Terraform-output contract renderer
├── state/                            projection ổn định do platform renderer tạo
└── .github/workflows/
    ├── validate.yml
    ├── workload-release.yml
    ├── model-release.yml
    ├── recovery.yml
    ├── platform-reconcile.yml
    └── release-automation-image.yml
```

Không có `staging` hoặc cấu trúc base/overlay giả. Khi có nhu cầu staging thật mới thêm environment,
policy, credential boundary và promotion rule tương ứng.

Training dùng PVC riêng cho mỗi Workflow với StorageClass `iris-training-gp3` do Application
`platform-storage` quản lý. Driver EBS CSI/IAM thuộc Terraform infrastructure; PV/EBS được cấp
động, không có manifest PV tĩnh. Xem [TRAINING_STORAGE.md](docs/TRAINING_STORAGE.md) về AZ,
encryption, cleanup workspace và kiểm tra PVC Pending.

## Ba lifecycle độc lập

### 1. Workload release

`workload-release.yml` nhận một `workload-release-v1` intent. Một workflow xử lý cả ba trường hợp:

| Thay đổi | Nội dung intent | GitOps PR |
|---|---|---|
| Chỉ image | image digest + schema compatibility metadata | chỉ đổi image |
| Chỉ config | `runtime_config` + schema version/digest | chỉ đổi ConfigMap/env |
| Image cần config mới | cả `image` và `runtime_config` | một PR atomic chứa cả hai |

Inference và MLflow repo giữ JSON Schema cùng bộ giá trị production đã review. GitOps giữ một bản
schema được phê duyệt. Receiver chỉ chấp nhận config khi version tồn tại, SHA-256 của hai bản schema
trùng nhau, `required_config` khớp chính xác, đủ key, đúng kiểu/range và không có key mang tên giống
secret. Với image-only, cùng metadata đó được dùng để validate config hiện có trong desired state;
do đó image mới cần thêm biến phải phát hành atomic cùng config thay vì lách qua image-only.

Image luôn được pin dạng `repository@sha256:...`. `state/production-platform.json`, do
`platform-reconcile` tạo, giữ allow-list ECR URL từ Terraform. Producer không thể dùng workload
intent để đổi sang repository ngoài platform đã cấp. Trước khi render, receiver còn chạy
`cosign verify` với đúng GitHub OIDC certificate identity của workflow `main` sở hữu component;
digest không ký hoặc ký từ workflow/repo khác bị từ chối.

Schema runtime đã phát hành là bất biến. Khi app cần `v2`, Dev thêm schema mới và đổi
`release/config-schema-version.txt`; DevOps review/copy schema đó thành
`contracts/workload-config/<component>-v2.schema.json` trước khi release intent v2 được chấp nhận.
Việc này là contract-review gate, không buộc app biết đường dẫn manifest production.

Secret value không xuất hiện trong intent. Producer chỉ có thể gửi reference:

```json
{
  "secret_refs": {
    "UPSTREAM_API_TOKEN": {
      "key": "iris/inference/upstream",
      "property": "token"
    }
  }
}
```

Renderer tạo `ExternalSecret`; External Secrets đọc AWS Secrets Manager và tạo Kubernetes Secret.
IAM của External Secrets vẫn là gate cuối: ARN mới phải được thêm trước vào Terraform
`additional_external_secret_arns`. Gửi `{ "secret_refs": {} }` xóa binding runtime đã quản lý.

Inference workload release và model release dùng chung một concurrency group. Receiver còn chặn
image/config PR nếu model PR đang mở hoặc desired state đang có canary annotation; chiều ngược lại,
model rollout bị chặn khi inference workload PR đang mở. Lock này giữ image/config ổn định trong
toàn bộ khoảng đo canary, không chỉ trong thời gian một GitHub Actions run.

Nếu image mới không tương thích khi pod cũ và mới cùng tồn tại, dùng ba PR tuần tự: thêm config
backward-compatible, deploy image mới, rồi xóa config cũ. Đây là ba phase deployment, không phải ba
workflow khác nhau.

### 2. Model release

Training image chỉ tạo `model-result-v1`. Release-automation image do repo này build chứa smoke,
candidate evaluator, MLflow promoter và dispatcher. DAG production hoạt động như sau:

```text
S3 dataset event
  -> exact training image digest
  -> train + register immutable MLflow candidate
  -> offline quality gate
  -> model-release intent: canary
  -> protected GitOps PR: canaryTrafficPercent=10
  -> Argo CD + KServe Ready
  -> smoke traffic tới /v1/models/iris:predict
  -> Prometheus query chỉ model_version của candidate
  -> pass: protected PR rollout 100% -> KServe Ready -> set MLflow champion
  -> fail: protected PR khôi phục đúng baseline version, bỏ canary
```

Dispatcher nhận **toàn bộ** `model-result.json` qua workflow volume; ba file scalar chỉ phục vụ Argo
`when`. Credential GitHub App chỉ được inject vào dispatch task, không vào train task. `wait-rollout`
chỉ có RBAC `get/list/watch`, kiểm tra model URI, traffic, observed generation và Ready thay vì patch
cluster.

Metric canary có label `service` và `model_version`, nên evaluator không trộn baseline 90% với
candidate 10%. Endpoint nội bộ và public cùng dùng contract chuẩn `/v1/models/iris:predict`.
Sau khi rollout 100% Ready, registry promoter chỉ đổi alias nếu champion hiện tại vẫn đúng baseline
đã capture lúc train; retry sau khi alias đã trỏ candidate là idempotent. Vì vậy một thay đổi alias
ngoài luồng không bị pipeline âm thầm ghi đè.

### 3. Platform reconcile

`iris-infrastructure` apply Terraform rồi build `platform-contract-v1` từ allow-list output không
nhạy cảm. `platform-reconcile.yml` ánh xạ contract vào EKS cluster/VPC ID, IRSA, bucket, queue, RDS
endpoint/secret ARN, ECR allow-list, Route53 hostname và ACM certificate. Infrastructure producer
không biết bất kỳ path nào dưới `applications/`, `platform/` hoặc `environments/production/`.

```text
Terraform output -json
  -> build + validate platform-contract-v1
  -> terraform/github-config publishes trusted receiver variables
  -> dedicated platform publisher dispatches platform-reconcile.yml
  -> GitOps renderer
  -> protected PR
  -> Argo CD reconcile
```

Run-specific provenance không được ghi vào `state/production-platform.json`, vì một Terraform apply
không đổi hạ tầng không nên tạo PR nhiễu. File state chỉ giữ constraint ổn định như cluster và ECR
repository.
Platform renderer cố ý không điền image workload: nó chỉ công bố ECR allow-list. Hai placeholder
image ban đầu được thay atomically bởi workload-release PR đầu tiên kèm digest đã ký.

## Boundary Terraform và Argo CD

| Terraform (`iris-infrastructure`) | Argo CD (`iris-gitops`) |
|---|---|
| VPC, endpoint, EKS, node group | AppProject và child Applications |
| RDS Multi-AZ, S3, SQS, ECR | Knative, KServe, monitoring |
| IAM/OIDC/IRSA, Route53, ACM | ExternalSecret và workload manifests |
| `helm_release.argocd` | MLflow, training lifecycle, inference |
| Root Application bootstrap | Auto-sync/prune/self-heal children |

Argo CD không self-manage. Repo này không chứa `platform-argocd.yaml`, Helm install hoặc bootstrap
script cho Argo CD. Upgrade Argo CD là infrastructure PR; upgrade KServe/Knative/monitoring là
GitOps PR. GitHub Actions không chạy `kubectl apply`, `helm upgrade` hoặc `argocd app sync`.

`platform-namespaces` là owner duy nhất của các Namespace dùng chung (`mlops`, `argo`,
`argo-events`, monitoring và controller namespaces). Child Applications không dùng
`CreateNamespace=true` và workload roots không lặp `Namespace/mlops`, tránh SharedResourceWarning
hoặc prune nhầm toàn bộ workload của Application khác. Mỗi namespace dùng chung có
`argocd.argoproj.io/sync-options: Prune=false`; namespace `argocd` không nằm ở đây vì thuộc
Terraform `helm_release.argocd`.

## CI, credentials và review gate

`validate.yml` chạy unit tests cho ba renderer, build thử release-automation image, render mọi
Kustomize root, kubeconform schema check và enforce ownership policy. Required check ổn định là
`validate`; ruleset solo yêu cầu PR/CI, linear history, resolved conversation và cấm
force-push/delete.

Inference, model-registry và in-cluster model lifecycle dùng ba GitHub App publisher riêng, mỗi App
chỉ có `Actions: write`. Receiver so `github.actor` với component actor do Terraform quản lý, nên
một producer bị compromise không thể tự khai `source_repository` để giả làm producer khác. Platform
handoff dùng App thứ tư `iris-platform-contract-publisher` với cùng quyền tối thiểu và actor gate.
Receiver
dùng GitHub OIDC assume role branch-bound, đọc `iris-gitops-automation` App credential từ Secrets
Manager và mint token chỉ có `Contents/Pull requests: write`. Không repo nào lưu PAT, AWS access key
tĩnh hoặc kubeconfig trong GitHub Secrets.

Các repository variable của repo này do `terraform/github-config` quản lý:

- `AWS_REGION`;
- `RELEASE_AUTOMATION_ECR_REPOSITORY`;
- `RELEASE_AUTOMATION_PUBLISH_AWS_ROLE_ARN`;
- `GITOPS_AUTOMATION_AWS_ROLE_ARN`;
- `GITOPS_AUTOMATION_SECRET_ARN`;
- `PLATFORM_RECONCILE_ALLOWED_ACTOR`;
- `INFERENCE_RELEASE_ALLOWED_ACTOR`, `MODEL_REGISTRY_RELEASE_ALLOWED_ACTOR` và
  `MODEL_RELEASE_ALLOWED_ACTOR`.

Hai ARN dùng để authenticate receiver **không nằm trong platform contract**. Chúng chỉ đến từ
trusted repository variables do `terraform/github-config` quản lý, nên dispatch payload không thể
chọn role hoặc Secrets Manager secret mà workflow sẽ đọc. `source_repository` trong JSON vẫn là
provenance claim; trust thực tế đến từ dedicated App actor, protected infra Environment và PR gate.

## Thứ tự khởi tạo production

1. Terraform platform tạo AWS/EKS, cài Argo CD + Root Application, tạo ECR/IAM/Secrets Manager.
2. Lần platform run đầu dừng fail-closed trước GitOps dispatch nếu hai container chưa có version
   `AWSCURRENT`; Terraform apply trước đó vẫn thành công và không bị rollback.
3. Seed hai GitHub App private key theo runbook hạ tầng; Terraform quản lý container/ARN, không quản
   lý secret value. Dispatch `production-infra.yml` với `scope=handoff` để phát lại contract từ state hiện hành.
4. Orchestrator hạ tầng gọi `github-config-after` bằng reusable workflow để publish trusted receiver
   variables, sau đó gọi `handoff`. Các job dùng Environment `prod` có owner self-approval; dedicated
   publisher App chỉ dispatch contract sau khi config và preflight đã đạt.
5. Review/merge PR `platform-reconcile` để xóa placeholder hạ tầng và tạo ECR allow-list state.
6. Chạy lại `release-automation-image.yml`, review/merge PR pin automation digest.
7. Merge workload release PR cho MLflow và inference images/config.
8. Merge data-pipeline code/dataset; S3 notification mới bắt đầu model lifecycle.

Không phát dataset event khi `RELEASE_AUTOMATION_IMAGE_NOT_PUBLISHED` còn trong WorkflowTemplate.
