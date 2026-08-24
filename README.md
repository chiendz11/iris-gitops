# Iris production GitOps

Repo này là **nguồn sự thật duy nhất** cho desired state của production EKS. Ba repo ứng dụng
chỉ test, build và push image ECR; chúng không được giữ credential Kubernetes hay chạy
`kubectl apply` vào production.

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

## Cấu trúc

- `bootstrap/`: cài Argo CD một lần và tạo root Application.
- `applications/`: AppProject và các child Application (App of Apps).
- `platform/`: Knative/Kourier và cấu hình platform do Argo CD quản lý.
- `environments/production/`: desired state của MLflow, training lifecycle và inference.

Helm chart bên thứ ba được pin version trong các Application. Workload tự viết dùng Kustomize.
Argo CD sync waves đảm bảo CRD/controller có trước custom resource.

## Bootstrap

1. Bootstrap và merge platform ở repo `iris-infrastructure`.
2. Workflow hạ tầng tự mở PR thay output Terraform không nhạy cảm trong repo này.
3. Review/merge PR output; image placeholder được ba application repo cập nhật bằng PR riêng.
4. Cấu hình Git repository credential nếu repo chuyển thành private.
5. Chạy:

```bash
export EKS_CLUSTER_NAME=iris-mlops-prod
export AWS_REGION=ap-southeast-1
./bootstrap/install-argocd.sh
```

Sau bootstrap, không apply workload bằng tay. Mọi thay đổi production đi qua pull request repo này.
AWS Load Balancer Controller tạo NLB cho Kourier; ExternalDNS tự reconcile hostname phẳng với
Route53 nên không có lần Terraform apply thứ hai.

## Biến GitHub của ba app repo

- `GITOPS_REPOSITORY=chiendz11/iris-gitops`
- Secret `GITOPS_TOKEN`: fine-grained token hoặc GitHub App token chỉ có quyền
  Contents/PR trên repo GitOps.
- Các biến ECR/AWS hiện có vẫn được dùng để build và upload.

Không dùng PAT cá nhân lâu dài trong production; GitHub App là lựa chọn tốt hơn.
