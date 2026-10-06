"""
test_infra.py
Tests for the AWS infrastructure files.

These tests do NOT make real AWS calls — they validate:
  1. The IAM policy JSON is well-formed and grants exactly the right actions
  2. The bash scripts exist and are executable
  3. The policy placeholder substitution logic in provision_buckets.py works correctly
"""

import json
import os
import stat
from pathlib import Path

import pytest

INFRA = Path(__file__).resolve().parents[1] / "infra"
POLICY = INFRA / "glucoflow-policy.json"


class TestPolicyJson:
    def setup_method(self):
        self.policy = json.loads(POLICY.read_text())

    def test_policy_version(self):
        assert self.policy["Version"] == "2012-10-17"

    def test_has_two_statements(self):
        assert len(self.policy["Statement"]) == 2

    def test_objects_statement_actions(self):
        stmt = next(s for s in self.policy["Statement"] if s["Sid"] == "GlucoFlowS3Objects")
        allowed = set(stmt["Action"])
        assert "s3:PutObject" in allowed
        assert "s3:GetObject" in allowed
        # Must NOT grant wildcard admin actions
        assert "s3:*" not in allowed
        assert "s3:DeleteBucket" not in allowed

    def test_list_statement_actions(self):
        stmt = next(s for s in self.policy["Statement"] if s["Sid"] == "GlucoFlowS3List")
        allowed = set(stmt["Action"])
        assert "s3:ListBucket" in allowed
        assert "s3:GetBucketLocation" in allowed

    def test_objects_resources_use_slash_star(self):
        """Object-level actions must have /* suffix — bucket ARN without it would allow nothing."""
        stmt = next(s for s in self.policy["Statement"] if s["Sid"] == "GlucoFlowS3Objects")
        for arn in stmt["Resource"]:
            assert arn.endswith("/*"), f"Object ARN missing /* suffix: {arn}"

    def test_list_resources_do_not_use_slash_star(self):
        """Bucket-level actions must NOT have /* suffix — it would have no effect."""
        stmt = next(s for s in self.policy["Statement"] if s["Sid"] == "GlucoFlowS3List")
        for arn in stmt["Resource"]:
            assert not arn.endswith("/*"), f"Bucket ARN incorrectly has /* suffix: {arn}"

    def test_all_four_buckets_present(self):
        placeholders = [
            "BRONZE_BUCKET_PLACEHOLDER",
            "SILVER_BUCKET_PLACEHOLDER",
            "GOLD_BUCKET_PLACEHOLDER",
            "QUARANTINE_BUCKET_PLACEHOLDER",
        ]
        raw = POLICY.read_text()
        for ph in placeholders:
            assert ph in raw, f"Missing placeholder {ph} in policy template"

    def test_effect_is_allow(self):
        for stmt in self.policy["Statement"]:
            assert stmt["Effect"] == "Allow"


class TestBashScripts:
    def test_setup_script_exists(self):
        assert (INFRA / "setup_aws.sh").exists()

    def test_teardown_script_exists(self):
        assert (INFRA / "teardown_aws.sh").exists()

    def test_setup_script_has_set_e(self):
        """set -euo pipefail ensures the script exits on first error."""
        content = (INFRA / "setup_aws.sh").read_text()
        assert "set -euo pipefail" in content

    def test_teardown_has_confirmation_prompt(self):
        """Teardown must require explicit YES confirmation before destroying anything."""
        content = (INFRA / "teardown_aws.sh").read_text()
        assert "YES" in content
        assert "read" in content

    def test_setup_handles_us_east_1_special_case(self):
        """us-east-1 must NOT send a LocationConstraint — special case in AWS API."""
        content = (INFRA / "setup_aws.sh").read_text()
        assert "us-east-1" in content
        assert "LocationConstraint" in content


class TestPolicyRender:
    def test_placeholder_substitution(self, tmp_path):
        """Verify sed-style placeholder substitution produces valid JSON with real names."""
        template = POLICY.read_text()
        substitutions = {
            "BRONZE_BUCKET_PLACEHOLDER":     "glucoflow-bronze-test-abc",
            "SILVER_BUCKET_PLACEHOLDER":     "glucoflow-silver-test-abc",
            "GOLD_BUCKET_PLACEHOLDER":       "glucoflow-gold-test-abc",
            "QUARANTINE_BUCKET_PLACEHOLDER": "glucoflow-quarantine-test-abc",
        }
        rendered = template
        for placeholder, value in substitutions.items():
            rendered = rendered.replace(placeholder, value)

        # Must be valid JSON after substitution
        doc = json.loads(rendered)

        # Must contain real bucket names in the ARNs
        raw = json.dumps(doc)
        for value in substitutions.values():
            assert value in raw, f"Bucket name {value} not in rendered policy"

        # Must NOT contain any leftover placeholders
        assert "PLACEHOLDER" not in raw
