# Training workspace: EBS CSI, StorageClass, PVC and PV

Only production is configured. Storage ownership is split deliberately:

| Component | Owner |
| --- | --- |
| EBS CSI EKS managed add-on and controller IRSA | iris-infrastructure Terraform |
| `iris-training-gp3` StorageClass | Argo CD `platform-storage` Application |
| Per-run `workspace` PVC | Argo Workflows `volumeClaimTemplates` |
| PV plus EBS volume/attachment | EBS CSI dynamic provisioner |

```text
Terraform installs driver + scoped IAM
  -> Argo CD reconciles StorageClass
  -> Sensor creates Workflow
  -> Argo creates workspace PVC (1Gi, ReadWriteOnce)
  -> first Pod is scheduled; CSI selects its AZ and creates encrypted gp3/PV
  -> fetch, train and dispatch Pods share the run's workspace
  -> completed Workflow removes PVC -> Delete policy removes PV/EBS
```

There is intentionally no static PV YAML or Terraform `aws_ebs_volume`. A static disk would
couple runs to a particular volume ID/AZ and risks sharing unrelated training data. The non-default
class must be selected explicitly, leaving other platform persistence policies unchanged.
`WaitForFirstConsumer` avoids pre-allocating a disk in the wrong AZ. EBS remains zonal, not
Multi-AZ storage; subsequent Pods using this volume must run in that same AZ. `ReadWriteOnce`
is not multi-node shared storage. The current PVC-consuming DAG stages run sequentially.

The disk uses EBS encryption without a specified custom KMS key. This assumes the account's
default EBS encryption key is accessible to the driver. If the account uses a customer-managed
default key, add the specific KMS grants/key policy in infrastructure; do not grant all KMS keys.

`volumeClaimGC: OnWorkflowCompletion` deliberately deletes temporary data even on failure.
Data/artifacts already persisted in DVC S3, MLflow S3/RDS and the Argo artifact repository are
not deleted by workspace GC. Failed-run files not uploaded to those stores will be lost; capture
required artifacts before completion if debugging requires them. Keep long-lived state off this
class. Volume deletion is asynchronous and still requires a healthy CSI controller/IAM.

## Read-only checks after a future deployment

```bash
kubectl -n kube-system get pods -l app=ebs-csi-controller
kubectl get csidrivers
kubectl get storageclass iris-training-gp3
kubectl -n argo get pvc
kubectl get pv
kubectl -n argo describe pvc <workflow-workspace-pvc>
kubectl -n argo get events --sort-by=.lastTimestamp
```

Before a consumer Pod exists, Pending can be normal with WaitForFirstConsumer. Afterwards check
driver readiness, IRSA errors, EC2 API connectivity, available nodes in the selected AZ and volume
attachment limits. Do not delete a live workflow's PVC as a troubleshooting shortcut.

StorageClass changes to immutable fields require a new class name and a reviewed change for
future workflows, not deletion/recreation of volumes used by active runs. These changes do not
migrate an existing bound PVC, and WorkflowTemplate updates do not mutate already-running runs.

Internal smoke uses `/v1/models/iris:predict` on `iris-classifier-predictor.mlops.svc.cluster.local`.
`iris-classifier` is the Kubernetes/MLflow model name, not the HTTP route name. Bootstrap, canary,
stable, rollback and recovery-finalize all reuse that same smoke template.
