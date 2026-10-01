# Model release automation

This single capstone automation image owns the operational part of the model lifecycle:

- validate a training `model-result-v1` contract and dispatch a release intent;
- generate production smoke traffic;
- evaluate only metrics labelled with the candidate `model_version`;
- promote the accepted MLflow version to the `champion` alias.

It is intentionally separate from the training image. Only the dispatcher container receives the
model-release-publisher GitHub App credential; smoke, evaluator and registry-promoter containers use the
same immutable image but receive no GitHub credential.

## Python runtime upgrades

The supported container runtime is Python 3.12, matching the automation CI tests.
Dependabot may update its patch/digest, but Python major/minor upgrades are a
coordinated manual PR: update the Docker base, the CI Python version and compatible
dependencies together, then pass the unit tests and Docker build before merging.
Do not bypass the build check or install a compiler just to make an unvalidated
runtime upgrade pass. The initial Python 3.14 proposal failed while building
`pyarrow` from source with the existing dependency set.

An already-open Python 3.14 Dependabot PR is not repaired by this policy change;
leave it unmerged and close it after reviewing this replacement policy. A future
runtime upgrade still needs compatibility testing. Dependency/security updates
within the supported runtime remain enabled.
