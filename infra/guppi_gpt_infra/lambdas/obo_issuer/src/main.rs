//! The issuer in Lambda: loads the issuer URL, the public key and the client secrets during
//! init, then answers API Gateway events (lib.rs decides). A cold start logs one
//! `cold_start` line with each step's start and end in milliseconds from the start of the
//! load, so the log shows what runs in sequence and what in parallel; every /token line
//! carries `ms` and `cold` (guppi-gpt README backlog item 9).

use std::collections::HashMap;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use async_trait::async_trait;
use aws_sdk_kms::primitives::Blob;
use aws_sdk_kms::types::{MessageType, SigningAlgorithmSpec};
use lambda_runtime::{Error, LambdaEvent, service_fn};
use obo_issuer::{Deps, Fetcher, OktaKeys, Settings, Signer, Timing, b64u, handle, keys_by_kid, log, rsa_public_numbers};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use tokio::sync::Mutex;

struct KmsSigner {
    kms: aws_sdk_kms::Client,
    key_id: String,
}

#[async_trait]
impl Signer for KmsSigner {
    async fn sign(&self, message: &[u8]) -> Result<Vec<u8>, String> {
        let out = self
            .kms
            .sign()
            .key_id(&self.key_id)
            .message(Blob::new(message))
            .message_type(MessageType::Raw)
            .signing_algorithm(SigningAlgorithmSpec::RsassaPkcs1V15Sha256)
            .send()
            .await
            .map_err(|e| e.to_string())?;
        out.signature.map(Blob::into_inner).ok_or_else(|| "no signature".into())
    }
}

struct HttpFetcher(reqwest::Client);

#[async_trait]
impl Fetcher for HttpFetcher {
    async fn fetch_json(&self, url: &str) -> Result<Value, String> {
        let response = self.0.get(url).send().await.map_err(|e| e.to_string())?;
        response.error_for_status().map_err(|e| e.to_string())?.json().await.map_err(|e| e.to_string())
    }
}

fn now() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

fn ms(since: Instant, at: Instant) -> u64 {
    at.duration_since(since).as_millis() as u64
}

/// Runs `step`, recording its start and end in milliseconds from `t0`.
async fn timed<T>(steps: &Arc<std::sync::Mutex<Map<String, Value>>>, t0: Instant, name: &str, step: impl Future<Output = T>) -> T {
    let start = Instant::now();
    let out = step.await;
    if let Ok(mut steps) = steps.lock() {
        steps.insert(name.to_string(), json!([ms(t0, start), ms(t0, Instant::now())]));
    }
    out
}

async fn load() -> Result<(Deps, Settings, u64), Error> {
    let t0 = Instant::now();
    let steps = Arc::new(std::sync::Mutex::new(Map::new()));
    let config = timed(&steps, t0, "config", aws_config::load_defaults(aws_config::BehaviorVersion::latest())).await;
    let (kms, ssm, secrets) = timed(&steps, t0, "clients", async {
        (aws_sdk_kms::Client::new(&config), aws_sdk_ssm::Client::new(&config), aws_sdk_secretsmanager::Client::new(&config))
    })
    .await;
    let settings = Settings::from_env()?;
    let key_id = std::env::var("KEY_ID")?;
    let arns: HashMap<String, String> = serde_json::from_str(&std::env::var("CLIENT_SECRET_ARNS")?)?;
    let parameter = std::env::var("ISSUER_PARAMETER")?;

    let issuer_f = timed(&steps, t0, "ssm_issuer", ssm.get_parameter().name(&parameter).send());
    let key_f = timed(&steps, t0, "kms_public_key", kms.get_public_key().key_id(&key_id).send());
    let secret_fs = futures::future::join_all(arns.iter().map(|(client, arn)| {
        let (secrets, steps) = (secrets.clone(), steps.clone());
        async move {
            let out = timed(&steps, t0, &format!("secret_{client}"), secrets.get_secret_value().secret_id(arn).send()).await;
            (client.clone(), out)
        }
    }));
    let parallel_start = Instant::now();
    let (issuer, key, secret_outs) = tokio::join!(issuer_f, key_f, secret_fs);
    steps.lock().map_err(|_| "steps")?.insert("parallel_calls".into(), json!([ms(t0, parallel_start), ms(t0, Instant::now())]));

    let issuer = issuer?.parameter.and_then(|p| p.value).ok_or("issuer parameter has no value")?;
    let spki = key?.public_key.ok_or("no public key")?.into_inner();
    let (n, e) = rsa_public_numbers(&spki).ok_or("unreadable public key")?;
    let kid: String = Sha256::digest(key_id.as_bytes()).iter().map(|b| format!("{b:02x}")).collect::<String>()[..16].to_string();
    let mut client_secrets = HashMap::new();
    for (client, out) in secret_outs {
        let text = out?.secret_string.ok_or("secret without a string")?;
        let value: Value = serde_json::from_str(&text)?;
        let secret = value.get("client_secret").and_then(Value::as_str).ok_or("secret without client_secret")?;
        client_secrets.insert(client, secret.to_string());
    }
    let built_in: Value = serde_json::from_str(obo_issuer::BUILT_IN_OKTA_KEYS)?;
    let okta = OktaKeys { keys: keys_by_kid(&built_in).unwrap_or_default(), fetched_at: now(), refetch_at: 0.0 };
    let http = reqwest::Client::builder().timeout(Duration::from_secs(3)).build()?;
    let deps = Deps {
        issuer: issuer.trim_end_matches('/').to_string(),
        kid: kid.clone(),
        public_jwk: json!({"kty": "RSA", "use": "sig", "alg": "RS256", "kid": kid, "n": b64u(&n), "e": b64u(&e)}),
        signer: Arc::new(KmsSigner { kms, key_id }),
        fetcher: Arc::new(HttpFetcher(http)),
        client_secrets,
        clock: Arc::new(now),
        okta: Arc::new(Mutex::new(okta)),
    };
    let load_ms = ms(t0, Instant::now());
    let steps = steps.lock().map_err(|_| "steps")?.clone();
    log(json!({"event": "cold_start", "load_ms": load_ms, "steps": steps}));
    Ok((deps, settings, load_ms))
}

#[tokio::main]
async fn main() -> Result<(), Error> {
    let (deps, settings, load_ms) = load().await?;
    let (deps, settings) = (Arc::new(deps), Arc::new(settings));
    let first = Arc::new(AtomicBool::new(true));
    lambda_runtime::run(service_fn(move |event: LambdaEvent<Value>| {
        let (deps, settings, first) = (deps.clone(), settings.clone(), first.clone());
        async move {
            // The load runs in init, before the first request; that request carries its time.
            let cold = first.swap(false, Ordering::Relaxed);
            let mut ms = Map::new();
            ms.insert("load".into(), json!(if cold { load_ms } else { 0 }));
            Ok::<Value, Error>(handle(&event.payload, &deps, &settings, Timing { cold, ms }).await)
        }
    }))
    .await
}
