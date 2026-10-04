"""The on-behalf-of token issuer (guppi-hr D20, D47; docs in guppi-hr
docs/proposals/obo-token-exchange.md).

An RFC 8693 token exchange endpoint that stands in for the production identity provider.
Okta signs people in (D46) but its free plan cannot exchange tokens, so this function
trades a token for the next hop's, still naming the employee, and records who acted in a
nested `act` claim. AgentCore Identity calls it through on-behalf-of credential providers.

Routes: GET /.well-known/openid-configuration, GET /jwks.json, POST /token.

Every client has a rule (the RULES environment value, built by the stack): which subject
tokens it may present (issuer, audience, acting client, depth of the `act` chain) and
which scopes it may receive for which audience. A client that inherits scopes receives the
subject token's own scopes for its one audience, whatever it requests; the tools gateway
uses this, since its target requests fixed scopes.

Tokens are verified with the standard library alone (RS256 by modular exponentiation and a
constant-time comparison of the PKCS#1 v1.5 encoding), so the function has no
dependencies to bundle. KMS signs; the private key never leaves it. Client secrets come
from Secrets Manager at cold start. Nothing logged names a person or holds a token.

Timings (guppi-gpt README backlog item 9): a cold start logs one `cold_start` line with each
step's start and end in milliseconds from the start of the load, so the log shows what runs
in sequence and what in parallel; every /token line carries `ms` (load, verify, mint) and
`cold`. X-Ray traces the function and the API stage for the console's timeline.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"
SUBJECT_TOKEN_TYPES = {ACCESS_TOKEN_TYPE, "urn:ietf:params:oauth:token-type:jwt"}
LIFETIME = 3600
LEEWAY = 60
# A subject token this close to expiry is refused rather than traded for one that would
# arrive already expired.
MIN_REMAINING = 60
JWKS_TTL = 3600
JWKS_REFETCH_MIN_INTERVAL = 60
MAX_TOKEN_LENGTH = 8192
SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


class Refused(Exception):
    """A request the endpoint refuses, with the OAuth error code and an internal reason."""

    def __init__(self, status: int, error: str, reason: str, claimed: str = "") -> None:
        super().__init__(reason)
        self.status, self.error, self.reason, self.claimed = status, error, reason, claimed


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def parse_der(data: bytes, i: int) -> tuple[int, int, int]:
    """(tag, content start, content end) of the DER element at i."""
    tag, length = data[i], data[i + 1]
    i += 2
    if length & 0x80:
        count = length & 0x7F
        length = int.from_bytes(data[i : i + count], "big")
        i += count
    return tag, i, i + length


def rsa_public_numbers(spki_der: bytes) -> tuple[bytes, bytes]:
    """(n, e) from a DER SubjectPublicKeyInfo holding an RSA key, as KMS returns it."""
    _, s, _ = parse_der(spki_der, 0)
    _, _, alg_end = parse_der(spki_der, s)
    _, bits, _ = parse_der(spki_der, alg_end)
    _, r, _ = parse_der(spki_der, bits + 1)  # skip the BIT STRING's unused-bits byte
    _, n_s, n_e = parse_der(spki_der, r)
    _, e_s, e_e = parse_der(spki_der, n_e)
    return spki_der[n_s:n_e].lstrip(b"\x00"), spki_der[e_s:e_e]


def rs256_valid(jwk: dict, signing_input: bytes, signature: bytes) -> bool:
    """RSASSA-PKCS1-v1_5 with SHA-256 (RFC 8017 8.2.2), by re-encoding and comparing."""
    try:
        n = int.from_bytes(unb64u(jwk["n"]), "big")
        e = int.from_bytes(unb64u(jwk["e"]), "big")
    except (KeyError, ValueError, binascii.Error):
        return False
    k = (n.bit_length() + 7) // 8
    if n.bit_length() < 2048 or len(signature) != k:
        return False
    s = int.from_bytes(signature, "big")
    if s >= n:
        return False
    em = pow(s, e, n).to_bytes(k, "big")
    t = SHA256_DIGEST_INFO + hashlib.sha256(signing_input).digest()
    expected = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return hmac.compare_digest(em, expected)


def act_depth(claims: dict) -> int:
    depth, act = 0, claims.get("act")
    while isinstance(act, dict):
        depth += 1
        act = act.get("act")
    return depth


@dataclass
class Deps:
    """What the function reaches outside itself; the tests replace it."""

    issuer: str
    kid: str
    public_jwk: dict
    sign: Callable[[bytes], bytes]
    client_secrets: dict[str, str]
    fetch_json: Callable[[str], dict]
    now: Callable[[], float] = time.time
    okta_keys: dict[str, dict] = field(default_factory=dict)
    okta_keys_at: float = 0.0
    okta_refetch_at: float = 0.0


@dataclass
class Settings:
    okta_issuer: str
    okta_audience: str
    okta_clients: frozenset[str]
    rules: dict[str, dict]

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            okta_issuer=os.environ["OKTA_ISSUER"],
            okta_audience=os.environ["OKTA_AUDIENCE"],
            okta_clients=frozenset(c for c in os.environ["OKTA_CLIENTS"].split(",") if c),
            rules=json.loads(os.environ["RULES"]),
        )


_deps: Deps | None = None
_settings: Settings | None = None
_timing: dict[str, Any] = {}  # this request's timings, for its log line


def _ms(seconds: float) -> int:
    return round(seconds * 1000)


def _load() -> tuple[Deps, Settings]:
    """Cold start: the issuer URL, the public key and the client secrets, in parallel."""
    global _deps, _settings
    if _deps is None:
        t0 = time.perf_counter()
        steps: dict[str, list[int]] = {}

        def timed(name: str, fn: Callable[[], Any]) -> Callable[[], Any]:
            def run() -> Any:
                start = time.perf_counter()
                try:
                    return fn()
                finally:
                    steps[name] = [_ms(start - t0), _ms(time.perf_counter() - t0)]
            return run

        boto3 = timed("import_boto3", lambda: __import__("boto3"))()  # only in Lambda; the tests set _deps
        kms, ssm, secrets = timed("clients", lambda: (boto3.client("kms"), boto3.client("ssm"),
                                                       boto3.client("secretsmanager")))()
        key_id = os.environ["KEY_ID"]
        arns: dict[str, str] = json.loads(os.environ["CLIENT_SECRET_ARNS"])

        def secret(arn: str) -> str:
            return json.loads(secrets.get_secret_value(SecretId=arn)["SecretString"])["client_secret"]

        with ThreadPoolExecutor(max_workers=8) as pool:
            issuer_f = pool.submit(timed("ssm_issuer", lambda: ssm.get_parameter(
                Name=os.environ["ISSUER_PARAMETER"])["Parameter"]["Value"]))
            key_f = pool.submit(timed("kms_public_key", lambda: kms.get_public_key(KeyId=key_id)["PublicKey"]))
            secret_fs = {client: pool.submit(timed(f"secret_{client}", lambda arn=arn: secret(arn)))
                         for client, arn in arns.items()}
            settings = Settings.from_env()
            okta_f = pool.submit(timed("okta_keys", lambda: fetch_json(f"{settings.okta_issuer}/v1/keys")))
        steps["parallel_calls"] = [min(s[0] for k, s in steps.items() if k not in ("import_boto3", "clients")),
                                   _ms(time.perf_counter() - t0)]
        n, e = rsa_public_numbers(key_f.result())
        kid = hashlib.sha256(key_id.encode()).hexdigest()[:16]
        _deps = Deps(
            issuer=issuer_f.result().rstrip("/"),
            kid=kid,
            public_jwk={"kty": "RSA", "use": "sig", "alg": "RS256", "kid": kid, "n": b64u(n), "e": b64u(e)},
            sign=lambda message: kms.sign(KeyId=key_id, Message=message, MessageType="RAW",
                                          SigningAlgorithm="RSASSA_PKCS1_V1_5_SHA_256")["Signature"],
            client_secrets={client: f.result() for client, f in secret_fs.items()},
            fetch_json=fetch_json,
        )
        try:
            _deps.okta_keys = {k["kid"]: k for k in okta_f.result()["keys"]}
            _deps.okta_keys_at = _deps.now()
        except Exception:  # noqa: BLE001 - fetched again on first use
            pass
        _settings = settings
        log(event="cold_start", load_ms=_ms(time.perf_counter() - t0), steps=steps)
    return _deps, _settings  # type: ignore[return-value]


def fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=3) as response:  # noqa: S310 - fixed https URLs
        return json.load(response)


def okta_key(deps: Deps, settings: Settings, kid: str) -> dict | None:
    now = deps.now()
    stale = now - deps.okta_keys_at > JWKS_TTL
    unknown = kid not in deps.okta_keys
    if (stale or unknown) and now - deps.okta_refetch_at >= JWKS_REFETCH_MIN_INTERVAL:
        deps.okta_refetch_at = now
        try:
            deps.okta_keys = {k["kid"]: k for k in deps.fetch_json(f"{settings.okta_issuer}/v1/keys")["keys"]}
            deps.okta_keys_at = now
        except Exception:  # noqa: BLE001 - keep the cached keys
            pass
    return deps.okta_keys.get(kid)


def verify(token: str, deps: Deps, settings: Settings) -> tuple[dict, str]:
    """The subject token's claims and its source, "okta" or "self"; Refused otherwise."""
    if not token or len(token) > MAX_TOKEN_LENGTH or token.count(".") != 2:
        raise Refused(400, "invalid_grant", "malformed subject token")
    head, body, sig = token.split(".")
    try:
        header, claims, signature = json.loads(unb64u(head)), json.loads(unb64u(body)), unb64u(sig)
    except (ValueError, binascii.Error):
        raise Refused(400, "invalid_grant", "undecodable subject token") from None
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise Refused(400, "invalid_grant", "malformed subject token")
    if header.get("alg") != "RS256" or "crit" in header:
        raise Refused(400, "invalid_grant", "unsupported token header")
    issuer = claims.get("iss")
    if issuer == settings.okta_issuer:
        source, key = "okta", okta_key(deps, settings, str(header.get("kid", "")))
    elif issuer == deps.issuer:
        if header.get("typ") != "at+jwt":
            raise Refused(400, "invalid_grant", "own token without typ at+jwt")
        source, key = "self", deps.public_jwk if header.get("kid") == deps.kid else None
    else:
        raise Refused(400, "invalid_grant", "untrusted issuer")
    if not key or not rs256_valid(key, f"{head}.{body}".encode(), signature):
        raise Refused(400, "invalid_grant", "bad signature")
    now = deps.now()
    for name in ("exp", "iat"):
        if not isinstance(claims.get(name), (int, float)):
            raise Refused(400, "invalid_grant", f"missing {name}")
    if claims["exp"] <= now - LEEWAY:
        raise Refused(400, "invalid_grant", "expired")
    if claims["iat"] > now + LEEWAY or claims.get("nbf", 0) > now + LEEWAY:
        raise Refused(400, "invalid_grant", "not yet valid")
    if claims["exp"] - now < MIN_REMAINING:
        raise Refused(400, "invalid_grant", "expires too soon")
    if source == "okta":
        if claims.get("aud") != settings.okta_audience:
            raise Refused(400, "invalid_grant", "okta audience")
        if claims.get("cid") not in settings.okta_clients:
            raise Refused(400, "invalid_grant", "okta client")
        if not isinstance(claims.get("uid"), str) or not claims["uid"]:
            raise Refused(400, "invalid_grant", "okta token without uid")
    return claims, source


def header(event: dict, name: str) -> str:
    """A request header by name, whatever its case (REST API events keep the caller's)."""
    for key, value in (event.get("headers") or {}).items():
        if key.lower() == name:
            return value or ""
    return ""


def client_of(event: dict, form: dict, deps: Deps) -> str:
    authorization = header(event, "authorization")
    try:
        if authorization[:6].lower() == "basic ":
            raw = base64.b64decode(authorization[6:], validate=True).decode()
            client_id, _, secret = raw.partition(":")
            client_id, secret = urllib.parse.unquote(client_id), urllib.parse.unquote(secret)
        else:
            client_id, secret = form.get("client_id", ""), form.get("client_secret", "")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise Refused(401, "invalid_client", "malformed client authentication") from None
    expected = deps.client_secrets.get(client_id)
    # Compared even for an unknown client, so the time does not say which clients exist.
    ok = hmac.compare_digest(hashlib.sha256(secret.encode()).digest(),
                             hashlib.sha256((expected or "\x00unknown").encode()).digest())
    if not expected or not ok:
        # The client id the caller claimed, cut short, so a probe can be told from a test.
        raise Refused(401, "invalid_client", "client authentication failed", claimed=client_id[:40])
    return client_id


def subject_allowed(rule: dict, claims: dict, source: str) -> None:
    allowed = rule["subject"]
    if source != allowed["issuer"]:
        raise Refused(400, "invalid_grant", "subject issuer not allowed for client")
    if source == "self":
        if claims.get("aud") not in allowed["audiences"]:
            raise Refused(400, "invalid_grant", "subject audience not allowed for client")
        if claims.get("client_id") not in allowed["clients"]:
            raise Refused(400, "invalid_grant", "subject client not allowed for client")
    if act_depth(claims) not in allowed["act_depths"]:
        raise Refused(400, "invalid_grant", "subject act depth not allowed for client")


def grant_for(rule: dict, requested: list[str], claims: dict) -> tuple[str, list[str]]:
    """(audience, scopes) the client receives."""
    if rule.get("inherit_scopes"):
        grant = rule["grants"][0]
        held = str(claims.get("scope", "")).split()
        scopes = [s for s in held if s in grant["scopes"]]
        if not scopes:
            raise Refused(400, "invalid_scope", "subject holds no inheritable scope")
        return grant["audience"], scopes
    if not requested:
        raise Refused(400, "invalid_scope", "no scope requested")
    for grant in rule["grants"]:
        if all(s in grant["scopes"] for s in requested):
            return grant["audience"], requested
    raise Refused(400, "invalid_scope", "scope not allowed for client")


def mint(deps: Deps, client_id: str, claims: dict, source: str, audience: str, scopes: list[str]) -> tuple[str, int, str]:
    now = int(deps.now())
    act: dict[str, Any] = {"sub": client_id}
    if isinstance(claims.get("act"), dict):
        act["act"] = claims["act"]
    payload = {
        "iss": deps.issuer,
        "sub": str(claims["uid"] if source == "okta" else claims["sub"]),
        "aud": audience,
        "scope": " ".join(scopes),
        "client_id": client_id,
        "act": act,
        "iat": now,
        "nbf": now,
        "exp": min(int(claims["exp"]), now + LIFETIME),
        "jti": uuid.uuid4().hex,
    }
    header = {"alg": "RS256", "typ": "at+jwt", "kid": deps.kid}
    signing_input = (f"{b64u(json.dumps(header, separators=(',', ':')).encode())}."
                     f"{b64u(json.dumps(payload, separators=(',', ':')).encode())}")
    return f"{signing_input}.{b64u(deps.sign(signing_input.encode()))}", payload["exp"] - now, payload["jti"]


def respond(status: int, body: dict, cacheable: bool = False) -> dict:
    # Tokens are never cached; the discovery document and the key may be, for five minutes.
    cache = {"cache-control": "public, max-age=300"} if cacheable else {"cache-control": "no-store", "pragma": "no-cache"}
    return {"statusCode": status, "headers": {"content-type": "application/json", **cache}, "body": json.dumps(body)}


def log(**fields: Any) -> None:
    print(json.dumps(fields, separators=(",", ":")))


def subject_fields(claims: dict | None) -> dict:
    """What a refusal log line may say about the subject token: never who it names."""
    if not claims:
        return {}
    return {"subject_jti": str(claims.get("jti", "")), "subject_aud": claims.get("aud"),
            "subject_client": claims.get("client_id") or claims.get("cid"), "subject_depth": act_depth(claims)}


def token(event: dict, deps: Deps, settings: Settings) -> dict:
    client_id = "-"
    claims: dict | None = None
    try:
        raw = event.get("body") or ""
        if event.get("isBase64Encoded"):
            raw = base64.b64decode(raw).decode()
        form = {k: v[0] for k, v in urllib.parse.parse_qs(raw, max_num_fields=20).items()}
        client_id = client_of(event, form, deps)
        if form.get("grant_type") != TOKEN_EXCHANGE:
            raise Refused(400, "unsupported_grant_type", "grant type")
        if form.get("subject_token_type", ACCESS_TOKEN_TYPE) not in SUBJECT_TOKEN_TYPES:
            raise Refused(400, "invalid_request", "subject token type")
        if "actor_token" in form:
            raise Refused(400, "invalid_request", "actor tokens are not accepted")
        rule = settings.rules.get(client_id)
        if not rule:
            raise Refused(400, "unauthorized_client", "client has no rule")
        started = time.perf_counter()
        claims, source = verify(form.get("subject_token", ""), deps, settings)
        verified = time.perf_counter()
        subject_allowed(rule, claims, source)
        audience, scopes = grant_for(rule, form.get("scope", "").split(), claims)
        access_token, expires_in, jti = mint(deps, client_id, claims, source, audience, scopes)
        # mint is the KMS Sign call, all but a fraction of a millisecond.
        _timing.setdefault("ms", {}).update(verify=_ms(verified - started), mint=_ms(time.perf_counter() - verified))
    except Refused as refused:
        log(route="/token", client=client_id, claimed_client=refused.claimed or None, status=refused.status,
            error=refused.error, reason=refused.reason, **subject_fields(claims), **_timing)
        return respond(refused.status, {"error": refused.error})
    except Exception as error:  # noqa: BLE001 - never a 500 with a reason in it
        log(route="/token", client=client_id, status=400, error="invalid_request", reason=type(error).__name__)
        return respond(400, {"error": "invalid_request"})
    # The new token's jti and the subject's, never who it names: an incident can follow a
    # chain of exchanges back to the Okta token, whose own jti Okta's system log has.
    log(route="/token", client=client_id, status=200, audience=audience, scope=" ".join(scopes),
        depth=act_depth(claims) + 1, jti=jti, subject_jti=str(claims.get("jti", "")), **_timing)
    return respond(200, {"access_token": access_token, "issued_token_type": ACCESS_TOKEN_TYPE,
                         "token_type": "Bearer", "expires_in": expires_in, "scope": " ".join(scopes)})


def discovery(deps: Deps, settings: Settings) -> dict:
    scopes = sorted({s for rule in settings.rules.values() for g in rule["grants"] for s in g["scopes"]})
    return {
        "issuer": deps.issuer,
        "token_endpoint": f"{deps.issuer}/token",
        "jwks_uri": f"{deps.issuer}/jwks.json",
        # AgentCore reads the document as OpenID Connect discovery, which names an
        # authorization endpoint; this issuer has none, and the path answers 404.
        "authorization_endpoint": f"{deps.issuer}/authorize",
        "grant_types_supported": [TOKEN_EXCHANGE],
        "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
        "response_types_supported": ["token"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "scopes_supported": scopes,
    }


def handler(event: dict, _context: Any) -> dict:
    cold, started = _deps is None, time.perf_counter()
    deps, settings = _load()
    _timing.clear()
    _timing.update(cold=cold, ms={"load": _ms(time.perf_counter() - started)})
    # REST API events (httpMethod, path) since D48; HTTP API events too.
    method = event.get("httpMethod") or event.get("requestContext", {}).get("http", {}).get("method", "")
    path = event.get("path") or event.get("rawPath", "")
    if method == "GET" and path == "/.well-known/openid-configuration":
        return respond(200, discovery(deps, settings), cacheable=True)
    if method == "GET" and path == "/jwks.json":
        return respond(200, {"keys": [deps.public_jwk]}, cacheable=True)
    if method == "POST" and path == "/token":
        return token(event, deps, settings)
    return respond(404, {"error": "not_found"})
