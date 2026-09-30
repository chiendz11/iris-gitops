# Model release automation

This single capstone automation image owns the operational part of the model lifecycle:

- validate a training `model-result-v1` contract and dispatch a release intent;
- generate production smoke traffic;
- evaluate only metrics labelled with the candidate `model_version`;
- promote the accepted MLflow version to the `champion` alias.

It is intentionally separate from the training image. Only the dispatcher container receives the
model-release-publisher GitHub App credential; smoke, evaluator and registry-promoter containers use the
same immutable image but receive no GitHub credential.
