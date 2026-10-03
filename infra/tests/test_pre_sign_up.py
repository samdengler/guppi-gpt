"""The Cognito pre sign-up trigger that makes sign-in invite only (docs/proposals/invites.md)."""

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "guppi_gpt_infra" / "lambdas" / "pre_sign_up" / "index.py"
_spec = importlib.util.spec_from_file_location("pre_sign_up", _PATH)
pre_sign_up = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pre_sign_up)


class FakeTable:
    def __init__(self, items):
        self.items = items
        self.reads = []

    def get_item(self, Key, ConsistentRead):  # noqa: N803 - boto3's names
        self.reads.append((Key, ConsistentRead))
        item = self.items.get(Key["email"])
        return {"Item": item} if item else {}


def event(email, source="PreSignUp_ExternalProvider"):
    return {
        "triggerSource": source,
        "request": {"userAttributes": {"email": email}},
        "response": {},
    }


@pytest.fixture
def table(monkeypatch):
    fake = FakeTable({"pat@example.com": {"email": "pat@example.com", "status": "approved"}})
    monkeypatch.setattr(pre_sign_up, "_table", lambda: fake)
    return fake


def test_an_approved_address_signs_up(table):
    incoming = event("Pat@Example.com ")
    assert pre_sign_up.handler(incoming, None) is incoming
    assert table.reads == [({"email": "pat@example.com"}, True)]


@pytest.mark.parametrize("status", ["pending", "revoked"])
def test_a_request_not_approved_is_refused(table, status):
    table.items["lee@example.com"] = {"email": "lee@example.com", "status": status}
    with pytest.raises(pre_sign_up.NotInvited):
        pre_sign_up.handler(event("lee@example.com"), None)


def test_an_address_with_no_request_is_refused(table):
    with pytest.raises(pre_sign_up.NotInvited):
        pre_sign_up.handler(event("stranger@example.com"), None)


def test_a_sign_up_without_an_email_is_refused(table):
    with pytest.raises(pre_sign_up.NotInvited):
        pre_sign_up.handler(event(""), None)


def test_every_way_into_the_pool_is_gated(table):
    for source in ("PreSignUp_ExternalProvider", "PreSignUp_AdminCreateUser", "PreSignUp_SignUp"):
        with pytest.raises(pre_sign_up.NotInvited):
            pre_sign_up.handler(event("stranger@example.com", source), None)


def test_the_refusal_names_a_marker_the_page_can_find(table):
    with pytest.raises(pre_sign_up.NotInvited) as refused:
        pre_sign_up.handler(event("stranger@example.com"), None)
    assert pre_sign_up.MARKER in str(refused.value)
    # The address stays out of the message, which Cognito puts in the redirect URL.
    assert "stranger" not in str(refused.value)
