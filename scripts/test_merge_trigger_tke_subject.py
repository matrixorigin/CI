#!/usr/bin/env python3

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/merge-trigger-tke.yaml"


def validate_subject_contract(source: str) -> None:
    merge_sha = "${{ github.event.pull_request.merge_commit_sha }}"
    verified_sha = "${{ needs.docker_image_build.outputs.full_commit_id }}"

    if "full_commit_id: ${{ steps.subject.outputs.commit_id }}" not in source:
        raise AssertionError("build job does not publish its verified full commit")
    if f"ref: {merge_sha}" not in source:
        raise AssertionError("image build checkout is not bound to merge_commit_sha")
    if "Verify exact merged MatrixOne subject" not in source:
        raise AssertionError("exact checkout has no fail-closed identity check")
    if '[ "$actual" = "$expected" ]' not in source:
        raise AssertionError("identity check does not compare actual and requested SHA")
    if 'rev-parse --short HEAD' not in source or 'short_commit_id=$short' not in source:
        raise AssertionError("image tag does not retain Git's unique short-SHA contract")

    # Setup and BVT each consume MatrixOne source after the image build. They
    # must use the SHA already verified by the producer, not resolve a moving
    # branch independently.
    if source.count("path: ./matrixone") != 3:
        raise AssertionError("unexpected MatrixOne checkout inventory")
    if source.count(f"ref: {verified_sha}") != 2:
        raise AssertionError("Setup and BVT are not both bound to the built SHA")

    # Both CN resource checkouts use an explicit synthetic local ref sourced
    # from the same verified SHA.
    fetch_contract = (
        f"+{verified_sha}:refs/remotes/origin/merged-subject"
    )
    if source.count(fetch_contract) != 2:
        raise AssertionError("CN resource fetches are not both bound to the built SHA")
    if source.count(f'== "{verified_sha}"') != 2:
        raise AssertionError("CN resource checkouts do not verify their resulting HEAD")

    # pull_request_target resolves these contexts from the default branch.
    # Their reintroduction would silently restore the release-branch bug.
    for forbidden in ("${{ github.sha }}", "${{ github.ref_name }}"):
        if forbidden in source:
            raise AssertionError(f"moving/default subject context is forbidden: {forbidden}")


class MergeTriggerTkeSubjectContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.source = WORKFLOW.read_text(encoding="utf-8")

    def test_current_workflow_closes_exact_subject_identity(self) -> None:
        validate_subject_contract(self.source)

    def test_missing_build_ref_is_rejected(self) -> None:
        broken = self.source.replace(
            "          ref: ${{ github.event.pull_request.merge_commit_sha }}\n",
            "",
            1,
        )
        with self.assertRaisesRegex(AssertionError, "merge_commit_sha"):
            validate_subject_contract(broken)

    def test_default_branch_context_is_rejected(self) -> None:
        broken = self.source.replace(
            "${{ needs.docker_image_build.outputs.full_commit_id }}",
            "${{ github.sha }}",
            1,
        )
        with self.assertRaisesRegex(AssertionError, "built SHA"):
            validate_subject_contract(broken)


if __name__ == "__main__":
    unittest.main()
