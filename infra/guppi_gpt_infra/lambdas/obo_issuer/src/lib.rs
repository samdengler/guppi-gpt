//! The on-behalf-of token issuer (guppi-hr D20, D47, D51; docs in guppi-hr
//! docs/proposals/obo-token-exchange.md).
//!
//! An RFC 8693 token exchange endpoint that stands in for the production identity provider.
//! Okta signs people in (D46) but its free plan cannot exchange tokens, so this function
//! trades a token for the next hop's, still naming the employee, and records who acted in a
//! nested `act` claim. AgentCore Identity calls it through on-behalf-of credential providers.
//!
//! Routes: GET /.well-known/openid-configuration, GET /jwks.json, POST /token.
//!
//! Every client has a rule (the RULES environment value, built by the stack): which subject
//! tokens it may present (issuer, audience, acting client, depth of the `act` chain) and
//! which scopes it may receive for which audience. A client that inherits scopes receives the
//! subject token's own scopes for its one audience, whatever it requests; the tools gateway
//! uses this, since its target requests fixed scopes.
//!
//! Tokens are verified by modular exponentiation and a constant-time comparison of the
//! PKCS#1 v1.5 encoding. KMS signs; the private key never leaves it. Okta's public keys are
//! built into the binary (`okta-keys.json`, written by scripts/okta.py); Okta is asked only
//! for a key the binary does not have, at most once a minute, and the keys are refreshed in
//! the background after an hour. Nothing logged names a person or holds a token.
//!
//! This file holds everything that decides; `main.rs` reaches AWS. Rewritten from Python in
//! October 2026 for the cold start (guppi-hr D51, L25); the rules and refusals are the same.

use std::collections::{BTreeSet, HashMap, HashSet};
use std::sync::Arc;
use std::time::Instant;

use async_trait::async_trait;
use base64::Engine;
use base64::engine::general_purpose::{GeneralPurpose, GeneralPurposeConfig, STANDARD};
use base64::engine::{DecodePaddingMode, general_purpose};
use num_bigint::BigUint;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use subtle::ConstantTimeEq;
use tokio::sync::Mutex;

pub const TOKEN_EXCHANGE: &str = "urn:ietf:params:oauth:grant-type:token-exchange";
pub const ACCESS_TOKEN_TYPE: &str = "urn:ietf:params:oauth:token-type:access_token";
const JWT_TOKEN_TYPE: &str = "urn:ietf:params:oauth:token-type:jwt";
pub const LIFETIME: i64 = 3600;
pub const LEEWAY: f64 = 60.0;
/// A subject token this close to expiry is refused rather than traded for one that would
/// arrive already expired.
pub const MIN_REMAINING: f64 = 60.0;
pub const JWKS_TTL: f64 = 3600.0;
pub const JWKS_REFETCH_MIN_INTERVAL: f64 = 60.0;
pub const MAX_TOKEN_LENGTH: usize = 8192;
const MAX_FORM_FIELDS: usize = 20;
const SHA256_DIGEST_INFO: [u8; 19] = [
    0x30, 0x31, 0x30, 0x0d, 0x06, 0x09, 0x60, 0x86, 0x48, 0x01, 0x65, 0x03, 0x04, 0x02, 0x01, 0x05, 0x00, 0x04, 0x20,
];

/// Okta's public keys as of the build (scripts/okta.py writes the file).
pub const BUILT_IN_OKTA_KEYS: &str = include_str!("../okta-keys.json");

/// Base64url as JWTs use it: no padding on the way out, padding optional on the way in.
const B64U: GeneralPurpose = GeneralPurpose::new(
    &base64::alphabet::URL_SAFE,
    GeneralPurposeConfig::new()
        .with_encode_padding(false)
        .with_decode_padding_mode(DecodePaddingMode::Indifferent)
        .with_decode_allow_trailing_bits(true),
);

pub fn b64u(data: &[u8]) -> String {
    B64U.encode(data)
}

pub fn unb64u(text: &str) -> Option<Vec<u8>> {
    B64U.decode(text).ok()
}

/// A request the endpoint refuses, with the OAuth error code and an internal reason.
#[derive(Debug)]
pub struct Refused {
    pub status: u16,
    pub error: &'static str,
    pub reason: String,
    pub claimed: String,
}

fn refused(status: u16, error: &'static str, reason: &str) -> Refused {
    Refused { status, error, reason: reason.to_string(), claimed: String::new() }
}

fn grant_refused(reason: &str) -> Refused {
    refused(400, "invalid_grant", reason)
}

/// (n, e) from a DER SubjectPublicKeyInfo holding an RSA key, as KMS returns it.
pub fn rsa_public_numbers(spki_der: &[u8]) -> Option<(Vec<u8>, Vec<u8>)> {
    fn element(data: &[u8], i: usize) -> Option<(usize, usize)> {
        let length = *data.get(i + 1)? as usize;
        let mut start = i + 2;
        let mut len = length;
        if length & 0x80 != 0 {
            let count = length & 0x7f;
            len = data.get(start..start + count)?.iter().fold(0usize, |acc, b| (acc << 8) | *b as usize);
            start += count;
        }
        (start + len <= data.len()).then_some((start, start + len))
    }
    let (seq, _) = element(spki_der, 0)?;
    let (_, alg_end) = element(spki_der, seq)?;
    let (bits, _) = element(spki_der, alg_end)?;
    let (rsa_key, _) = element(spki_der, bits + 1)?; // skip the BIT STRING's unused-bits byte
    let (n_start, n_end) = element(spki_der, rsa_key)?;
    let (e_start, e_end) = element(spki_der, n_end)?;
    let n = &spki_der[n_start..n_end];
    let n = &n[n.iter().position(|b| *b != 0).unwrap_or(n.len())..];
    Some((n.to_vec(), spki_der[e_start..e_end].to_vec()))
}

/// RSASSA-PKCS1-v1_5 with SHA-256 (RFC 8017 8.2.2), by re-encoding and comparing.
pub fn rs256_valid(jwk: &Value, signing_input: &[u8], signature: &[u8]) -> bool {
    let part = |name: &str| jwk.get(name).and_then(Value::as_str).and_then(unb64u);
    let (Some(n), Some(e)) = (part("n"), part("e")) else { return false };
    let n = BigUint::from_bytes_be(&n);
    let e = BigUint::from_bytes_be(&e);
    let k = n.bits().div_ceil(8) as usize;
    if n.bits() < 2048 || signature.len() != k {
        return false;
    }
    let s = BigUint::from_bytes_be(signature);
    if s >= n {
        return false;
    }
    let m = s.modpow(&e, &n).to_bytes_be();
    let mut em = vec![0u8; k - m.len()];
    em.extend_from_slice(&m);
    let mut t = SHA256_DIGEST_INFO.to_vec();
    t.extend_from_slice(&Sha256::digest(signing_input));
    let mut expected = vec![0x00, 0x01];
    expected.resize(k - t.len() - 1, 0xff);
    expected.push(0x00);
    expected.extend_from_slice(&t);
    em.ct_eq(&expected).into()
}

pub fn act_depth(claims: &Map<String, Value>) -> i64 {
    let mut depth = 0;
    let mut act = claims.get("act");
    while let Some(Value::Object(inner)) = act {
        depth += 1;
        act = inner.get("act");
    }
    depth
}

/// Signs with the issuer's private key (KMS in Lambda).
#[async_trait]
pub trait Signer: Send + Sync {
    async fn sign(&self, message: &[u8]) -> Result<Vec<u8>, String>;
}

/// Fetches a JSON document (Okta's keys).
#[async_trait]
pub trait Fetcher: Send + Sync {
    async fn fetch_json(&self, url: &str) -> Result<Value, String>;
}

/// Okta's keys by kid, when they were fetched, and when a fetch was last tried.
#[derive(Default)]
pub struct OktaKeys {
    pub keys: HashMap<String, Value>,
    pub fetched_at: f64,
    pub refetch_at: f64,
}

/// The keys of a JWKS document, by kid.
pub fn keys_by_kid(jwks: &Value) -> Option<HashMap<String, Value>> {
    let keys = jwks.get("keys")?.as_array()?;
    Some(
        keys.iter()
            .filter_map(|k| Some((k.get("kid")?.as_str()?.to_string(), k.clone())))
            .collect(),
    )
}

/// What the function reaches outside itself; the tests replace it.
pub struct Deps {
    pub issuer: String,
    pub kid: String,
    pub public_jwk: Value,
    pub signer: Arc<dyn Signer>,
    pub fetcher: Arc<dyn Fetcher>,
    pub client_secrets: HashMap<String, String>,
    pub clock: Arc<dyn Fn() -> f64 + Send + Sync>,
    pub okta: Arc<Mutex<OktaKeys>>,
}

impl Deps {
    fn now(&self) -> f64 {
        (self.clock)()
    }
}

pub struct Settings {
    pub okta_issuer: String,
    pub okta_audience: String,
    pub okta_clients: HashSet<String>,
    pub rules: Map<String, Value>,
}

impl Settings {
    pub fn from_env() -> Result<Self, String> {
        let var = |name: &str| std::env::var(name).map_err(|_| format!("{name} is not set"));
        let rules = serde_json::from_str::<Value>(&var("RULES")?).map_err(|e| e.to_string())?;
        Ok(Settings {
            okta_issuer: var("OKTA_ISSUER")?,
            okta_audience: var("OKTA_AUDIENCE")?,
            okta_clients: var("OKTA_CLIENTS")?.split(',').filter(|c| !c.is_empty()).map(String::from).collect(),
            rules: rules.as_object().cloned().ok_or("RULES is not an object")?,
        })
    }
}

/// The Okta key named `kid`. A key the function does not have is fetched before answering,
/// at most once a minute; keys older than an hour are refreshed in the background, so a
/// known key never waits on Okta.
async fn okta_key(deps: &Deps, settings: &Settings, kid: &str) -> Option<Value> {
    let now = deps.now();
    let url = format!("{}/v1/keys", settings.okta_issuer);
    let mut okta = deps.okta.lock().await;
    let stale = now - okta.fetched_at > JWKS_TTL;
    let unknown = !okta.keys.contains_key(kid);
    if (stale || unknown) && now - okta.refetch_at >= JWKS_REFETCH_MIN_INTERVAL {
        okta.refetch_at = now;
        if unknown {
            if let Some(keys) = deps.fetcher.fetch_json(&url).await.ok().as_ref().and_then(keys_by_kid) {
                okta.keys = keys;
                okta.fetched_at = now;
            }
        } else {
            let (fetcher, shared) = (deps.fetcher.clone(), deps.okta.clone());
            tokio::spawn(async move {
                if let Some(keys) = fetcher.fetch_json(&url).await.ok().as_ref().and_then(keys_by_kid) {
                    let mut okta = shared.lock().await;
                    okta.keys = keys;
                    okta.fetched_at = now;
                }
            });
        }
    }
    okta.keys.get(kid).cloned()
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Source {
    Okta,
    Own,
}

impl Source {
    fn name(self) -> &'static str {
        match self {
            Source::Okta => "okta",
            Source::Own => "self",
        }
    }
}

fn number(claims: &Map<String, Value>, name: &str) -> Option<f64> {
    claims.get(name).and_then(Value::as_f64)
}

/// The subject token's claims and its source; Refused otherwise.
pub async fn verify(token: &str, deps: &Deps, settings: &Settings) -> Result<(Map<String, Value>, Source), Refused> {
    if token.is_empty() || token.chars().count() > MAX_TOKEN_LENGTH || token.matches('.').count() != 2 {
        return Err(grant_refused("malformed subject token"));
    }
    let mut parts = token.split('.');
    let (head, body, sig) = (parts.next().unwrap_or(""), parts.next().unwrap_or(""), parts.next().unwrap_or(""));
    let decoded = |part: &str| unb64u(part).and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok());
    let (Some(header), Some(claims), Some(signature)) = (decoded(head), decoded(body), unb64u(sig)) else {
        return Err(grant_refused("undecodable subject token"));
    };
    let (Value::Object(header), Value::Object(claims)) = (header, claims) else {
        return Err(grant_refused("malformed subject token"));
    };
    if header.get("alg").and_then(Value::as_str) != Some("RS256") || header.contains_key("crit") {
        return Err(grant_refused("unsupported token header"));
    }
    let issuer = claims.get("iss").and_then(Value::as_str);
    let (source, key) = if issuer == Some(settings.okta_issuer.as_str()) {
        let kid = match header.get("kid") {
            Some(Value::String(kid)) => kid.clone(),
            Some(other) => other.to_string(),
            None => String::new(),
        };
        (Source::Okta, okta_key(deps, settings, &kid).await)
    } else if issuer == Some(deps.issuer.as_str()) {
        if header.get("typ").and_then(Value::as_str) != Some("at+jwt") {
            return Err(grant_refused("own token without typ at+jwt"));
        }
        let own = header.get("kid").and_then(Value::as_str) == Some(deps.kid.as_str());
        (Source::Own, own.then(|| deps.public_jwk.clone()))
    } else {
        return Err(grant_refused("untrusted issuer"));
    };
    let signing_input = format!("{head}.{body}");
    if !key.is_some_and(|key| rs256_valid(&key, signing_input.as_bytes(), &signature)) {
        return Err(grant_refused("bad signature"));
    }
    let now = deps.now();
    let (Some(exp), Some(iat)) = (number(&claims, "exp"), number(&claims, "iat")) else {
        return Err(grant_refused(if number(&claims, "exp").is_none() { "missing exp" } else { "missing iat" }));
    };
    let nbf = match claims.get("nbf") {
        None => 0.0,
        Some(value) => value.as_f64().ok_or_else(|| grant_refused("malformed nbf"))?,
    };
    if exp <= now - LEEWAY {
        return Err(grant_refused("expired"));
    }
    if iat > now + LEEWAY || nbf > now + LEEWAY {
        return Err(grant_refused("not yet valid"));
    }
    if exp - now < MIN_REMAINING {
        return Err(grant_refused("expires too soon"));
    }
    if source == Source::Okta {
        if claims.get("aud").and_then(Value::as_str) != Some(settings.okta_audience.as_str()) {
            return Err(grant_refused("okta audience"));
        }
        if !claims.get("cid").and_then(Value::as_str).is_some_and(|cid| settings.okta_clients.contains(cid)) {
            return Err(grant_refused("okta client"));
        }
        if !claims.get("uid").and_then(Value::as_str).is_some_and(|uid| !uid.is_empty()) {
            return Err(grant_refused("okta token without uid"));
        }
    }
    Ok((claims, source))
}

/// A request header by name, whatever its case (REST API events keep the caller's).
pub fn header<'a>(event: &'a Value, name: &str) -> &'a str {
    event
        .get("headers")
        .and_then(Value::as_object)
        .and_then(|headers| headers.iter().find(|(key, _)| key.to_lowercase() == name))
        .and_then(|(_, value)| value.as_str())
        .unwrap_or("")
}

fn unquote(text: &str) -> String {
    percent_encoding::percent_decode_str(text).decode_utf8_lossy().into_owned()
}

fn sha256(text: &str) -> [u8; 32] {
    Sha256::digest(text.as_bytes()).into()
}

pub fn client_of(event: &Value, form: &HashMap<String, String>, deps: &Deps) -> Result<String, Refused> {
    let authorization = header(event, "authorization");
    let malformed = || refused(401, "invalid_client", "malformed client authentication");
    let (client_id, secret) = match authorization.get(..6) {
        Some(prefix) if prefix.eq_ignore_ascii_case("basic ") => {
            let raw = STANDARD.decode(&authorization[6..]).map_err(|_| malformed())?;
            let raw = String::from_utf8(raw).map_err(|_| malformed())?;
            let (client_id, secret) = raw.split_once(':').unwrap_or((raw.as_str(), ""));
            (unquote(client_id), unquote(secret))
        }
        _ => (
            form.get("client_id").cloned().unwrap_or_default(),
            form.get("client_secret").cloned().unwrap_or_default(),
        ),
    };
    let expected = deps.client_secrets.get(&client_id);
    // Compared even for an unknown client, so the time does not say which clients exist.
    let ok: bool = sha256(&secret).ct_eq(&sha256(expected.map_or("\0unknown", String::as_str))).into();
    if expected.is_none() || !ok {
        // The client id the caller claimed, cut short, so a probe can be told from a test.
        let mut rejected = refused(401, "invalid_client", "client authentication failed");
        rejected.claimed = client_id.chars().take(40).collect();
        return Err(rejected);
    }
    Ok(client_id)
}

fn strings(value: Option<&Value>) -> Vec<&Value> {
    value.and_then(Value::as_array).map(|items| items.iter().collect()).unwrap_or_default()
}

pub fn subject_allowed(rule: &Value, claims: &Map<String, Value>, source: Source) -> Result<(), Refused> {
    let allowed = &rule["subject"];
    if allowed["issuer"].as_str() != Some(source.name()) {
        return Err(grant_refused("subject issuer not allowed for client"));
    }
    if source == Source::Own {
        let aud = claims.get("aud").unwrap_or(&Value::Null);
        if !strings(allowed.get("audiences")).contains(&aud) {
            return Err(grant_refused("subject audience not allowed for client"));
        }
        let client = claims.get("client_id").unwrap_or(&Value::Null);
        if !strings(allowed.get("clients")).contains(&client) {
            return Err(grant_refused("subject client not allowed for client"));
        }
    }
    let depth = json!(act_depth(claims));
    if !strings(allowed.get("act_depths")).contains(&&depth) {
        return Err(grant_refused("subject act depth not allowed for client"));
    }
    Ok(())
}

/// (audience, scopes) the client receives.
pub fn grant_for(rule: &Value, requested: &[String], claims: &Map<String, Value>) -> Result<(String, Vec<String>), Refused> {
    let scopes_of = |grant: &Value| -> Vec<String> {
        strings(grant.get("scopes")).iter().filter_map(|s| s.as_str().map(String::from)).collect()
    };
    let grants = strings(rule.get("grants"));
    if rule.get("inherit_scopes").and_then(Value::as_bool).unwrap_or(false) {
        let grant = grants.first().ok_or_else(|| refused(400, "invalid_scope", "rule has no grant"))?;
        let allowed = scopes_of(grant);
        let held = claims.get("scope").and_then(Value::as_str).unwrap_or("");
        let scopes: Vec<String> = held.split_whitespace().filter(|s| allowed.iter().any(|a| a == s)).map(String::from).collect();
        if scopes.is_empty() {
            return Err(refused(400, "invalid_scope", "subject holds no inheritable scope"));
        }
        return Ok((grant["audience"].as_str().unwrap_or("").to_string(), scopes));
    }
    if requested.is_empty() {
        return Err(refused(400, "invalid_scope", "no scope requested"));
    }
    for grant in grants {
        let allowed = scopes_of(grant);
        if requested.iter().all(|s| allowed.contains(s)) {
            return Ok((grant["audience"].as_str().unwrap_or("").to_string(), requested.to_vec()));
        }
    }
    Err(refused(400, "invalid_scope", "scope not allowed for client"))
}

fn invalid_request(reason: &str) -> Refused {
    refused(400, "invalid_request", reason)
}

/// A new access token naming the same person, and its lifetime and jti.
pub async fn mint(
    deps: &Deps,
    client_id: &str,
    claims: &Map<String, Value>,
    source: Source,
    audience: &str,
    scopes: &[String],
) -> Result<(String, i64, String), Refused> {
    let now = deps.now().floor() as i64;
    let mut act = Map::new();
    act.insert("sub".into(), json!(client_id));
    if let Some(inner @ Value::Object(_)) = claims.get("act") {
        act.insert("act".into(), inner.clone());
    }
    let subject = match claims.get(if source == Source::Okta { "uid" } else { "sub" }) {
        Some(Value::String(text)) => text.clone(),
        Some(value @ Value::Number(_)) => value.to_string(),
        _ => return Err(invalid_request("subject without sub")),
    };
    let subject_exp = number(claims, "exp").ok_or_else(|| invalid_request("missing exp"))?.trunc() as i64;
    let exp = subject_exp.min(now + LIFETIME);
    let jti = uuid::Uuid::new_v4().simple().to_string();
    let payload = json!({
        "iss": deps.issuer, "sub": subject, "aud": audience, "scope": scopes.join(" "),
        "client_id": client_id, "act": act, "iat": now, "nbf": now, "exp": exp, "jti": jti,
    });
    let header = json!({"alg": "RS256", "typ": "at+jwt", "kid": deps.kid});
    let signing_input = format!("{}.{}", b64u(header.to_string().as_bytes()), b64u(payload.to_string().as_bytes()));
    let signature = deps.signer.sign(signing_input.as_bytes()).await.map_err(|_| invalid_request("sign failed"))?;
    Ok((format!("{signing_input}.{}", b64u(&signature)), exp - now, jti))
}

pub fn respond(status: u16, body: &Value, cacheable: bool) -> Value {
    // Tokens are never cached; the discovery document and the key may be, for five minutes.
    let headers = if cacheable {
        json!({"content-type": "application/json", "cache-control": "public, max-age=300"})
    } else {
        json!({"content-type": "application/json", "cache-control": "no-store", "pragma": "no-cache"})
    };
    json!({"statusCode": status, "headers": headers, "body": body.to_string()})
}

pub fn log(fields: Value) {
    println!("{fields}");
    #[cfg(test)]
    tests::LOGS.with(|logs| logs.borrow_mut().push(fields));
}

/// What a refusal log line may say about the subject token: never who it names.
fn subject_fields(claims: Option<&Map<String, Value>>) -> Map<String, Value> {
    let Some(claims) = claims else { return Map::new() };
    let mut fields = Map::new();
    fields.insert("subject_jti".into(), json!(claims.get("jti").and_then(Value::as_str).unwrap_or("")));
    fields.insert("subject_aud".into(), claims.get("aud").cloned().unwrap_or(Value::Null));
    let client = claims.get("client_id").filter(|v| truthy(v)).or_else(|| claims.get("cid"));
    fields.insert("subject_client".into(), client.cloned().unwrap_or(Value::Null));
    fields.insert("subject_depth".into(), json!(act_depth(claims)));
    fields
}

fn truthy(value: &Value) -> bool {
    !matches!(value, Value::Null | Value::Bool(false)) && value != "" && value != &json!(0)
}

/// This request's timings, for its log line.
#[derive(Clone, Default)]
pub struct Timing {
    pub cold: bool,
    pub ms: Map<String, Value>,
}

fn millis(since: Instant) -> Value {
    json!(since.elapsed().as_millis() as u64)
}

fn with(mut base: Value, extra: Map<String, Value>) -> Value {
    if let Value::Object(map) = &mut base {
        map.extend(extra);
    }
    base
}

fn form_of(event: &Value) -> Result<HashMap<String, String>, Refused> {
    let raw = event.get("body").and_then(Value::as_str).unwrap_or("");
    let raw = if event.get("isBase64Encoded").and_then(Value::as_bool).unwrap_or(false) {
        let bytes = general_purpose::STANDARD.decode(raw).map_err(|_| invalid_request("body"))?;
        String::from_utf8(bytes).map_err(|_| invalid_request("body"))?
    } else {
        raw.to_string()
    };
    if !raw.is_empty() && raw.matches('&').count() + 1 > MAX_FORM_FIELDS {
        return Err(invalid_request("too many fields"));
    }
    let mut form = HashMap::new();
    for (key, value) in form_urlencoded::parse(raw.as_bytes()) {
        if !value.is_empty() {
            form.entry(key.into_owned()).or_insert_with(|| value.into_owned());
        }
    }
    Ok(form)
}

pub async fn token(event: &Value, deps: &Deps, settings: &Settings, timing: &mut Timing) -> Value {
    let mut client_id = "-".to_string();
    let mut claims: Option<Map<String, Value>> = None;
    let result: Result<(String, Vec<String>, String, i64, String), Refused> = async {
        let form = form_of(event)?;
        client_id = client_of(event, &form, deps)?;
        if form.get("grant_type").map(String::as_str) != Some(TOKEN_EXCHANGE) {
            return Err(refused(400, "unsupported_grant_type", "grant type"));
        }
        let token_type = form.get("subject_token_type").map(String::as_str).unwrap_or(ACCESS_TOKEN_TYPE);
        if token_type != ACCESS_TOKEN_TYPE && token_type != JWT_TOKEN_TYPE {
            return Err(invalid_request("subject token type"));
        }
        if form.contains_key("actor_token") {
            return Err(invalid_request("actor tokens are not accepted"));
        }
        let rule = settings.rules.get(&client_id).ok_or_else(|| refused(400, "unauthorized_client", "client has no rule"))?;
        let started = Instant::now();
        let (verified, source) = verify(form.get("subject_token").map(String::as_str).unwrap_or(""), deps, settings).await?;
        timing.ms.insert("verify".into(), millis(started));
        let verified_at = Instant::now();
        claims = Some(verified.clone());
        subject_allowed(rule, &verified, source)?;
        let requested: Vec<String> = form.get("scope").map(|s| s.split_whitespace().map(String::from).collect()).unwrap_or_default();
        let (audience, scopes) = grant_for(rule, &requested, &verified)?;
        let (access_token, expires_in, jti) = mint(deps, &client_id, &verified, source, &audience, &scopes).await?;
        // mint is the KMS Sign call, all but a fraction of a millisecond.
        timing.ms.insert("mint".into(), millis(verified_at));
        Ok((audience, scopes, access_token, expires_in, jti))
    }
    .await;
    let timing_fields = || {
        let mut fields = Map::new();
        fields.insert("cold".into(), json!(timing.cold));
        fields.insert("ms".into(), Value::Object(timing.ms.clone()));
        fields
    };
    match result {
        Err(rejected) => {
            let claimed = if rejected.claimed.is_empty() { Value::Null } else { json!(rejected.claimed) };
            let line = json!({"route": "/token", "client": client_id, "claimed_client": claimed,
                              "status": rejected.status, "error": rejected.error, "reason": rejected.reason});
            log(with(with(line, subject_fields(claims.as_ref())), timing_fields()));
            respond(rejected.status, &json!({"error": rejected.error}), false)
        }
        Ok((audience, scopes, access_token, expires_in, jti)) => {
            let claims = claims.unwrap_or_default();
            // The new token's jti and the subject's, never who it names: an incident can follow a
            // chain of exchanges back to the Okta token, whose own jti Okta's system log has.
            let line = json!({"route": "/token", "client": client_id, "status": 200, "audience": audience,
                              "scope": scopes.join(" "), "depth": act_depth(&claims) + 1, "jti": jti,
                              "subject_jti": claims.get("jti").and_then(Value::as_str).unwrap_or("")});
            log(with(line, timing_fields()));
            respond(200, &json!({"access_token": access_token, "issued_token_type": ACCESS_TOKEN_TYPE,
                                 "token_type": "Bearer", "expires_in": expires_in, "scope": scopes.join(" ")}), false)
        }
    }
}

pub fn discovery(deps: &Deps, settings: &Settings) -> Value {
    let scopes: BTreeSet<&str> = settings
        .rules
        .values()
        .flat_map(|rule| strings(rule.get("grants")))
        .flat_map(|grant| strings(grant.get("scopes")))
        .filter_map(Value::as_str)
        .collect();
    let issuer = &deps.issuer;
    json!({
        "issuer": issuer,
        "token_endpoint": format!("{issuer}/token"),
        "jwks_uri": format!("{issuer}/jwks.json"),
        // AgentCore reads the document as OpenID Connect discovery, which names an
        // authorization endpoint; this issuer has none, and the path answers 404.
        "authorization_endpoint": format!("{issuer}/authorize"),
        "grant_types_supported": [TOKEN_EXCHANGE],
        "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
        "response_types_supported": ["token"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "scopes_supported": scopes,
    })
}

fn first_text<'a>(candidates: &[Option<&'a Value>]) -> &'a str {
    candidates.iter().flatten().filter_map(|v| v.as_str()).find(|s| !s.is_empty()).unwrap_or("")
}

/// One API Gateway event (REST since D48; HTTP API events too) to its response.
pub async fn handle(event: &Value, deps: &Deps, settings: &Settings, mut timing: Timing) -> Value {
    let method = first_text(&[event.get("httpMethod"), event.pointer("/requestContext/http/method")]);
    let path = first_text(&[event.get("path"), event.get("rawPath")]);
    match (method, path) {
        ("GET", "/.well-known/openid-configuration") => respond(200, &discovery(deps, settings), true),
        ("GET", "/jwks.json") => respond(200, &json!({"keys": [deps.public_jwk]}), true),
        ("POST", "/token") => token(event, deps, settings, &mut timing).await,
        _ => respond(404, &json!({"error": "not_found"}), false),
    }
}

#[cfg(test)]
mod tests;
