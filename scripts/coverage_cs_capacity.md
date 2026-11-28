# PR #32922 capacity-aware canary

The `enable_cs_first_pool` input is default-off. Only the initial
`pull_request_target` run from MatrixOne `main` for open, eligible PR #32922
can use it. Synchronize the standby caller, worker pins and cross-repository
contract to the same reviewed CI revision before opting in. No other PR is
changed by the canary.

The trusted preflight reads an optional `CS_COVERAGE_CAPACITY_READ` secret.
It is a JSON object keyed by site (`mty`, `jed`, `mnl2`, `ruh`); an entry
has `server` (an HTTPS Kubernetes API origin), `token` (a dedicated, preferably
short-lived read-only bearer credential) and optionally `ca_pem` (its trusted
CA certificate). Use a platform-approved network path and restricted identity.
Do not copy a personal kubeconfig or grant a CI credential access to Secrets
or to any write API. The read-only identity needs list access to nodes, pods,
storage classes, and DirectPV drives, plus list access to the ARC runner sets
in the `cicd` namespace. The preflight filters pod reads to the candidate node
or pending `cicd` pods.

No credential or Kubernetes resource body is passed to selectors or workers.
Only a timestamp and numerical capacity counts for allowlisted pools are
passed. Missing credentials, inaccessible data, stale data and insufficient
resources fall back to the shared queue. Partial data may only nominate a pool
whose capacity was observed. No general capacity reservation is made.

The first exact probe returns to the normal shared CS path if still queued and
its cancellation is confirmed. The 1-hour Shanghai timer is measured from the
first observed CS queue wait and is not reset by the probe. Assigned workers
are not moved.
