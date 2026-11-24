"""Preflight the pinned cache catalog without requiring every producer to still exist."""

import json
from pathlib import Path
import sys
from urllib.error import HTTPError


def preflight(root, reviewed, api):
    catalog, _ = reviewed.load(root)
    missing = set()
    for binding in catalog["nodes"]:
        local = reviewed.cache.catalog_for_node(catalog, binding)
        # These checks include the live revocation registry and approval issuers.
        # Keep their errors separate from a missing individual producer run.
        grant = reviewed.authority(local, api)
        for entry in local["entries"]:
            reviewed.c.require(grant.covers(entry), "cache entry outside durable grant")
            producer = entry["producer"]
            producer_id = producer["id"]
            expected_url = f"https://api.github.com/repos/matrixorigin/matrixone/actions/runs/{producer_id}"
            try:
                permitted = grant.permits(entry, entry["runtime"])
            except HTTPError as exc:
                if exc.code != 404 or exc.url != expected_url or producer["repository"] != "matrixorigin/matrixone":
                    raise
                # No approval is granted here. The assigned worker still verifies
                # provenance itself and skips entries whose metadata it cannot read.
                missing.add(producer_id)
                continue
            reviewed.c.require(permitted is True, "native durable source approval unavailable or revoked")
    return sorted(missing)


if __name__ == "__main__":
    import node_cache_native_pvc_catalog as reviewed

    missing = preflight(Path(sys.argv[1]), reviewed, reviewed.cache.legacy.api)
    print(json.dumps({"unavailable_cache_producer_runs": missing}))
    if missing:
        print("::warning::Some cache producer runs no longer resolve; workers will independently validate cache entries before reuse.")
