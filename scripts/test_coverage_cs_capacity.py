import io
import json
import os
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import coverage_cs_capacity as cap
from scripts import coverage_standby as standby


HOST = "mo-standard-1"
PROFILE = "matrixone.cloud/profile"


def site_reader(site, free, *, replicas=0, limit=None, runner_bound=False, pod_cpu="0", pod_mem="0", ready=True, taints=()):
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
    scale = {"metadata": {"name": name, "uid": "scale-uid", "generation": 1, "annotations": {"runner-scale-set-id": "1"}},
             "spec": {"runnerScaleSetName": name, "runnerScaleSetLabels": [cap.LABEL],
                      "template": {"spec": template}},
             "status": {"phase": "Running", "observedGeneration": 1}}
    if limit is not None:
        scale["spec"]["maxRunners"] = limit
    runner_set = {"metadata": {"name": name, "ownerReferences": [
                      {"apiVersion": cap.ARC_API, "kind": "AutoscalingRunnerSet", "name": name,
                       "uid": "scale-uid", "controller": True}]},
                  "spec": {"replicas": replicas} if replicas else {}}
    drives = [{"metadata": {"labels": {"directpv.min.io/node": HOST, "directpv.min.io/tier": "ephemeral"}},
               "status": {"status": "Ready", "freeCapacity": value * cap.GIB,
                          "topology": {"directpv.min.io/identity": "allowed"}}} for value in free]
    storage_class = {"metadata": {"name": cap.WORK_CLASS}, "provisioner": "directpv-min-io",
                     "volumeBindingMode": "WaitForFirstConsumer", "parameters": {"directpv.min.io/tier": "ephemeral"},
                     "allowedTopologies": [{"matchLabelExpressions": [
                         {"key": "directpv.min.io/identity", "values": ["allowed"]}]}]}
    pod = {"metadata": {"name": "unrelated", "namespace": "other"},
           "spec": {"nodeName": HOST, "containers": [{"resources": {"requests": {"cpu": pod_cpu, "memory": pod_mem}}}]},
           "status": {"phase": "Running"}}
    if runner_bound:
        pod["metadata"] = {"name": f"{name}-runner-fresh", "namespace": "cicd",
                           "labels": {"actions-ephemeral-runner": "True"}}
        pod["spec"]["containers"] = template["containers"]

    def get(path, filters):
        if path == cap.PODS_PATH:
            if filters.get("fieldSelector") != f"spec.nodeName={HOST},status.phase!=Succeeded,status.phase!=Failed":
                raise AssertionError("capacity read must be limited to a candidate node")
            return [pod]
        return {cap.NODES_PATH: [node], cap.ARS_PATH: [scale], cap.CLASSES_PATH: [storage_class],
                cap.DRIVES_PATH: drives, cap.ERS_PATH: [runner_set]}[path]

    return get


def fixture_reader(config):
    if config.get("stall"):
        time.sleep(60)
    if error := config.get("error"):
        raise OSError(error)
    return site_reader(**config)


class Capacity(unittest.TestCase):
    def test_disk_must_fit_whole_claim_and_not_block_other_sites(self):
        inventory = {site: dict(site=site, free=space)
                     for site, space in (("jed", [34]), ("mnl2", [159]), ("ruh", [80, 79]))}
        receipt = cap.snapshot(inventory, reader=fixture_reader)
        self.assertEqual(receipt["pools"], [
            {"pool": "cs-jed-nc-n1-pr-v1", "slots": 0, "pending": 0},
            {"pool": "cs-mnl2-nc-n1-pr-v1", "slots": 1, "pending": 0},
            {"pool": "cs-ruh-nc-n1-pr-v1", "slots": 1, "pending": 0},
        ])
        self.assertCountEqual(standby.first_cs_pool_plan(json.dumps(receipt), receipt["captured_at"]),
                              ("cs-mnl2-nc-n1-pr-v1", "cs-ruh-nc-n1-pr-v1"))
        row, = cap.collect_site("jed", site_reader("jed", [63, 73]))
        self.assertEqual(row["slots"], 0)

    def test_allocatable_pending_and_node_readiness(self):
        for kwargs, expected in [
            ({"replicas": 2}, (2, 2)),
            ({"pod_cpu": "1600m", "pod_mem": "5Gi"}, (1, 0)),
            ({"pod_cpu": "9100m"}, (0, 0)),
            ({"ready": False}, None),
            ({"taints": [{"key": "reserved", "effect": "NoSchedule"}]}, None),
        ]:
            with self.subTest(kwargs=kwargs):
                rows = cap.collect_site("mnl2", site_reader("mnl2", [160], **kwargs))
                self.assertEqual([(r["slots"], r["pending"]) for r in rows], [] if expected is None else [expected])

    def test_listener_replicas_and_capped_slots_use_same_fields_across_arc_versions(self):
        for version, status in (
            ("0.14", {"phase": "Running", "currentRunners": 1, "pendingEphemeralRunners": 1}),
            ("0.15", {"phase": "Running", "observedGeneration": 1}),
        ):
            for replicas, limit, bound, available in ((0, 2, False, 2), (1, 1, False, 0),
                                                       (1, 2, True, 1), (1, 1, True, 0), (0, 1, True, 0)):
                with self.subTest(version=version, replicas=replicas, limit=limit, bound=bound):
                    inventory = site_reader("jed", [160], replicas=replicas, limit=limit, runner_bound=bound)

                    def get(path, filters):
                        rows = inventory(path, filters)
                        if path == cap.ARS_PATH:
                            rows[0]["status"] = status
                        return rows

                    row, = cap.collect_site("jed", get)
                    self.assertEqual(row["slots"] - row["pending"], available)

    def test_stale_or_ambiguous_runner_sets_do_not_offer_capacity(self):
        for case in ("wrong owner", "deleting", "overlapping", "old generation"):
            with self.subTest(case=case):
                inventory = site_reader("jed", [160], limit=2)

                def get(path, filters):
                    rows = inventory(path, filters)
                    if path == cap.ERS_PATH:
                        if case == "wrong owner":
                            rows[0]["metadata"]["ownerReferences"][0]["uid"] = "old-uid"
                        if case == "deleting":
                            rows[0]["metadata"]["deletionTimestamp"] = "2026-11-20T00:00:00Z"
                        if case == "overlapping":
                            return rows * 2
                    if path == cap.ARS_PATH and case == "old generation":
                        rows[0]["metadata"]["generation"] = 2
                    return rows

                self.assertEqual(cap.collect_site("jed", get), [])

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
        with patch("builtins.print") as out:
            receipt = cap.snapshot({"mty": {"error": "test-sensitive connection information"}}, reader=fixture_reader)
        self.assertEqual(receipt["pools"], [])
        out.assert_called_once()
        self.assertNotIn("test-sensitive", str(out.call_args_list))
        self.assertEqual(standby.first_cs_pool_plan(json.dumps(receipt), receipt["captured_at"]), ())

    def test_stalled_site_keeps_healthy_offer_at_snapshot_deadline(self):
        inventory = {"mty": {"stall": True}, "jed": {"site": "jed", "free": [80]}}
        start = time.monotonic()
        with patch.object(cap, "SNAPSHOT_SECONDS", 3):
            receipt = cap.snapshot(inventory, reader=fixture_reader)
        self.assertLess(time.monotonic() - start, 12)
        self.assertEqual(receipt["pools"], [{"pool": "cs-jed-nc-n1-pr-v1", "slots": 1, "pending": 0}])

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
        self.assertIn("COVERAGE_CS_SITE_RETRY: ${{ inputs.enable_cs_site_retry }}", selector)
        self.assertIn("github.actor == 'github-actions[bot]'", worker)
        self.assertIn("fromJSON(inputs.ticket || '{}').attempt == 1", worker)
        self.assertNotIn("32922", worker)

    def test_only_authorized_first_attempts_on_supported_bases_can_read_capacity(self):
        workflows = Path(__file__).resolve().parents[1] / ".github/workflows"
        entry = (workflows / "coverage-ut-standby.yaml").read_text()
        before, capacity = entry.split("      - id: capacity\n", 1)
        capacity = capacity.split("\n  legacy:", 1)[0]
        self.assertIn("id: entry", before)
        self.assertIn("Retain the authorized PR head receipt", before)
        for gate in ("inputs.enable_cs_first_pool", "steps.entry.outputs.route == 'cache'",
                     "steps.entry.outputs.allowed == 'true'", "github.run_attempt == 1",
                     "github.repository == 'matrixorigin/matrixone'",
                     "github.event_name == 'pull_request_target'",
                     "entrypoint.yaml@refs/heads/main", "github.event.pull_request.base.ref == 'main'",
                     "inputs.enable_4_2_dev && github.event.pull_request.base.ref == '4.2-dev'",
                     "entrypoint.yaml@refs/heads/4.2-dev"):
            self.assertIn(gate, capacity)
        for unrelated_gate in ("32922", "pull_request.number", "pull_request.draft", "kind/test-ci"):
            self.assertNotIn(unrelated_gate, capacity)
        self.assertIn("CS_COVERAGE_CAPACITY_READ: ${{ secrets.CS_COVERAGE_CAPACITY_READ }}", capacity)


if __name__ == "__main__":
    unittest.main()
