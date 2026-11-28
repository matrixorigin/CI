# Capacity-aware first placement for CS Coverage UT

The `enable_cs_first_pool` input is default-off. Once enabled in the protected
MatrixOne entrypoint, the initial run of every PR authorized for CS Coverage
against `main` or the existing opted-in `4.2-dev` path can use it, including
Draft PRs and PRs without `kind/test-ci`. It does not change the live PR identity,
membership, source or fixture checks. Synchronize the standby caller, both
worker pins and cross-repository contract to the same reviewed CI revision
before enabling the caller's `CS_COVERAGE_FIRST_POOL_ENABLED` setting. Missing
or disabled settings leave the existing routing in place.

Only the GitHub-hosted trusted preflight, after PR authorization, reads the
optional `CS_COVERAGE_CAPACITY_READ` secret.
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
whose capacity was observed. Each initial parent run reads at most one bounded
snapshot for both groups. Tie-breaking rotates between equally suitable pools
by parent run ID; pending demand is deducted, but no capacity is reserved
across PRs. Validate collector reachability, API load and real allocations
before enabling the repository setting.

An exact probe uses an approved CS scale set and a worker dispatched by GitHub
Actions. The worker still needs the exact parent grant before checking out PR
code. If the probe is still unassigned, it returns to the normal shared CS queue
only after cancellation is confirmed. The capacity-aware mode does not add the
legacy site sweep. The 1-hour Shanghai timer is measured from the first observed
CS queue wait and is not reset by the probe. Assigned workers are not moved.
