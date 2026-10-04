"""The on-behalf-of token issuer (guppi-hr D20, D47, D51). The function is Rust; its own
tests (lambdas/obo_issuer/src/tests.rs: every client rule, the refusals, the verification
checks) run here through cargo, against the rules this stack deploys."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from guppi_gpt_infra.obo import ISSUER_DIR, RULES


def test_the_rust_tests_use_the_rules_the_stack_deploys():
    fixture = json.loads((ISSUER_DIR / "testdata" / "rules.json").read_text())
    assert fixture == RULES, "rewrite testdata/rules.json from guppi_gpt_infra.obo.RULES"


def test_the_built_in_okta_keys_are_public_rsa_keys():
    keys = json.loads((ISSUER_DIR / "okta-keys.json").read_text())["keys"]
    assert keys and all(set(k) == {"kid", "kty", "n", "e"} and k["kty"] == "RSA" for k in keys)


@pytest.mark.skipif(shutil.which("cargo") is None, reason="no Rust toolchain")
def test_the_issuers_rust_tests_pass():
    result = subprocess.run(["cargo", "test", "--lib", "--quiet"], cwd=ISSUER_DIR, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
