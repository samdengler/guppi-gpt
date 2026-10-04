"""The on-behalf-of token issuer (guppi-hr D20, D47): every client rule, the refusals, the
verification checks. Keys are generated here; KMS and Secrets Manager are replaced."""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
import urllib.parse
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from guppi_gpt_infra.obo import CLIENTS, RULES_FOR_TESTS

_PATH = Path(__file__).resolve().parents[1] / "guppi_gpt_infra" / "lambdas" / "obo_issuer" / "index.py"
_spec = importlib.util.spec_from_file_location("obo_issuer", _PATH)
issuer = importlib.util.module_from_spec(_spec)
sys.modules["obo_issuer"] = issuer  # dataclasses look the module up
_spec.loader.exec_module(issuer)

OKTA = "https://example.okta.com/oauth2/aus1"
OWN = "https://obo.example.com"
CHAT_APP = "0oachat"
UID = "00u-employee"
NOW = 1_800_000_000


def jwk_of(private_key, kid: str) -> dict:
    numbers = private_key.public_key().public_numbers()
    n = numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")
    return {"kty": "RSA", "kid": kid, "alg": "RS256", "n": issuer.b64u(n), "e": issuer.b64u(numbers.e.to_bytes(3, "big"))}


def signed(private_key, header: dict, claims: dict) -> str:
    signing_input = f"{issuer.b64u(json.dumps(header).encode())}.{issuer.b64u(json.dumps(claims).encode())}"
    signature = private_key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{issuer.b64u(signature)}"


OKTA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OWN_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
SECRETS = {client: f"secret-{client}" for client in CLIENTS}


@pytest.fixture
def env():
    deps = issuer.Deps(
        issuer=OWN, kid="own-kid", public_jwk=jwk_of(OWN_KEY, "own-kid"),
        sign=lambda message: OWN_KEY.sign(message, padding.PKCS1v15(), hashes.SHA256()),
        client_secrets=dict(SECRETS),
        fetch_json=lambda url: {"keys": [jwk_of(OKTA_KEY, "okta-kid")]},
        now=lambda: NOW,
    )
    settings = issuer.Settings(okta_issuer=OKTA, okta_audience="api://guppi",
                               okta_clients=frozenset({CHAT_APP, "0oaharness"}), rules=RULES_FOR_TESTS)
    return deps, settings


def okta_token(**overrides) -> str:
    claims = {"iss": OKTA, "aud": "api://guppi", "cid": CHAT_APP, "uid": UID, "sub": "person@example.com",
              "scp": ["openid", "email"], "iat": NOW - 10, "exp": NOW + 3000, **overrides}
    return signed(OKTA_KEY, {"alg": "RS256", "kid": "okta-kid"}, claims)


def exchange(env, client: str, subject: str, scope: str = "", secret: str | None = None, **form) -> tuple[int, dict]:
    deps, settings = env
    body = urllib.parse.urlencode({"grant_type": issuer.TOKEN_EXCHANGE, "subject_token": subject,
                                   "subject_token_type": issuer.ACCESS_TOKEN_TYPE, "scope": scope, **form})
    basic = base64.b64encode(f"{client}:{secret if secret is not None else SECRETS.get(client, 'x')}".encode()).decode()
    event = {"requestContext": {"http": {"method": "POST"}}, "rawPath": "/token",
             "headers": {"authorization": f"Basic {basic}"}, "body": body}
    response = issuer.token(event, deps, settings)
    return response["statusCode"], json.loads(response["body"])


def claims_of(token: str) -> dict:
    return json.loads(issuer.unb64u(token.split(".")[1]))


CANVAS = "hr.tools.policy hr.tools.profile.read hr.tools.pay.statements.read"


def chain(env):
    """The Profile agents token, the canvas token and a Profile T2, as the hops get them."""
    _, t1 = exchange(env, "hr-bridge", okta_token(), "hr.agents.profile")
    _, t1p = exchange(env, "hr-bridge", okta_token(), CANVAS)
    _, t2 = exchange(env, "hr-agent-profile", t1["access_token"], "hr.tools.policy hr.tools.profile.read hr.tools.profile.write")
    return t1["access_token"], t1p["access_token"], t2["access_token"]


def test_the_bridge_gets_an_agents_token_naming_the_employee(env):
    status, body = exchange(env, "hr-bridge", okta_token(), "hr.agents.profile")
    assert status == 200 and body["issued_token_type"] == issuer.ACCESS_TOKEN_TYPE
    claims = claims_of(body["access_token"])
    assert claims["iss"] == OWN and claims["aud"] == "api://hr-agents/profile" and claims["scope"] == "hr.agents.profile"
    assert claims["sub"] == UID and claims["client_id"] == "hr-bridge" and claims["act"] == {"sub": "hr-bridge"}
    assert claims["exp"] == NOW + 3000 - 0 and "scp" not in claims
    header = json.loads(issuer.unb64u(body["access_token"].split(".")[0]))
    assert header == {"alg": "RS256", "typ": "at+jwt", "kid": "own-kid"}


def test_tokens_never_outlive_the_subject_or_an_hour(env):
    _, short = exchange(env, "hr-bridge", okta_token(exp=NOW + 600), "hr.agents.pay")
    _, long = exchange(env, "hr-bridge", okta_token(exp=NOW + 9000), "hr.agents.pay")
    assert claims_of(short["access_token"])["exp"] == NOW + 600
    assert claims_of(long["access_token"])["exp"] == NOW + 3600


def test_the_canvas_token_reads_but_cannot_write_or_see_bank_details(env):
    status, body = exchange(env, "hr-bridge", okta_token(), CANVAS)
    assert status == 200 and claims_of(body["access_token"])["aud"] == "api://hr-tools"
    assert exchange(env, "hr-bridge", okta_token(), "hr.tools.pay.write")[1] == {"error": "invalid_scope"}
    assert exchange(env, "hr-bridge", okta_token(), "hr.tools.pay.read")[1] == {"error": "invalid_scope"}


def test_scopes_for_two_audiences_are_refused(env):
    assert exchange(env, "hr-bridge", okta_token(), "hr.agents.pay hr.tools.policy")[1] == {"error": "invalid_scope"}
    assert exchange(env, "hr-bridge", okta_token(), "hr.agents.pay hr.agents.travel")[1] == {"error": "invalid_scope"}


def agents_token(env, domain: str) -> str:
    return exchange(env, "hr-bridge", okta_token(), f"hr.agents.{domain}")[1]["access_token"]


def test_each_sub_agent_gets_only_its_domain(env):
    status, body = exchange(env, "hr-agent-pay", agents_token(env, "pay"),
                            "hr.tools.policy hr.tools.pay.read hr.tools.pay.write")
    assert status == 200
    claims = claims_of(body["access_token"])
    assert claims["act"] == {"sub": "hr-agent-pay", "act": {"sub": "hr-bridge"}} and claims["sub"] == UID
    assert exchange(env, "hr-agent-travel", agents_token(env, "travel"), "hr.tools.pay.read")[1] == {"error": "invalid_scope"}
    assert exchange(env, "hr-agent-profile", agents_token(env, "profile"), "hr.tools.pay.read")[1] == {"error": "invalid_scope"}
    assert exchange(env, "hr-agent-travel", agents_token(env, "travel"), "hr.tools.policy")[0] == 200


def test_an_agents_token_serves_only_its_own_sub_agent(env):
    # A Travel runtime that holds the Travel agents token cannot become the Pay agent.
    travel = agents_token(env, "travel")
    assert exchange(env, "hr-agent-pay", travel, "hr.tools.policy")[1] == {"error": "invalid_grant"}
    assert exchange(env, "hr-agent-profile", travel, "hr.tools.policy")[1] == {"error": "invalid_grant"}


def test_sub_agents_accept_only_the_bridges_agents_token(env):
    t1, t1p, t2 = chain(env)
    assert exchange(env, "hr-agent-profile", okta_token(), "hr.tools.policy")[1] == {"error": "invalid_grant"}
    assert exchange(env, "hr-agent-profile", t1p, "hr.tools.policy")[1] == {"error": "invalid_grant"}
    assert exchange(env, "hr-agent-profile", t2, "hr.tools.policy")[1] == {"error": "invalid_grant"}


def test_the_bridge_accepts_only_okta_tokens(env):
    t1, t1p, _ = chain(env)
    assert exchange(env, "hr-bridge", t1p, "hr.agents.profile")[1] == {"error": "invalid_grant"}
    assert exchange(env, "hr-bridge", t1, "hr.agents.profile")[1] == {"error": "invalid_grant"}


def test_the_tools_gateway_carries_the_callers_scopes_to_the_runtime(env):
    _, t1p, t2 = chain(env)
    status, body = exchange(env, "hr-tools-gateway", t2, "hr.tools.policy")
    assert status == 200
    claims = claims_of(body["access_token"])
    assert claims["aud"] == "api://hr-tools-runtime" and claims["sub"] == UID
    assert claims["scope"] == "hr.tools.policy hr.tools.profile.read hr.tools.profile.write"
    assert claims["act"] == {"sub": "hr-tools-gateway", "act": {"sub": "hr-agent-profile", "act": {"sub": "hr-bridge"}}}
    _, body = exchange(env, "hr-tools-gateway", t1p, "hr.tools.policy")
    assert claims_of(body["access_token"])["scope"] == CANVAS


def test_the_tools_gateway_refuses_tokens_not_meant_for_the_tools_gateway(env):
    t1, _, t2 = chain(env)
    _, t3 = exchange(env, "hr-tools-gateway", t2, "hr.tools.policy")
    assert exchange(env, "hr-tools-gateway", t1, "hr.tools.policy")[1] == {"error": "invalid_grant"}
    assert exchange(env, "hr-tools-gateway", okta_token(), "hr.tools.policy")[1] == {"error": "invalid_grant"}
    assert exchange(env, "hr-tools-gateway", t3["access_token"], "hr.tools.policy")[1] == {"error": "invalid_grant"}


def test_client_authentication(env):
    assert exchange(env, "hr-bridge", okta_token(), "hr.agents.profile", secret="wrong") == (401, {"error": "invalid_client"})
    assert exchange(env, "nobody", okta_token(), "hr.agents.profile") == (401, {"error": "invalid_client"})
    deps, settings = env
    event = {"requestContext": {"http": {"method": "POST"}}, "rawPath": "/token",
             "headers": {"authorization": "Basic %%%not-base64"}, "body": ""}
    assert issuer.token(event, deps, settings)["statusCode"] == 401


@pytest.mark.parametrize("overrides,expected", [
    ({"aud": "api://other"}, "invalid_grant"),
    ({"cid": "0oaother"}, "invalid_grant"),
    ({"uid": None}, "invalid_grant"),
    ({"exp": NOW - 120}, "invalid_grant"),
    ({"iat": NOW + 600}, "invalid_grant"),
    ({"nbf": NOW + 600}, "invalid_grant"),
    ({"iss": "https://elsewhere"}, "invalid_grant"),
])
def test_okta_token_checks(env, overrides, expected):
    claims = {k: v for k, v in overrides.items() if v is not None}
    token = okta_token(**claims)
    if overrides.get("uid", 1) is None:
        token = signed(OKTA_KEY, {"alg": "RS256", "kid": "okta-kid"},
                       {"iss": OKTA, "aud": "api://guppi", "cid": CHAT_APP, "iat": NOW, "exp": NOW + 600})
    assert exchange(env, "hr-bridge", token, "hr.agents.profile")[1] == {"error": expected}


def test_signature_and_header_checks(env):
    good = okta_token()
    head, body, sig = good.split(".")
    tampered = f"{head}.{issuer.b64u(json.dumps({**claims_of(good), 'uid': 'someone-else'}).encode())}.{sig}"
    short_sig = f"{head}.{body}.{issuer.b64u(issuer.unb64u(sig)[1:])}"
    none_alg = f"{issuer.b64u(json.dumps({'alg': 'none', 'kid': 'okta-kid'}).encode())}.{body}."
    hs256 = f"{issuer.b64u(json.dumps({'alg': 'HS256', 'kid': 'okta-kid'}).encode())}.{body}.{sig}"
    for token in (tampered, short_sig, none_alg, hs256, "not-a-token", ""):
        assert exchange(env, "hr-bridge", token, "hr.agents.profile")[1] == {"error": "invalid_grant"}
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = signed(other_key, {"alg": "RS256", "kid": "okta-kid"}, claims_of(good))
    assert exchange(env, "hr-bridge", forged, "hr.agents.profile")[1] == {"error": "invalid_grant"}


def test_own_tokens_need_typ_and_kid(env):
    t1, _, _ = chain(env)
    _, body, sig = t1.split(".")
    no_typ = signed(OWN_KEY, {"alg": "RS256", "kid": "own-kid"}, claims_of(t1))
    other_kid = signed(OWN_KEY, {"alg": "RS256", "typ": "at+jwt", "kid": "old"}, claims_of(t1))
    for token in (no_typ, other_kid):
        assert exchange(env, "hr-agent-profile", token, "hr.tools.policy")[1] == {"error": "invalid_grant"}


def test_other_grants_and_actor_tokens_are_refused(env):
    assert exchange(env, "hr-bridge", okta_token(), "hr.agents.profile", grant_type="client_credentials")[1] == \
        {"error": "unsupported_grant_type"}
    assert exchange(env, "hr-bridge", okta_token(), "hr.agents.profile", actor_token="x")[1] == {"error": "invalid_request"}


def test_unknown_okta_keys_are_refetched_at_most_once_a_minute(env):
    deps, settings = env
    calls, clock = [], [NOW]
    deps.now = lambda: clock[0]
    deps.fetch_json = lambda url: calls.append(url) or {"keys": [jwk_of(OKTA_KEY, "okta-kid")]}
    assert exchange(env, "hr-bridge", okta_token(), "hr.agents.profile")[0] == 200
    rotated = signed(OKTA_KEY, {"alg": "RS256", "kid": "rotated"}, claims_of(okta_token()))
    for _ in range(5):
        exchange(env, "hr-bridge", rotated, "hr.agents.profile")
    assert len(calls) == 1  # within the minute of the first fill, an unknown kid waits
    clock[0] += 61
    for _ in range(5):
        exchange(env, "hr-bridge", rotated, "hr.agents.profile")
    assert len(calls) == 2  # then one refetch, and no more for a minute


def test_errors_carry_no_reason_and_never_500(env):
    deps, settings = env
    event = {"requestContext": {"http": {"method": "POST"}}, "rawPath": "/token", "headers": {},
             "body": "%%%", "isBase64Encoded": True}
    response = issuer.token(event, deps, settings)
    assert response["statusCode"] in (400, 401) and set(json.loads(response["body"])) == {"error"}


def test_der_parse_matches_the_key():
    der = OWN_KEY.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    n, e = issuer.rsa_public_numbers(der)
    numbers = OWN_KEY.public_key().public_numbers()
    assert int.from_bytes(n, "big") == numbers.n and int.from_bytes(e, "big") == numbers.e


def test_discovery_lists_the_scopes(env):
    deps, settings = env
    doc = issuer.discovery(deps, settings)
    assert doc["issuer"] == OWN and doc["token_endpoint"] == f"{OWN}/token"
    assert "hr.agents.pay" in doc["scopes_supported"] and doc["grant_types_supported"] == [issuer.TOKEN_EXCHANGE]



def test_a_subject_about_to_expire_is_refused(env):
    assert exchange(env, "hr-bridge", okta_token(exp=NOW + 30), "hr.agents.pay")[1] == {"error": "invalid_grant"}
    assert exchange(env, "hr-bridge", okta_token(exp=NOW + 120), "hr.agents.pay")[0] == 200


def test_client_secret_post_and_url_encoded_basic(env):
    deps, settings = env
    deps.client_secrets["hr-bridge"] = "a b:c"
    body = urllib.parse.urlencode({"grant_type": issuer.TOKEN_EXCHANGE, "subject_token": okta_token(),
                                   "scope": "hr.agents.pay", "client_id": "hr-bridge", "client_secret": "a b:c"})
    event = {"requestContext": {}, "httpMethod": "POST", "path": "/token", "headers": {}, "body": body}
    assert issuer.token(event, deps, settings)["statusCode"] == 200
    encoded = base64.b64encode(f"hr-bridge:{urllib.parse.quote('a b:c')}".encode()).decode()
    body = urllib.parse.urlencode({"grant_type": issuer.TOKEN_EXCHANGE, "subject_token": okta_token(), "scope": "hr.agents.pay"})
    event = {"httpMethod": "POST", "path": "/token", "headers": {"Authorization": f"Basic {encoded}"}, "body": body}
    assert issuer.token(event, deps, settings)["statusCode"] == 200


def test_an_unknown_subject_token_type_is_refused(env):
    status, body = exchange(env, "hr-bridge", okta_token(), "hr.agents.pay",
                            subject_token_type="urn:ietf:params:oauth:token-type:id_token")
    assert (status, body) == (400, {"error": "invalid_request"})


def test_cached_okta_keys_survive_an_okta_outage(env):
    deps, settings = env
    assert exchange(env, "hr-bridge", okta_token(), "hr.agents.pay")[0] == 200
    clock = [NOW + 2 * issuer.JWKS_TTL]
    deps.now = lambda: clock[0]

    def down(url):
        raise OSError("okta unreachable")

    deps.fetch_json = down
    token = okta_token(iat=clock[0] - 10, exp=clock[0] + 600)
    assert exchange(env, "hr-bridge", token, "hr.agents.pay")[0] == 200


def test_rest_api_events_and_cacheable_reads(env):
    deps, settings = env
    issuer._deps, issuer._settings = deps, settings
    try:
        for path in ("/jwks.json", "/.well-known/openid-configuration"):
            response = issuer.handler({"httpMethod": "GET", "path": path, "headers": {}}, None)
            assert response["statusCode"] == 200 and response["headers"]["cache-control"] == "public, max-age=300"
        assert issuer.handler({"httpMethod": "GET", "path": "/token", "headers": {}}, None)["statusCode"] == 404
    finally:
        issuer._deps = issuer._settings = None


def test_the_log_names_tokens_by_jti_never_the_employee(env, capsys):
    exchange(env, "hr-bridge", okta_token(jti="okta-jti-1"), "hr.agents.pay")
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["subject_jti"] == "okta-jti-1" and len(line["jti"]) == 32
    assert UID not in json.dumps(line) and "person@example.com" not in json.dumps(line)
