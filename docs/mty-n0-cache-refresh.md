# MTY node0: one candidate for the PR 32922 group-1 cold comparison

These helpers do not install a publisher, register a runner, grant cache
authority, or dispatch a UT. They are not part of normal PR execution.

1. In a trusted producer with a read-only node0 volume, check out the fixed
   **upstream merge base**, not the fork. Restore the reviewed tools. Configure
   the normal private Go cache and check the runtime before any expensive build
   with `mty_n0_refresh_preflight.py`. Prepare native outputs privately.
   Use the same two-group planner, Go flags and package discovery as the cold
   worker. `mty_n0_prepare_group1.py` freezes the private compile-only candidate.
2. A **separate protected job** must get the exact producer run, caller/workflow
   revisions, artifact ID and digest from GitHub, not from a fork job. Pin the
   reviewed entry bytes separately, including the target runtime and native
   key from the approved build request. Do not accept arbitrary entries or
   pins from a workflow-dispatch input. Run `mty_n0_verify_candidate.py` with
   that entry, its protected SHA-256 and a fresh private staging directory.
   The existing transport verifier authenticates the run/artifact and hashes
   every indexed object. Its receipt **does not authorize publication**.
3. The protected operator must check actual node/PV/PVC UIDs, capacity, active
   workloads and revocation before admitting a missing-object-only append.
   Do not reuse the old CPU/native-only publication intents. On completion,
   mount the exact volume read-only in an independent reader and run the
   existing `node_cache_pvc_scan.py` with an independently pinned catalog
   containing the old entries plus this exact new entry bound only to node0.
   Issue the consumer grant only after that receipt and protected publication
   provenance are verified. The original indexes and grants remain intact.
4. Only then compare **one group 1**, same PR head and MTY node0, against
   [the cold job](https://github.com/matrixorigin/matrixone/actions/runs/44685555632/job/136118920832).
   Report shared reads separately from private hits, native hit, actual compile
   calls and wall time. A verifier unit test or a successful copy is not a UT.
