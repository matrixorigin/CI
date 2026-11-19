# Persistent PVC authority and transport retention

The lifetime of an uploaded GitHub artifact is not the lifetime of its verified
copy on an independently owned persistent volume. Do not extend old artifact
dates, delete old catalog entries, recreate the full cache, or allow an
unreviewed candidate to approve itself.

## Authorization

`node-cache-pvc-authorization.json` is a separately reviewed, immutable CI input.
Its exact canonical SHA is fixed in `node_cache_pvc_authority.py`. The current
approval covers the three independently revalidated MNL2 volumes. Additional
independently reviewed documents cover MTY/JED/RUH, selected by exact environment
and node/PVC/PV identity (all twelve standard nodes). MTY includes the later
node1 CPU generation and node0/node1 native additions, each independently
full-hashed, with the original base authorization retained beneath it. The
three new documents were first committed and tested by a separate immutable
public Linux issuer before consumer activation. They cannot approve themselves.
The family preserves original entries, dates, producer descriptors and volume
markers. A native input/CPU that has not been published remains
a real cold miss; this change is not native-generation acceptance.

Before approval, the independent reader verifies every index, native manifest,
object and tool archive on **each** exact PVC/PV/node UID. It makes no cache
writes, compiler calls, downloads or retransfers. The operator independently
checks the original protected publication and producer identities, original
payload file/byte counts, unchanged workloads/resources, and UID-scoped helper
retirement. The Linux-tested scanner and its immutable revision are recorded.

Consumers require that exact reviewed document, binding and full runtime
fingerprint, successful primary producer runs, the public independent approval
issuer, and a live revocation check. The protected private origin is authenticated
at grant issuance; its exact receipt and full-byte proof are pinned in the public
CI approval. Ordinary PRs need no new permission to read a private control repo.
They no longer ask whether the **temporary transport** still exists or
has expired. Index and every copied object are still SHA-checked by the original
cache protocol. Go ActionIDs still account for source/dependency changes.
Missing data is not repaired or silently re-authorized by an ordinary PR.

## Revocation and failure

The authoritative live file is `config/node-cache-pvc-revocations.json` on
`matrixorigin/CI:codex/pr-node-cache-two-group-capacity`. It supports explicit
authorization SHA, index SHA, or whole-environment revocation, via an ordinary
reviewed fast-forward change. Consumers verify GitHub's exact file path and Git
blob SHA before parsing. An unavailable check causes a cold miss, never a bypass;
a corrupt/invalid check or false provenance fails validation. A changed volume
UID, changed runtime/toolchain, corrupt index/object, failed source/approval issuer,
or explicit revocation cannot inherit the old authority.

There is no arbitrary periodic compile or time-only invalidation of an unchanged
approved volume. A replacement volume/new generation needs its own independent
full-byte review. The current isolated workflow is not a merged production PR
entrypoint and does not establish all-node/multi-PR capacity acceptance.

The existing PVC Go/CMake restore already checks the complete archive and exact
reviewed binaries without a live artifact lookup. Publication/download stages
still authenticate their transport before introducing any new bytes. PVC restore
zero downloads does not mean the entire job performs no dependency downloads.
