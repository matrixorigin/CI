"""Read bounded, non-secret CS placement offers using a dedicated read-only identity."""

import http.client
import json
import multiprocessing
import os
import re
import ssl
import time
import urllib.request
from decimal import Decimal, ROUND_CEILING
from urllib.parse import urlencode, urlsplit

SITES = ("mty", "jed", "mnl2", "ruh")
POOLS = frozenset(f"cs-{site}-nc-n{node}-pr-v1" for site in SITES
                  for node in range(3) if (site, node) != ("mty", 2))
LABEL = "cs-node-cache-7c14g"
WORK_CLASS = "directpv-min-io-ephemeral"
GIB = 1024 ** 3
MAX_BUCKET = 128  # The selector rejects out-of-range receipts.
MAX_PAGE_BYTES = 8 * 1024 * 1024
SNAPSHOT_SECONDS = 15
ARC_API = "actions.github.com/v1alpha1"
ARC_NAMESPACE = "cicd"
ARC_PATH = f"/apis/{ARC_API}/namespaces/{ARC_NAMESPACE}"
ARS_PATH = f"{ARC_PATH}/autoscalingrunnersets"
ERS_PATH = f"{ARC_PATH}/ephemeralrunnersets"
DRIVES_PATH = "/apis/directpv.min.io/v1beta1/directpvdrives"
CLASSES_PATH = "/apis/storage.k8s.io/v1/storageclasses"
NODES_PATH = "/api/v1/nodes"
PODS_PATH = "/api/v1/pods"
QUANTITIES = {"": 1, "n": Decimal("1e-9"), "u": Decimal("1e-6"), "m": Decimal("1e-3")}
QUANTITIES.update({s: 1000 ** i for i, s in enumerate("kMGTPE", 1)})
QUANTITIES.update({s + "i": 1024 ** i for i, s in enumerate("KMGTPE", 1)})


def amount(raw):
    """Supported Kubernetes resource quantities; unknown formats make the offer unavailable."""
    match = re.fullmatch(r"([0-9]{1,20}(?:\.[0-9]{1,12})?)(n|u|m|k|[MGTPE]|[KMGTPE]i)?", raw)
    if not match:
        raise ValueError("unrecognized resource quantity")
    return Decimal(match[1]) * QUANTITIES[match[2] or ""]


def pod_request(spec, resource):
    """Account for regular containers, restartable inits, init peaks and overhead."""
    def request(container):
        return amount(container.get("resources", {}).get("requests", {}).get(resource, "0"))

    sidecars = init_peak = Decimal(0)
    for container in spec.get("initContainers", []):
        req = request(container)
        init_peak = max(init_peak, sidecars + req)
        if container.get("restartPolicy") == "Always":
            sidecars += req
    workload = sum(request(c) for c in spec["containers"]) + sidecars
    pod_level = amount(spec.get("resources", {}).get("requests", {}).get(resource, "0"))
    overhead = amount(spec.get("overhead", {}).get(resource, "0"))
    factor = 1000 if resource == "cpu" else 1
    return int(((max(workload, init_peak, pod_level) + overhead) * factor).to_integral_value(
        rounding=ROUND_CEILING))


def tolerates(taint, toleration):
    if toleration.get("effect", "") not in ("", taint["effect"]):
        return False
    operator = toleration.get("operator", "Equal")
    if operator == "Exists":
        return toleration.get("key", "") in ("", taint["key"])
    return (operator == "Equal" and toleration.get("key") == taint["key"]
            and toleration.get("value", "") == taint.get("value", ""))


def ready_node(node, template, host):
    conditions = {c["type"]: c["status"] for c in node["status"]["conditions"]}
    selector = template["nodeSelector"]
    labels = node["metadata"]["labels"]
    return (not node["spec"].get("unschedulable", False)
            and conditions.get("Ready") == "True"
            and all(conditions.get(k) == "False" for k in ("MemoryPressure", "DiskPressure", "PIDPressure"))
            and conditions.get("NetworkUnavailable", "False") == "False"
            and selector.get("kubernetes.io/hostname") == host
            and all(labels.get(k) == v for k, v in selector.items())
            and template.get("nodeName", host) == host
            # Do not guess that a new hard scheduling constraint can be satisfied.
            and not template.get("affinity", {}).get("nodeAffinity", {}).get("requiredDuringSchedulingIgnoredDuringExecution")
            and not template.get("affinity", {}).get("podAffinity", {}).get("requiredDuringSchedulingIgnoredDuringExecution")
            and not template.get("affinity", {}).get("podAntiAffinity", {}).get("requiredDuringSchedulingIgnoredDuringExecution")
            and not any(r.get("whenUnsatisfiable") == "DoNotSchedule" for r in template.get("topologySpreadConstraints", []))
            and all(any(tolerates(taint, t) for t in template.get("tolerations", []))
                    for taint in node["spec"].get("taints", []) if taint["effect"] in ("NoSchedule", "NoExecute")))


def runner_set(ars, ephemeral_sets):
    """Use the listener's desired replicas from the current, owned runner set."""
    owner = ars["metadata"]
    owned = [r for r in ephemeral_sets if any(
        ref.get("controller") is True and ref["kind"] == "AutoscalingRunnerSet"
        and ref["apiVersion"] == ARC_API and ref["uid"] == owner["uid"] and ref["name"] == owner["name"]
        for ref in r["metadata"].get("ownerReferences", []))]
    if len(owned) == 1 and not owned[0]["metadata"].get("deletionTimestamp"):
        return owned[0]
    return None


def pool_offer(ars, node, drives, active_pods, ephemeral_sets):
    metadata, spec, status = ars["metadata"], ars["spec"], ars["status"]
    pool = metadata["name"]
    host = node["metadata"]["name"]
    template = spec["template"]["spec"]
    if (spec.get("runnerScaleSetName") != pool or LABEL not in spec.get("runnerScaleSetLabels", [])
            or metadata.get("deletionTimestamp") or not metadata.get("annotations", {}).get("runner-scale-set-id")
            or status.get("observedGeneration", metadata.get("generation")) != metadata.get("generation")
            or status.get("phase") != "Running" or not ready_node(node, template, host)):
        return None
    active_set = runner_set(ars, ephemeral_sets)
    if active_set is None:
        return None
    replicas = active_set["spec"].get("replicas", 0)  # Zero is omitted by ARC.
    limit = spec.get("maxRunners")
    if type(replicas) is not int or replicas < 0 or (limit is not None and (type(limit) is not int or limit < 0)):
        return None
    work, = [v["ephemeral"]["volumeClaimTemplate"]["spec"] for v in template["volumes"]
             if v["name"] == "work"]
    if work.get("storageClassName") != WORK_CLASS:
        return None
    work_size = amount(work["resources"]["requests"]["storage"])
    cpu, mem = (pod_request(template, key) for key in ("cpu", "memory"))
    if work_size < 80 * GIB or cpu < 7000 or mem < 12 * GIB:
        return None
    alloc = node["status"]["allocatable"]
    free_cpu = int(amount(alloc["cpu"]) * 1000) - sum(pod_request(p["spec"], "cpu") for p in active_pods)
    free_mem = int(amount(alloc["memory"])) - sum(pod_request(p["spec"], "memory") for p in active_pods)
    # Never add fractions across drives: the whole PVC must fit one eligible drive.
    disk_slots = 0
    for drive in drives:
        metadata, capacity = drive["metadata"], drive["status"]
        labels = metadata["labels"]
        if (labels.get("directpv.min.io/node") != host or labels.get("directpv.min.io/tier") != "ephemeral"
                or capacity.get("status") != "Ready" or metadata.get("deletionTimestamp")
                or drive.get("spec", {}).get("unschedulable")):
            continue
        free = capacity["freeCapacity"]
        if type(free) is not int or free < 0:
            raise ValueError("invalid work-disk capacity")
        disk_slots += int(free // work_size)
    # ARC derives pod names from the owned set. Bound pods already consume the
    # node's resources, so only the remaining desired replicas claim new slots.
    prefix = active_set["metadata"]["name"] + "-runner-"
    bound = sum(p["namespace"] == ARC_NAMESPACE and p["name"].startswith(prefix)
                and p.get("labels", {}).get("actions-ephemeral-runner") == "True"
                for p in (pod["metadata"] for pod in active_pods))
    pending = max(0, replicas - bound)
    slots = max(0, min(free_cpu // cpu, free_mem // mem, disk_slots, MAX_BUCKET))
    if limit is not None:
        slots = min(slots, max(0, limit - bound))
    return {"pool": pool, "slots": slots, "pending": min(MAX_BUCKET, pending)}


def collect_site(site, get_items):
    """Return aggregate slots only; do not carry Kubernetes objects into Actions outputs."""
    nodes = {n["metadata"]["name"]: n for n in get_items(NODES_PATH, {})}
    scales = get_items(ARS_PATH, {})
    storage_class, = [c for c in get_items(CLASSES_PATH, {"fieldSelector": f"metadata.name={WORK_CLASS}"})
                      if c["metadata"]["name"] == WORK_CLASS]
    if (storage_class["provisioner"] != "directpv-min-io"
            or storage_class["parameters"].get("directpv.min.io/tier") != "ephemeral"
            or storage_class["volumeBindingMode"] != "WaitForFirstConsumer"):
        return []
    drives = get_items(DRIVES_PATH, {"labelSelector": "directpv.min.io/tier=ephemeral"})
    if terms := storage_class.get("allowedTopologies"):
        drives = [d for d in drives if any(
            all(d["status"].get("topology", {}).get(req["key"]) in req["values"]
                for req in term["matchLabelExpressions"])
            for term in terms)]
    ephemeral_sets = get_items(ERS_PATH, {})
    rows = []
    for ars in scales:
        pool = ars["metadata"]["name"]
        if pool not in POOLS or not pool.startswith(f"cs-{site}-"):
            continue
        host = f"mo-standard-{pool.split('-')[3][1:]}"
        if host not in nodes:
            continue
        pods = get_items(PODS_PATH, {"fieldSelector": f"spec.nodeName={host},status.phase!=Succeeded,status.phase!=Failed"})
        active = [p for p in pods if p["spec"].get("nodeName") == host
                  and p["status"]["phase"] not in ("Succeeded", "Failed")]
        if row := pool_offer(ars, nodes[host], drives, active, ephemeral_sets):
            rows.append(row)
    return rows


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # Do not send bearer credentials to a redirected host.


def kube_reader(config):
    base, token = config["server"], config["token"]
    origin = urlsplit(base)
    if (origin.scheme != "https" or not origin.hostname or origin.username or origin.password
            or origin.path not in ("", "/") or origin.query or origin.fragment
            or not isinstance(token, str) or not token or re.search(r"\s", token)):
        raise ValueError("invalid read-only capacity connection")
    ctx = ssl.create_default_context(cadata=config.get("ca_pem"))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                          urllib.request.HTTPSHandler(context=ctx), NoRedirect())
    deadline = time.monotonic() + SNAPSHOT_SECONDS

    def get_items(path, filters):
        items, cursor = [], None
        for _ in range(8):
            query = dict(filters, limit=250)
            if cursor:
                query["continue"] = cursor
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("capacity read budget exhausted")
            req = urllib.request.Request(base.rstrip("/") + path + "?" + urlencode(query),
                                         headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
            with opener.open(req, timeout=min(6, remaining)) as response:
                raw = response.read(MAX_PAGE_BYTES + 1)
            if len(raw) > MAX_PAGE_BYTES:
                raise ValueError("capacity response exceeds page limit")
            page = json.loads(raw)
            items.extend(page["items"])
            cursor = page["metadata"].get("continue")
            if not cursor:
                return items
        raise ValueError("incomplete capacity inventory")

    return get_items


def site_offer(site, config, reader):
    try:
        return collect_site(site, reader(config))
    except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError, OSError, http.client.HTTPException):
        # Connection errors can include endpoints and headers. Return no details.
        return None


def snapshot(configs, reader=kube_reader):
    receipt = {"captured_at": int(time.time()), "pools": []}
    sites = [site for site in SITES if site in configs]
    if not sites:
        return receipt
    deadline = time.monotonic() + SNAPSHOT_SECONDS
    # The hosted step is on the critical path for both groups. Terminate any
    # unfinished site reads at exit, even if DNS or a streaming body is stuck.
    with multiprocessing.get_context("spawn").Pool(len(sites)) as pool:
        reads = [(site, pool.apply_async(site_offer, (site, configs[site], reader))) for site in sites]
        for site, result in reads:
            try:
                rows = result.get(timeout=max(0, deadline - time.monotonic()))
            except multiprocessing.TimeoutError:
                rows = None
            if rows is None:
                print(f"Capacity unavailable for {site.upper()}; will not target its pools.", flush=True)
            else:
                receipt["pools"].extend(rows)
    return receipt


def main():
    raw = os.environ.get("CS_COVERAGE_CAPACITY_READ")
    try:
        configs = json.loads(raw) if raw else {}
        if not isinstance(configs, dict) or not configs.keys() <= set(SITES):
            raise ValueError("invalid capacity configuration")
    except ValueError:
        configs = {}
    data = snapshot(configs)
    if not any(row["slots"] > row["pending"] for row in data["pools"]):
        print("No verified CS capacity offer; keeping the shared queue.", flush=True)
        return
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        print("snapshot=" + json.dumps(data, separators=(",", ":")), file=output)
    print(f"Checked capacity for {len(data['pools'])} approved CS pools.", flush=True)


if __name__ == "__main__":
    main()
