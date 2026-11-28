import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import coverage_cs_capacity as cap
from scripts import coverage_standby as standby


HOST = "mo-standard-1"
PROFILE = "matrixone.cloud/profile"


def site_reader(site, free, *, pending=0, pod_cpu="0", pod_mem="0", ready=True, taints=()):
    name = f"cs-{site}-nc-n1-pr-v1"
    node = {"metadata": {"name": HOST, "labels": {"kubernetes.io/hostname": HOST, PROFILE: "cn.cloudsigma"}},
            "spec": {"taints": list(taints)},
            "status": {"conditions": [{"type": k, "status": v} for k, v in
                                      {"Ready": "True" if ready else "False", "DiskPressure": "False",
                                       "MemoryPressure": "False", "PIDPressure": "False"}.items()],
                       "allocatable": {"cpu": "16", "memory": "28Gi"}}}
    template = {"nodeSelector": {"kubernetes.io/hostname": HOST, PROFILE: "cn.cloudsigma"},
                "containers": [{"name": "runner", "resources": {"requests": {"cpu": "7", "memory": "12Gi"}}}],
                "volumes": [{"name": "work", "ephemeral": {"volumeClaimTemplate": {
                    "spec": {"storageClassName": cap.WORK_CLASS, "resources": {"requests": {"storage": "80Gi"}}}}}}]}
    scale = {"metadata": {"name": name, "annotations": {"runner-scale-set-id": "1"}},
             "spec": {"runnerScaleSetName": name, "runnerScaleSetLabels": [cap.LABEL],
                      "template": {"spec": template}},
             "status": {"phase": "Running", "pendingEphemeralRunners": pending}}
    drives = [{"metadata": {"labels": {"directpv.min.io/node": HOST, "directpv.min.io/tier": "ephemeral"}},
               "status": {"status": "Ready", "freeCapacity": value * cap.GIB,
                          "topology": {"directpv.min.io/identity": "allowed"}}} for value in free]
    storage_class = {"metadata": {"name": cap.WORK_CLASS}, "provisioner": "directpv-min-io",
                     "volumeBindingMode": "WaitForFirstConsumer", "parameters": {"directpv.min.io/tier": "ephemeral"},
                     "allowedTopologies": [{"matchLabelExpressions": [
                         {"key": "directpv.min.io/identity", "values": ["allowed"]}]}]}
    pod = {"spec": {"nodeName": HOST, "containers": [{"resources": {"requests": {"cpu": pod_cpu, "memory": pod_mem}}}]},
           "status": {"phase": "Running"}}

    def get(path, filters):
        if path == cap.PODS_PATH:
            if filters.get("fieldSelector") != f"spec.nodeName={HOST},status.phase!=Succeeded,status.phase!=Failed":
                raise AssertionError("capacity read must be limited to a candidate node")
            return [pod]
        return {cap.NODES_PATH: [node], cap.ARS_PATH: [scale], cap.CLASSES_PATH: [storage_class],
                cap.DRIVES_PATH: drives, cap.CI_PODS_PATH: []}[path]

    return get


class Capacity(unittest.TestCase):
    def test_disk_must_fit_whole_claim_and_not_block_other_sites(self):
        inventory = {"jed": site_reader("jed", [34]), "mnl2": site_reader("mnl2", [159]),
                     "ruh": site_reader("ruh", [80, 79])}
        receipt = cap.snapshot(inventory, reader=lambda get: get)
        self.assertEqual(receipt["pools"], [
            {"pool": "cs-jed-nc-n1-pr-v1", "slots": 0, "pending": 0},
            {"pool": "cs-mnl2-nc-n1-pr-v1", "slots": 1, "pending": 0},
            {"pool": "cs-ruh-nc-n1-pr-v1", "slots": 1, "pending": 0},
        ])
        self.assertEqual(standby.first_cs_pool_plan(json.dumps(receipt), receipt["captured_at"]),
                         ("cs-mnl2-nc-n1-pr-v1", "cs-ruh-nc-n1-pr-v1"))
        row, = cap.collect_site("jed", site_reader("jed", [63, 73]))
        self.assertEqual(row["slots"], 0)

    def test_allocatable_pending_and_node_readiness(self):
        for kwargs, expected in [
            ({"pending": 2}, (2, 2)),
            ({"pod_cpu": "1600m", "pod_mem": "5Gi"}, (1, 0)),
            ({"pod_cpu": "9100m"}, (0, 0)),
            ({"ready": False}, None),
            ({"taints": [{"key": "reserved", "effect": "NoSchedule"}]}, None),
        ]:
            with self.subTest(kwargs=kwargs):
                rows = cap.collect_site("mnl2", site_reader("mnl2", [160], **kwargs))
                self.assertEqual([(r["slots"], r["pending"]) for r in rows], [] if expected is None else [expected])

    def test_init_container_resources_and_overhead(self):
        spec = {"containers": [{"resources": {"requests": {"cpu": "2"}}}],
                "initContainers": [
                    {"restartPolicy": "Always", "resources": {"requests": {"cpu": "700m"}}},
                    {"resources": {"requests": {"cpu": "2500m"}}},
                ], "overhead": {"cpu": "45m"}}
        self.assertEqual(cap.pod_request(spec, "cpu"), 3245)

    def test_excludes_drive_outside_work_volume_topology(self):
        inventory = site_reader("jed", [180])

        def get(path, filters):
            items = inventory(path, filters)
            if path == cap.DRIVES_PATH:
                items[0]["status"]["topology"]["directpv.min.io/identity"] = "wrong"
            return items

        row, = cap.collect_site("jed", get)
        self.assertEqual(row["slots"], 0)

    def test_missing_or_unreachable_sites_do_not_emit_credentials(self):
        def unavailable(config):
            raise OSError("test-sensitive connection information")

        with patch("builtins.print") as out:
            receipt = cap.snapshot({"mty": {}}, reader=unavailable)
        self.assertEqual(receipt["pools"], [])
        self.assertNotIn("test-sensitive", str(out.call_args_list))
        self.assertEqual(standby.first_cs_pool_plan(json.dumps(receipt), receipt["captured_at"]), ())

    def test_output_is_aggregate_only_and_empty_credentials_keep_shared_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp, "outputs")
            with patch.dict(os.environ, CS_COVERAGE_CAPACITY_READ="", GITHUB_OUTPUT=str(output)):
                cap.main()
            self.assertFalse(output.exists())
            secret = '{"jed":{"token":"test-private-capacity-key"}}'
            receipt = {"captured_at": 123, "pools": [{"pool": "cs-jed-nc-n1-pr-v1", "slots": 1, "pending": 0}]}
            with patch.dict(os.environ, CS_COVERAGE_CAPACITY_READ=secret, GITHUB_OUTPUT=str(output)), \
                 patch.object(cap, "snapshot", return_value=receipt):
                cap.main()
            self.assertEqual(output.read_text().strip().split("=", 1)[1], json.dumps(receipt, separators=(",", ":")))

    def test_https_read_follows_pagination_but_not_redirects(self):
        config = {"server": "https://capacity.invalid", "token": "test-only"}
        opener = Mock()
        opener.open.side_effect = [
            io.BytesIO(b'{"metadata":{"continue":"next/+?"},"items":[1]}'),
            io.BytesIO(b'{"metadata":{},"items":[2]}'),
        ]
        with patch.object(cap.urllib.request, "build_opener", return_value=opener):
            get = cap.kube_reader(config)
            self.assertEqual(get(cap.DRIVES_PATH, {"labelSelector": "test=only"}), [1, 2])
        self.assertIn("continue=next%2F%2B%3F", opener.open.call_args.args[0].full_url)
        self.assertEqual(opener.open.call_args.args[0].get_method(), "GET")
        self.assertIsNone(cap.NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere.invalid"))
        for bad in ["http://capacity.invalid", "https://user@c.invalid", "https://c.invalid/other", "https://c.invalid/?x=1"]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                cap.kube_reader(dict(config, server=bad))

    def test_collector_selector_and_worker_allowlist_are_consistent(self):
        workflows = Path(__file__).resolve().parents[1] / ".github/workflows"
        worker = (workflows / "coverage-worker-cs.yaml").read_text()
        allowlist = set(re.findall(r'"(cs-[a-z0-9]+-nc-n[0-2]-pr-v1)"', worker))
        self.assertEqual(allowlist, cap.POOLS)
        self.assertEqual(standby.FIRST_CS_POOLS, cap.POOLS)
        selector = (workflows / "coverage-queue-handoff.yaml").read_text()
        self.assertNotIn("CS_COVERAGE_CAPACITY_READ", selector)
        self.assertNotIn("CS_COVERAGE_CAPACITY_READ", worker)
        self.assertIn("COVERAGE_CS_FIRST_POOL_SNAPSHOT: ${{ inputs.first_pool_snapshot }}", selector)


if __name__ == "__main__":
    unittest.main()
