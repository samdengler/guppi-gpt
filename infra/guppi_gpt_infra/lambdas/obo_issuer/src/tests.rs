//! The on-behalf-of token issuer (guppi-hr D20, D47): every client rule, the refusals, the
//! verification checks. Keys are generated here; KMS and Okta are replaced. The rules are
//! the stack's (testdata/rules.json, which infra/tests/test_obo_issuer.py keeps equal to
//! guppi_gpt_infra.obo.RULES).

use std::cell::RefCell;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Mutex as StdMutex, OnceLock};

use rsa::pkcs1v15::SigningKey;
use rsa::pkcs8::EncodePublicKey;
use rsa::signature::{SignatureEncoding, Signer as _};
use rsa::traits::PublicKeyParts;
use rsa::RsaPrivateKey;

use super::*;

thread_local! {
    pub static LOGS: RefCell<Vec<Value>> = const { RefCell::new(Vec::new()) };
}

const OKTA: &str = "https://example.okta.com/oauth2/aus1";
const OWN: &str = "https://obo.example.com";
const CHAT_APP: &str = "0oachat";
const UID: &str = "00u-employee";
const NOW: f64 = 1_800_000_000.0;
const CANVAS: &str = "hr.tools.policy hr.tools.profile.read hr.tools.pay.statements.read";

fn new_key() -> RsaPrivateKey {
    RsaPrivateKey::new(&mut rand::thread_rng(), 2048).expect("key")
}

fn okta_private() -> &'static RsaPrivateKey {
    static KEY: OnceLock<RsaPrivateKey> = OnceLock::new();
    KEY.get_or_init(new_key)
}

fn own_private() -> &'static RsaPrivateKey {
    static KEY: OnceLock<RsaPrivateKey> = OnceLock::new();
    KEY.get_or_init(new_key)
}

fn jwk_of(key: &RsaPrivateKey, kid: &str) -> Value {
    json!({"kty": "RSA", "kid": kid, "alg": "RS256", "n": b64u(&key.n().to_bytes_be()), "e": b64u(&key.e().to_bytes_be())})
}

fn rs_sign(key: &RsaPrivateKey, message: &[u8]) -> Vec<u8> {
    SigningKey::<Sha256>::new(key.clone()).sign(message).to_vec()
}

fn signed(key: &RsaPrivateKey, header: Value, claims: Value) -> String {
    let input = format!("{}.{}", b64u(header.to_string().as_bytes()), b64u(claims.to_string().as_bytes()));
    format!("{input}.{}", b64u(&rs_sign(key, input.as_bytes())))
}

struct KeySigner;

#[async_trait]
impl Signer for KeySigner {
    async fn sign(&self, message: &[u8]) -> Result<Vec<u8>, String> {
        Ok(rs_sign(own_private(), message))
    }
}

/// Okta's key endpoint: counts calls, and can be down.
#[derive(Default)]
struct FakeOkta {
    calls: AtomicUsize,
    down: AtomicBool,
}

#[async_trait]
impl Fetcher for FakeOkta {
    async fn fetch_json(&self, _url: &str) -> Result<Value, String> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        if self.down.load(Ordering::SeqCst) {
            return Err("okta unreachable".into());
        }
        Ok(json!({"keys": [jwk_of(okta_private(), "okta-kid")]}))
    }
}

fn clients() -> Vec<String> {
    rules().keys().cloned().collect()
}

fn rules() -> Map<String, Value> {
    serde_json::from_str::<Value>(include_str!("../testdata/rules.json")).unwrap().as_object().unwrap().clone()
}

fn secret_of(client: &str) -> String {
    format!("secret-{client}")
}

struct Env {
    deps: Deps,
    settings: Settings,
    clock: Arc<StdMutex<f64>>,
    okta: Arc<FakeOkta>,
}

impl Env {
    fn set_clock(&self, value: f64) {
        *self.clock.lock().unwrap() = value;
    }
}

fn env() -> Env {
    let clock = Arc::new(StdMutex::new(NOW));
    let okta = Arc::new(FakeOkta::default());
    let shared_clock = clock.clone();
    let deps = Deps {
        issuer: OWN.into(),
        kid: "own-kid".into(),
        public_jwk: jwk_of(own_private(), "own-kid"),
        signer: Arc::new(KeySigner),
        fetcher: okta.clone(),
        client_secrets: clients().iter().map(|c| (c.clone(), secret_of(c))).collect(),
        clock: Arc::new(move || *shared_clock.lock().unwrap()),
        okta: Arc::new(Mutex::new(OktaKeys::default())),
    };
    let settings = Settings {
        okta_issuer: OKTA.into(),
        okta_audience: "api://guppi".into(),
        okta_clients: [CHAT_APP, "0oaharness"].iter().map(|s| s.to_string()).collect(),
        rules: rules(),
    };
    Env { deps, settings, clock, okta }
}

fn okta_claims(overrides: Value) -> Value {
    let mut claims = json!({"iss": OKTA, "aud": "api://guppi", "cid": CHAT_APP, "uid": UID, "sub": "person@example.com",
                            "scp": ["openid", "email"], "iat": NOW - 10.0, "exp": NOW + 3000.0});
    for (key, value) in overrides.as_object().unwrap() {
        claims[key] = value.clone();
    }
    claims
}

fn okta_token(overrides: Value) -> String {
    signed(okta_private(), json!({"alg": "RS256", "kid": "okta-kid"}), okta_claims(overrides))
}

fn ok() -> String {
    okta_token(json!({}))
}

fn form(fields: &[(&str, &str)]) -> String {
    let mut out = form_urlencoded::Serializer::new(String::new());
    for (k, v) in fields {
        out.append_pair(k, v);
    }
    out.finish()
}

fn basic(client: &str, secret: &str) -> String {
    format!("Basic {}", STANDARD.encode(format!("{client}:{secret}")))
}

async fn exchange_with(env: &Env, client: &str, subject: &str, scope: &str, secret: Option<&str>, extra: &[(&str, &str)]) -> (u16, Value) {
    let mut fields = vec![("grant_type", TOKEN_EXCHANGE), ("subject_token", subject), ("subject_token_type", ACCESS_TOKEN_TYPE), ("scope", scope)];
    for (key, value) in extra {
        fields.retain(|(k, _)| k != key);
        fields.push((key, value));
    }
    let secret = secret.map(String::from).unwrap_or_else(|| env.deps.client_secrets.get(client).cloned().unwrap_or("x".into()));
    let event = json!({"requestContext": {"http": {"method": "POST"}}, "rawPath": "/token",
                       "headers": {"authorization": basic(client, &secret)}, "body": form(&fields)});
    let response = token(&event, &env.deps, &env.settings, &mut Timing::default()).await;
    (response["statusCode"].as_u64().unwrap() as u16, serde_json::from_str(response["body"].as_str().unwrap()).unwrap())
}

async fn exchange(env: &Env, client: &str, subject: &str, scope: &str) -> (u16, Value) {
    exchange_with(env, client, subject, scope, None, &[]).await
}

async fn error_of(env: &Env, client: &str, subject: &str, scope: &str) -> Value {
    exchange(env, client, subject, scope).await.1
}

fn access(body: &Value) -> String {
    body["access_token"].as_str().expect("access_token").to_string()
}

fn claims_of(token: &str) -> Value {
    serde_json::from_slice(&unb64u(token.split('.').nth(1).unwrap()).unwrap()).unwrap()
}

fn err(code: &str) -> Value {
    json!({"error": code})
}

/// The Profile agents token, the canvas token and a Profile T2, as the hops get them.
async fn chain(env: &Env) -> (String, String, String) {
    let t1 = access(&exchange(env, "hr-bridge", &ok(), "hr.agents.profile").await.1);
    let t1p = access(&exchange(env, "hr-bridge", &ok(), CANVAS).await.1);
    let t2 = access(&exchange(env, "hr-agent-profile", &t1, "hr.tools.policy hr.tools.profile.read hr.tools.profile.write").await.1);
    (t1, t1p, t2)
}

async fn agents_token(env: &Env, domain: &str) -> String {
    access(&exchange(env, "hr-bridge", &ok(), &format!("hr.agents.{domain}")).await.1)
}

fn last_log() -> Value {
    LOGS.with(|logs| logs.borrow().last().cloned().expect("a log line"))
}

#[tokio::test]
async fn the_bridge_gets_an_agents_token_naming_the_employee() {
    let env = env();
    let (status, body) = exchange(&env, "hr-bridge", &ok(), "hr.agents.profile").await;
    assert_eq!(status, 200);
    assert_eq!(body["issued_token_type"], ACCESS_TOKEN_TYPE);
    let claims = claims_of(&access(&body));
    assert_eq!(claims["iss"], OWN);
    assert_eq!(claims["aud"], "api://hr-agents/profile");
    assert_eq!(claims["scope"], "hr.agents.profile");
    assert_eq!(claims["sub"], UID);
    assert_eq!(claims["client_id"], "hr-bridge");
    assert_eq!(claims["act"], json!({"sub": "hr-bridge"}));
    assert_eq!(claims["exp"], json!(NOW as i64 + 3000));
    assert!(claims.get("scp").is_none());
    let header: Value = serde_json::from_slice(&unb64u(access(&body).split('.').next().unwrap()).unwrap()).unwrap();
    assert_eq!(header, json!({"alg": "RS256", "typ": "at+jwt", "kid": "own-kid"}));
}

#[tokio::test]
async fn tokens_never_outlive_the_subject_or_an_hour() {
    let env = env();
    let short = exchange(&env, "hr-bridge", &okta_token(json!({"exp": NOW + 600.0})), "hr.agents.pay").await.1;
    let long = exchange(&env, "hr-bridge", &okta_token(json!({"exp": NOW + 9000.0})), "hr.agents.pay").await.1;
    assert_eq!(claims_of(&access(&short))["exp"], json!(NOW as i64 + 600));
    assert_eq!(claims_of(&access(&long))["exp"], json!(NOW as i64 + 3600));
}

#[tokio::test]
async fn the_canvas_token_reads_but_cannot_write_or_see_bank_details() {
    let env = env();
    let (status, body) = exchange(&env, "hr-bridge", &ok(), CANVAS).await;
    assert_eq!(status, 200);
    assert_eq!(claims_of(&access(&body))["aud"], "api://hr-tools");
    assert_eq!(error_of(&env, "hr-bridge", &ok(), "hr.tools.pay.write").await, err("invalid_scope"));
    assert_eq!(error_of(&env, "hr-bridge", &ok(), "hr.tools.pay.read").await, err("invalid_scope"));
}

#[tokio::test]
async fn scopes_for_two_audiences_are_refused() {
    let env = env();
    assert_eq!(error_of(&env, "hr-bridge", &ok(), "hr.agents.pay hr.tools.policy").await, err("invalid_scope"));
    assert_eq!(error_of(&env, "hr-bridge", &ok(), "hr.agents.pay hr.agents.travel").await, err("invalid_scope"));
}

#[tokio::test]
async fn each_sub_agent_gets_only_its_domain() {
    let env = env();
    let (status, body) = exchange(&env, "hr-agent-pay", &agents_token(&env, "pay").await, "hr.tools.policy hr.tools.pay.read hr.tools.pay.write").await;
    assert_eq!(status, 200);
    let claims = claims_of(&access(&body));
    assert_eq!(claims["act"], json!({"sub": "hr-agent-pay", "act": {"sub": "hr-bridge"}}));
    assert_eq!(claims["sub"], UID);
    assert_eq!(error_of(&env, "hr-agent-travel", &agents_token(&env, "travel").await, "hr.tools.pay.read").await, err("invalid_scope"));
    assert_eq!(error_of(&env, "hr-agent-profile", &agents_token(&env, "profile").await, "hr.tools.pay.read").await, err("invalid_scope"));
    assert_eq!(exchange(&env, "hr-agent-travel", &agents_token(&env, "travel").await, "hr.tools.policy").await.0, 200);
}

#[tokio::test]
async fn an_agents_token_serves_only_its_own_sub_agent() {
    // A Travel runtime that holds the Travel agents token cannot become the Pay agent.
    let env = env();
    let travel = agents_token(&env, "travel").await;
    assert_eq!(error_of(&env, "hr-agent-pay", &travel, "hr.tools.policy").await, err("invalid_grant"));
    assert_eq!(error_of(&env, "hr-agent-profile", &travel, "hr.tools.policy").await, err("invalid_grant"));
}

#[tokio::test]
async fn sub_agents_accept_only_the_bridges_agents_token() {
    let env = env();
    let (_, t1p, t2) = chain(&env).await;
    assert_eq!(error_of(&env, "hr-agent-profile", &ok(), "hr.tools.policy").await, err("invalid_grant"));
    assert_eq!(error_of(&env, "hr-agent-profile", &t1p, "hr.tools.policy").await, err("invalid_grant"));
    assert_eq!(error_of(&env, "hr-agent-profile", &t2, "hr.tools.policy").await, err("invalid_grant"));
}

#[tokio::test]
async fn the_bridge_accepts_only_okta_tokens() {
    let env = env();
    let (t1, t1p, _) = chain(&env).await;
    assert_eq!(error_of(&env, "hr-bridge", &t1p, "hr.agents.profile").await, err("invalid_grant"));
    assert_eq!(error_of(&env, "hr-bridge", &t1, "hr.agents.profile").await, err("invalid_grant"));
}

#[tokio::test]
async fn the_tools_gateway_carries_the_callers_scopes_to_the_runtime() {
    let env = env();
    let (_, t1p, t2) = chain(&env).await;
    let (status, body) = exchange(&env, "hr-tools-gateway", &t2, "hr.tools.policy").await;
    assert_eq!(status, 200);
    let claims = claims_of(&access(&body));
    assert_eq!(claims["aud"], "api://hr-tools-runtime");
    assert_eq!(claims["sub"], UID);
    assert_eq!(claims["scope"], "hr.tools.policy hr.tools.profile.read hr.tools.profile.write");
    assert_eq!(claims["act"], json!({"sub": "hr-tools-gateway", "act": {"sub": "hr-agent-profile", "act": {"sub": "hr-bridge"}}}));
    let body = exchange(&env, "hr-tools-gateway", &t1p, "hr.tools.policy").await.1;
    assert_eq!(claims_of(&access(&body))["scope"], CANVAS);
}

#[tokio::test]
async fn the_tools_gateway_refuses_tokens_not_meant_for_the_tools_gateway() {
    let env = env();
    let (t1, _, t2) = chain(&env).await;
    let t3 = access(&exchange(&env, "hr-tools-gateway", &t2, "hr.tools.policy").await.1);
    assert_eq!(error_of(&env, "hr-tools-gateway", &t1, "hr.tools.policy").await, err("invalid_grant"));
    assert_eq!(error_of(&env, "hr-tools-gateway", &ok(), "hr.tools.policy").await, err("invalid_grant"));
    assert_eq!(error_of(&env, "hr-tools-gateway", &t3, "hr.tools.policy").await, err("invalid_grant"));
}

#[tokio::test]
async fn client_authentication() {
    let env = env();
    assert_eq!(exchange_with(&env, "hr-bridge", &ok(), "hr.agents.profile", Some("wrong"), &[]).await, (401, err("invalid_client")));
    assert_eq!(exchange(&env, "nobody", &ok(), "hr.agents.profile").await, (401, err("invalid_client")));
    let event = json!({"requestContext": {"http": {"method": "POST"}}, "rawPath": "/token",
                       "headers": {"authorization": "Basic %%%not-base64"}, "body": ""});
    assert_eq!(token(&event, &env.deps, &env.settings, &mut Timing::default()).await["statusCode"], 401);
}

#[tokio::test]
async fn okta_token_checks() {
    let env = env();
    let cases = [
        json!({"aud": "api://other"}),
        json!({"cid": "0oaother"}),
        json!({"exp": NOW - 120.0}),
        json!({"iat": NOW + 600.0}),
        json!({"nbf": NOW + 600.0}),
        json!({"iss": "https://elsewhere"}),
    ];
    for overrides in cases {
        assert_eq!(error_of(&env, "hr-bridge", &okta_token(overrides.clone()), "hr.agents.profile").await, err("invalid_grant"), "{overrides}");
    }
    let no_uid = signed(okta_private(), json!({"alg": "RS256", "kid": "okta-kid"}),
                        json!({"iss": OKTA, "aud": "api://guppi", "cid": CHAT_APP, "iat": NOW, "exp": NOW + 600.0}));
    assert_eq!(error_of(&env, "hr-bridge", &no_uid, "hr.agents.profile").await, err("invalid_grant"));
}

#[tokio::test]
async fn signature_and_header_checks() {
    let env = env();
    let good = ok();
    let parts: Vec<&str> = good.split('.').collect();
    let (head, body, sig) = (parts[0], parts[1], parts[2]);
    let mut claims = claims_of(&good);
    claims["uid"] = json!("someone-else");
    let tampered = format!("{head}.{}.{sig}", b64u(claims.to_string().as_bytes()));
    let short_sig = format!("{head}.{body}.{}", b64u(&unb64u(sig).unwrap()[1..]));
    let none_alg = format!("{}.{body}.", b64u(json!({"alg": "none", "kid": "okta-kid"}).to_string().as_bytes()));
    let hs256 = format!("{}.{body}.{sig}", b64u(json!({"alg": "HS256", "kid": "okta-kid"}).to_string().as_bytes()));
    for bad in [tampered, short_sig, none_alg, hs256, "not-a-token".into(), String::new()] {
        assert_eq!(error_of(&env, "hr-bridge", &bad, "hr.agents.profile").await, err("invalid_grant"), "{bad}");
    }
    let forged = signed(&new_key(), json!({"alg": "RS256", "kid": "okta-kid"}), claims_of(&good));
    assert_eq!(error_of(&env, "hr-bridge", &forged, "hr.agents.profile").await, err("invalid_grant"));
}

#[tokio::test]
async fn own_tokens_need_typ_and_kid() {
    let env = env();
    let (t1, _, _) = chain(&env).await;
    let no_typ = signed(own_private(), json!({"alg": "RS256", "kid": "own-kid"}), claims_of(&t1));
    let other_kid = signed(own_private(), json!({"alg": "RS256", "typ": "at+jwt", "kid": "old"}), claims_of(&t1));
    for bad in [no_typ, other_kid] {
        assert_eq!(error_of(&env, "hr-agent-profile", &bad, "hr.tools.policy").await, err("invalid_grant"));
    }
}

#[tokio::test]
async fn other_grants_and_actor_tokens_are_refused() {
    let env = env();
    let other = exchange_with(&env, "hr-bridge", &ok(), "hr.agents.profile", None, &[("grant_type", "client_credentials")]).await;
    assert_eq!(other.1, err("unsupported_grant_type"));
    let actor = exchange_with(&env, "hr-bridge", &ok(), "hr.agents.profile", None, &[("actor_token", "x")]).await;
    assert_eq!(actor.1, err("invalid_request"));
}

#[tokio::test]
async fn unknown_okta_keys_are_refetched_at_most_once_a_minute() {
    let env = env();
    assert_eq!(exchange(&env, "hr-bridge", &ok(), "hr.agents.profile").await.0, 200);
    let rotated = signed(okta_private(), json!({"alg": "RS256", "kid": "rotated"}), okta_claims(json!({})));
    for _ in 0..5 {
        exchange(&env, "hr-bridge", &rotated, "hr.agents.profile").await;
    }
    assert_eq!(env.okta.calls.load(Ordering::SeqCst), 1); // within the minute of the first fill, an unknown kid waits
    env.set_clock(NOW + 61.0);
    for _ in 0..5 {
        exchange(&env, "hr-bridge", &rotated, "hr.agents.profile").await;
    }
    assert_eq!(env.okta.calls.load(Ordering::SeqCst), 2); // then one refetch, and no more for a minute
}

#[tokio::test]
async fn built_in_okta_keys_answer_without_asking_okta() {
    let env = env();
    {
        let mut okta = env.deps.okta.lock().await;
        okta.keys = keys_by_kid(&json!({"keys": [jwk_of(okta_private(), "okta-kid")]})).unwrap();
        okta.fetched_at = NOW;
    }
    assert_eq!(exchange(&env, "hr-bridge", &ok(), "hr.agents.profile").await.0, 200);
    assert_eq!(env.okta.calls.load(Ordering::SeqCst), 0);
}

#[test]
fn the_built_in_okta_keys_are_rsa_keys_by_kid() {
    let keys = keys_by_kid(&serde_json::from_str(BUILT_IN_OKTA_KEYS).unwrap()).unwrap();
    assert!(!keys.is_empty());
    assert!(keys.values().all(|k| k["kty"] == "RSA" && k["n"].is_string() && k["e"].is_string()));
}

#[tokio::test]
async fn stale_keys_are_refreshed_in_the_background_and_still_answer() {
    let env = env();
    assert_eq!(exchange(&env, "hr-bridge", &ok(), "hr.agents.pay").await.0, 200);
    let later = NOW + 2.0 * JWKS_TTL;
    env.set_clock(later);
    let token = okta_token(json!({"iat": later - 10.0, "exp": later + 600.0}));
    assert_eq!(exchange(&env, "hr-bridge", &token, "hr.agents.pay").await.0, 200);
    tokio::task::yield_now().await;
    for _ in 0..50 {
        if env.okta.calls.load(Ordering::SeqCst) == 2 {
            break;
        }
        tokio::task::yield_now().await;
    }
    assert_eq!(env.okta.calls.load(Ordering::SeqCst), 2);
}

#[tokio::test]
async fn cached_okta_keys_survive_an_okta_outage() {
    let env = env();
    assert_eq!(exchange(&env, "hr-bridge", &ok(), "hr.agents.pay").await.0, 200);
    let later = NOW + 2.0 * JWKS_TTL;
    env.set_clock(later);
    env.okta.down.store(true, Ordering::SeqCst);
    let token = okta_token(json!({"iat": later - 10.0, "exp": later + 600.0}));
    assert_eq!(exchange(&env, "hr-bridge", &token, "hr.agents.pay").await.0, 200);
}

#[tokio::test]
async fn errors_carry_no_reason_and_never_500() {
    let env = env();
    let event = json!({"requestContext": {"http": {"method": "POST"}}, "rawPath": "/token", "headers": {},
                       "body": "%%%", "isBase64Encoded": true});
    let response = token(&event, &env.deps, &env.settings, &mut Timing::default()).await;
    let status = response["statusCode"].as_u64().unwrap();
    assert!(status == 400 || status == 401);
    let body: Value = serde_json::from_str(response["body"].as_str().unwrap()).unwrap();
    assert_eq!(body.as_object().unwrap().keys().collect::<Vec<_>>(), vec!["error"]);
}

#[test]
fn der_parse_matches_the_key() {
    let der = own_private().to_public_key().to_public_key_der().unwrap();
    let (n, e) = rsa_public_numbers(der.as_bytes()).unwrap();
    assert_eq!(n, own_private().n().to_bytes_be());
    assert_eq!(e, own_private().e().to_bytes_be());
}

#[test]
fn discovery_lists_the_scopes() {
    let env = env();
    let doc = discovery(&env.deps, &env.settings);
    assert_eq!(doc["issuer"], OWN);
    assert_eq!(doc["token_endpoint"], format!("{OWN}/token"));
    assert!(doc["scopes_supported"].as_array().unwrap().contains(&json!("hr.agents.pay")));
    assert_eq!(doc["grant_types_supported"], json!([TOKEN_EXCHANGE]));
}

#[tokio::test]
async fn a_subject_about_to_expire_is_refused() {
    let env = env();
    assert_eq!(error_of(&env, "hr-bridge", &okta_token(json!({"exp": NOW + 30.0})), "hr.agents.pay").await, err("invalid_grant"));
    assert_eq!(exchange(&env, "hr-bridge", &okta_token(json!({"exp": NOW + 120.0})), "hr.agents.pay").await.0, 200);
}

#[tokio::test]
async fn client_secret_post_and_url_encoded_basic() {
    let mut env = env();
    env.deps.client_secrets.insert("hr-bridge".into(), "a b:c".into());
    let subject = ok();
    let body = form(&[("grant_type", TOKEN_EXCHANGE), ("subject_token", &subject), ("scope", "hr.agents.pay"),
                      ("client_id", "hr-bridge"), ("client_secret", "a b:c")]);
    let event = json!({"requestContext": {}, "httpMethod": "POST", "path": "/token", "headers": {}, "body": body});
    assert_eq!(token(&event, &env.deps, &env.settings, &mut Timing::default()).await["statusCode"], 200);
    let encoded = STANDARD.encode("hr-bridge:a%20b%3Ac");
    let body = form(&[("grant_type", TOKEN_EXCHANGE), ("subject_token", &subject), ("scope", "hr.agents.pay")]);
    let event = json!({"httpMethod": "POST", "path": "/token", "headers": {"Authorization": format!("Basic {encoded}")}, "body": body});
    assert_eq!(token(&event, &env.deps, &env.settings, &mut Timing::default()).await["statusCode"], 200);
}

#[tokio::test]
async fn an_unknown_subject_token_type_is_refused() {
    let env = env();
    let result = exchange_with(&env, "hr-bridge", &ok(), "hr.agents.pay", None,
                               &[("subject_token_type", "urn:ietf:params:oauth:token-type:id_token")]).await;
    assert_eq!(result, (400, err("invalid_request")));
}

#[tokio::test]
async fn rest_api_events_and_cacheable_reads() {
    let env = env();
    for path in ["/jwks.json", "/.well-known/openid-configuration"] {
        let response = handle(&json!({"httpMethod": "GET", "path": path, "headers": {}}), &env.deps, &env.settings, Timing::default()).await;
        assert_eq!(response["statusCode"], 200);
        assert_eq!(response["headers"]["cache-control"], "public, max-age=300");
    }
    let response = handle(&json!({"httpMethod": "GET", "path": "/token", "headers": {}}), &env.deps, &env.settings, Timing::default()).await;
    assert_eq!(response["statusCode"], 404);
}

#[tokio::test]
async fn the_log_names_tokens_by_jti_never_the_employee() {
    let env = env();
    exchange(&env, "hr-bridge", &okta_token(json!({"jti": "okta-jti-1"})), "hr.agents.pay").await;
    let line = last_log();
    assert_eq!(line["subject_jti"], "okta-jti-1");
    assert_eq!(line["jti"].as_str().unwrap().len(), 32);
    let text = line.to_string();
    assert!(!text.contains(UID) && !text.contains("person@example.com"));
}

#[tokio::test]
async fn the_token_line_times_the_load_the_verify_and_the_mint() {
    let env = env();
    let body = form(&[("grant_type", TOKEN_EXCHANGE), ("subject_token", &ok()), ("subject_token_type", ACCESS_TOKEN_TYPE),
                      ("scope", "hr.agents.pay")]);
    let event = json!({"httpMethod": "POST", "path": "/token", "body": body,
                       "headers": {"Authorization": basic("hr-bridge", &secret_of("hr-bridge"))}});
    let mut ms = Map::new();
    ms.insert("load".into(), json!(0));
    let response = handle(&event, &env.deps, &env.settings, Timing { cold: false, ms }).await;
    let line = last_log();
    assert_eq!(response["statusCode"], 200);
    assert_eq!(line["status"], 200);
    let keys: HashSet<&str> = line["ms"].as_object().unwrap().keys().map(String::as_str).collect();
    assert_eq!(keys, HashSet::from(["load", "verify", "mint"]));
    assert_eq!(line["cold"], false);
}

#[tokio::test]
async fn refusals_say_what_was_claimed_and_presented_never_who() {
    let env = env();
    exchange_with(&env, "hr-bridge", &ok(), "hr.agents.pay", Some("wrong"), &[]).await;
    let line = last_log();
    assert_eq!(line["claimed_client"], "hr-bridge");
    assert_eq!(line["client"], "-");
    let pay = access(&exchange(&env, "hr-bridge", &ok(), "hr.agents.pay").await.1);
    exchange(&env, "hr-agent-travel", &pay, "hr.tools.policy").await;
    let line = last_log();
    assert_eq!(line["subject_aud"], "api://hr-agents/pay");
    assert_eq!(line["subject_client"], "hr-bridge");
    assert_eq!(line["subject_depth"], 1);
    assert_eq!(line["subject_jti"].as_str().unwrap().len(), 32);
    assert!(!line.to_string().contains(UID));
}
