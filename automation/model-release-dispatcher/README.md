# Model release dispatcher

DevOps-owned adapter that translates Argo parameters into the stable `model_release` GitHub event.
It deliberately contains no InferenceService path, YAML renderer or rollout policy. The image is
built from `iris-gitops/main`, pushed to its dedicated immutable ECR repository and referenced from
the WorkflowTemplate by digest.

Only the Dispatch Pod receives `GITHUB_APP_CLIENT_ID` and `GITHUB_APP_PRIVATE_KEY` at runtime.
The credential is never copied into this image.
