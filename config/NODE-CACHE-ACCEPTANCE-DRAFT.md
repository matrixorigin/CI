# Isolated full-suite activation gate

`node-cache-full-acceptance.yaml` is a local draft, not a running experiment.
The original production workflow and original runner pools are unchanged.

Before activation:

1. Independently audit each protected initial publication receipt and exact
   environment/node/PVC/PV identities; commit only those published bindings to
   `node-compile-cache-catalog.json`.
2. Safely create new node-bound reader pools with fresh combined resource
   budgets. Each may have multiple temporary runners, sharing only a read-only
   cache mount and separate mutable work directories.
3. Commit the complete two/three-group helpers and populated catalog, then
   replace all three helper checkout pins in the isolated workflow with that
   immutable commit. The temporary current pin predates this new catalog.
4. Add an existing-branch caller only after its prior CodeQL completes and its
   remote head matches. Do not alter or cancel current runs.
5. Run full same-source acceptance, review compile-only delta artifacts, and
   publish only new objects through a protected append path. Legacy native
   manifests lack v2 tool-hash evidence and are not relabeled as native hits.
6. Review a controlled changed-source commit into the source allowlist, then
   independently accept fresh runners, changed grouping and changed SHA in
   all four environments. Fixture tests or conversion alone do not prove this.

The shared graph uses Go input-derived ActionIDs, not SHA or group as its cache
key. Native changes retain independent input/toolchain/content checks. The
probe requires a real approved parent generation; production cold-miss policy
remains unchanged. Full tests execute with count=1, p=7, original timeouts,
same-environment S3 and complete scope/profile/skip evidence.
