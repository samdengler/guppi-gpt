# Conversation logging

Proposal for backlog item 6: every conversation recorded for long term storage in S3, pseudonymous to the user, with a way for a privileged person to correlate a record back to an account when troubleshooting requires it. Status: built on the branch `backlog/conversation-logging-impl` and shipped dark. The stack creates the bucket, the key, the secret, and the investigator role on the next deploy; the agent writes nothing and the page says nothing until two switches are flipped. Written 3 Sep 2026 against commit 186b01d, revised after review to make the thread the unit of record, and revised again as the build landed. The decisions section at the end holds the answers that settled the open questions, the flip procedure, and the follow-ups.

Vocabulary, used throughout: a thread is one conversation, the AG-UI `threadId` the page generates on load and on New chat; a run is one turn, the AG-UI `runId` the page generates on each send; a session is the AgentCore runtime session, the `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` header the page generates once per page load.

The backlog states a preference for AWS native services, serverless where possible (scale to zero, pay per use, automatic scaling). AGENTS.md adds the rule that no Lambda function sits in the request path or the content sync path. Both hold throughout this proposal.

## Summary of the recommendation

The agent keeps one object per thread at `threads/<threadId>.json` in a dedicated, versioned, SSE-KMS bucket. At the end of each run, from a background task with its own timeout, it reads the thread's current object, merges in the run, and writes the object back: the full thread as the page sent it plus the new reply, the pseudonymous subject, created and updated timestamps, and a compact list of the runs so far. Object versions give the turn-by-turn history. Per-run detail stays on the CloudWatch log line the agent already writes, keyed by the same pseudonym.

The user is identified only by an HMAC-SHA256 of the Cognito sub claim, keyed with a secret the stack generates in Secrets Manager. Re-identification is done by a separate investigator role that can read the bucket, the key, and the user pool. Reading objects by key is the query path until the store is large enough to want Athena over the `threads/` prefix, whose DDL is below. No Firehose, no DynamoDB, no Lambda, and no change to the request path's latency.

## The thread record

`app.py` today builds one dictionary per run as the events pass through `run_agent` and logs it in the `finally` of `event_stream`: hashed sub, session, thread, run, message count, tool call count, token counts, latency to first delta, total latency, outcome. The thread record is built from that dictionary plus the run input and the streamed reply.

| Field | Source | Notes |
| --- | --- | --- |
| `schema_version` | constant | `1`. Bumped when a field changes meaning. |
| `thread` | `RunAgentInput.thread_id` | The object key is derived from it. |
| `session` | the runtime session header | A thread lives inside one page load, so one session; the value of the latest run is kept. |
| `subject` | HMAC of the sub claim | Section below. Set on the first run; later runs of the thread carry the same sub, and a mismatch is logged and the write skipped. |
| `subject_key` | key version | `1` today; tells a rotated key apart later. |
| `created_at` | `started_at` of the first run, UTC ISO 8601 with milliseconds | Carried forward from the existing object (below). |
| `updated_at` | `finished_at` of the latest run | |
| `messages` | the run input's message list, untrimmed, plus the reply | `id`, `role`, `content` in order. The page resends the whole thread on every send, so the list is the conversation as the page holds it. The reply is appended as an assistant message using the `messageId` from `TEXT_MESSAGE_START`; the page assigns its own id to the same text, which shows up in the next run's input. A run with no reply text appends nothing. |
| `runs` | one entry per run, appended | `run`, `started_at`, `model`, `input_tokens`, `output_tokens`, `first_delta_ms`, `total_ms`, `tool_calls` (a count), `outcome`. `error_code` and `dropped_messages` were in the first draft of this table and are follow-ups; the error code is on the stream and the trim count is derivable from the message list. |

Size: a thread object is the thread's text plus a few hundred bytes per run, 2 to 20 KB for a typical thread and about 170 KB at the validation limits (40 messages under 4,000 characters). Every run rewrites the object and versioning keeps the previous body, so a twenty-run thread stores twenty versions whose text overlaps. At a few hundred threads a month that is under 100 MB a month including versions.

### The CloudWatch line, and what moves to it

The agent keeps logging one JSON line per run to the runtime's CloudWatch log group, as section 9 of the design document describes. As built the line is:

| Field | Status |
| --- | --- |
| `sub` | The truncated sha256 while the switch is off; the HMAC pseudonym while it is on, so a CloudWatch line and a thread object share one value. |
| `thread`, `run`, `session`, `messages` (count), `tool_calls` (a count), `input_tokens`, `output_tokens`, `first_delta_ms`, `total_ms`, `outcome` | As before. |
| `started_at`, `model` | New, and written whether the switch is on or off. |

The reply text, the messages, and the retrieval queries are not on the line. The record the agent assembles holds the reply under a key beginning with an underscore, and the function that renders the line drops every such key, so conversation content reaches the bucket and nothing else.

Turning the tool call count into a list carrying each retrieval query was in the first draft. It is a follow-up rather than part of this build: the query is derived from the user's text, so it would put a fragment of conversation content in a second store, and that store has no retention yet.

The line is written first, before the read and the write of the thread object, so it exists even when the S3 write fails.

### Carrying the created time and the prior runs

The run that ends knows only itself. The thread's `created_at` and its earlier run entries have to come from somewhere.

| Source | How | Trust | Cost | Wire format |
| --- | --- | --- | --- | --- |
| A GET of the existing object before the PUT | The background task reads `threads/<threadId>.json`; a missing key means the first run (`created_at` is now, `runs` is empty). The PUT carries `If-Match` with the ETag from the GET, or `If-None-Match: *` on the first run, so a concurrent writer is detected rather than overwritten. | Everything in the record was written by the agent. | One small GET per run (a few KB), in the background task, after the stream has ended. The runtime role gains `GetObject` on `threads/*` and `kms:Decrypt` on the bucket key. | Unchanged. |
| The page sends them | The agent emits the run entry as a CUSTOM event at run end; the page stores the entries and the created time in memory and returns them in `forwardedProps` on the next send. | The client is the source of audit data: token counts, latencies, and outcomes of earlier runs are whatever the page says. Every field needs validation and caps against a hostile client. | No read. The runtime role stays write-only. | A new event and a new request property, both to be validated. |

One thread runs one turn at a time, since the page disables send during a run and a thread id never leaves the page that made it, so there is no concurrent writer in normal operation; the conditional headers cost nothing and catch the abnormal case.

Recommendation: the GET before the PUT. The record then contains only what the agent observed, the wire format does not change, and the read is one request against an object the task is about to rewrite. The cost is that the runtime role can now read thread objects. Two things bound that: the role has no `ListBucket`, so a process in the container can read only objects whose UUID key it already knows, and CloudTrail data events record every read, where a `GetObject` without a `PutObject` of the same key seconds later is the anomaly to alarm on.

When the GET fails for a reason other than a missing key (a timeout, a permission error), the task logs a warning and writes nothing: overwriting the object with only the current run would erase the run list, and the page resends the whole thread, so the next run's write restores the text. The CloudWatch line for the failed write's run is already written. A `subject` that differs from the one in the existing object is treated the same way.

### What is excluded from both stores

| Excluded | Reason |
| --- | --- |
| The bearer token | A credential, valid for up to sixty minutes for the runtime and the tools gateway. The existing test asserts it never appears in the log line; the same assertion covers the thread record. |
| The email address | Personal data, and not available to the agent: the access token carries `sub`, `username`, `client_id`, and scopes; the email is in the ID token, which the page never sends. Re-identification reaches the email through the user pool, where it already is. |
| The raw sub claim | The stable identifier Cognito uses for the account. Storing it would make every record identifiable to anyone who can read the pool. The HMAC replaces it. |
| The Cognito `username` | For a federated user it is `google_<google account id>`, as identifying as the sub. |
| Tool results (the retrieved passages) | Bulk, and already in the content bucket at the revision the seed script pinned. The tool name and query are enough to rerun the retrieval. |
| The system prompt text | In the repository, versioned; the line carries its hash. |
| Request headers, viewer address | Not needed to troubleshoot a run; the viewer address never reaches the container in any case. |
| `TOOL_CALL_RESULT` content, `CUSTOM` ping events | Stream noise that carries nothing about the conversation. |

## Anonymity and re-identification

Pseudonymity is the accurate word: records can be linked to one another by the subject value, and to a person by whoever holds the key and can read the user pool. The design below makes the second step require a role nothing in the request path has.

### The subject value: keyed hash versus the plain hash used today

`subject_hash` in `app.py` takes the first 12 hex characters of `sha256(sub)`. A Cognito sub is a random UUID, so the hash cannot be inverted by enumerating a small input space. Its weakness is different: the sub is not a secret inside the AWS account. Any principal with `cognito-idp:ListUsers` on the pool can list every sub, hash each one, and link every record to an account with no further permission. The user can do the same for their own records from their token. Truncation to 48 bits adds nothing to privacy and introduces a collision risk.

An HMAC-SHA256 of the sub with a secret key removes that path: linking a record to an account requires the key, and the key can be held where the pool readers and the log readers are not. The output is 64 hex characters; the record keeps 32 (128 bits), enough to avoid collisions and short enough to type.

| Property | Truncated sha256 (today) | HMAC-SHA256 with a held key |
| --- | --- | --- |
| Linking records to an account needs | Read access to the user pool | The key, plus read access to the user pool |
| Compromise of a log reader yields | Pseudonyms that anyone with pool access can resolve | Pseudonyms only |
| Compromise of the runtime container yields | Nothing new | The key (it must be in process memory to compute the value) |
| Anonymizing the whole store at once | Impossible; the hash is recomputable forever | Destroy the key |
| Cost | None | One secret, USD 0.40 a month |

### Where the key lives

| Option | Secrets Manager secret, HMAC computed in the agent | KMS HMAC key, `GenerateMac` per run |
| --- | --- | --- |
| Key material | Generated by the stack (`generate_secret_string`, 64 characters), the same pattern as `OriginVerifySecret` | Never leaves KMS |
| Runtime needs | `secretsmanager:GetSecretValue` on one ARN, read once per process and cached | `kms:GenerateMac` on one key, one API call per run |
| Latency on the request path | None; the read happens in the background task on the first run of a container | None; the call happens in the background task |
| Investigator needs | `GetSecretValue` on the same secret, then `hmac.new` locally | `kms:GenerateMac` on the key |
| Rotation | A new secret version; `subject_key` tells the versions apart | Automatic rotation is not offered for HMAC keys; manual, same field |
| Cost | USD 0.40 a month | USD 1.00 a month plus USD 0.03 per 10,000 calls |
| Audit of key use | CloudTrail records `GetSecretValue` per container start | CloudTrail records every `GenerateMac`, including the investigator's |

Recommendation: the Secrets Manager secret. The stack already generates a secret this way, the agent already has boto3 through Strands, and one read per container start is the least the request path can do. The KMS HMAC key is the upgrade if the key must never sit in the container's memory; the agent change would be confined to the function that computes the subject.

### The mapping from subject to account

The subject value is one-way. To get from a subject to a person, something must hold the other direction.

| Design | How it works | Written by | Read by | What it adds |
| --- | --- | --- | --- | --- |
| Enumerate the pool | The investigator lists the users of the pool (`ListUsers`, paginated), computes the HMAC of each sub with the key, and keeps the match. Person to subject is one call: `ListUsers` filtered by email, then one HMAC. | Nobody; the pool is the mapping | Investigator role | No new store. The investigator holds the key. O(number of users) per lookup, seconds for a pool of a few thousand. A user deleted from the pool becomes unlinkable. |
| DynamoDB table | Partition key `subject`; attributes `sub`, `username`, `first_seen`, `last_seen`, `threads`. The agent upserts the item in the same background task. | Runtime role (`PutItem`, `UpdateItem` only) | Investigator role (`GetItem`, `Query`) | On-demand table, scales to zero, well under a cent a month. Keeps the key out of the investigator's hands. Adds a second store of the sub to protect and clean on deletion. The email is still not in it; the agent never sees it, so the pool supplies it either way. |
| S3 object per user | The same content at `subjects/<subject>.json`, written once when absent. | Runtime role | Investigator role | No new service, but a conditional put per run for no gain over the table. Set aside. |

A write at sign-in time was ruled out: it would be a Cognito post-authentication trigger, which is a Lambda function. First run is the only code path this design has.

Recommendation: start with enumeration through the pool, and add the DynamoDB table if the pool grows past a few thousand users, if lookups become frequent enough to want `last_seen` and counters, or if the key should stay out of the investigator's hands. The table is additive: the background task gains one `UpdateItem`, and the procedure below replaces the enumeration step with a `GetItem`. Open question 2.

### The investigator role

A stack-defined IAM role, `ConversationInvestigatorRole`, is the only principal outside the runtime that can read thread objects. Its trust is the account root, so any principal in the account holding `sts:AssumeRole` can assume it; the `InvestigatorPrincipalArn` parameter narrows the trust to one ARN when it is set. The maximum session is one hour. Every assumption and every read is in CloudTrail, which is the audit trail of re-identification.

As built the role holds exactly this and nothing else:

| Service | Actions | Resource |
| --- | --- | --- |
| S3 | `GetObject`, `GetObjectVersion` | `threads/*` in the log bucket |
| S3 | `ListBucket`, `ListBucketVersions` | The log bucket |
| KMS | `Decrypt` | The log bucket key |
| Secrets Manager | `GetSecretValue` | The subject key secret |
| Cognito | `ListUsers` | The user pool |

The role has no delete permission. A data subject deletion is an administrative act carried out with the deploy credentials, so that a stolen investigator session cannot erase history. It has no Athena or Glue permission either, because neither is a stack resource; the DDL below is what to create the day a query engine is wanted, and the role gains the query permissions in the same change.

### From a run id to a person, and back

The exact commands are in the re-identification procedure near the end of this document. In outline: the CloudWatch line for a run carries the thread id and the subject, so the object is `threads/<thread>.json` and the person is found by listing the user pool and computing the HMAC of each sub until one matches. The reverse direction, from an email address to the records, is one `ListUsers` filtered by email, one HMAC, and a scan of the objects for that subject.

To see a thread as it stood after a particular run, `aws s3api list-object-versions --prefix threads/<thread>.json` lists one version per run, and `get-object --version-id` fetches it.

### Retention per store

| Store | Holds | Retention | Mechanism |
| --- | --- | --- | --- |
| Log bucket, `threads/` | Thread objects and their versions | 30 days | Lifecycle rule `ExpireThreadRecords`: expire current versions 30 days after the write, expire noncurrent versions 30 days after they became noncurrent, abort incomplete multipart uploads after one day |
| CloudWatch runtime log group | The line per run: identifiers, counts, latencies, tool call count | None today | A follow-up. The group belongs to the service, which creates it on first use |
| Subject key secret | The key | Life of the system | Destroying it is the emergency anonymization of every record at once |
| Cognito user pool | sub, email, name | Until the user is deleted | Existing |

The lifecycle rule does not carry `ExpiredObjectDeleteMarker`: CloudFormation refuses it in the same rule as an expiration in days, and a second rule for delete markers is not worth its line until objects have actually expired, which is two years away.

### What a data subject deletion takes

1. Find the sub from the email (`ListUsers`), compute the subject.
2. List the person's thread ids: with no query engine this is a read of every object under `threads/` and a match on `subject`, which is seconds for a store of a few thousand small objects.
3. For each thread, delete every version of `threads/<thread>.json` (`list-object-versions` on the key, then `delete-objects` with the version ids). One key per thread, plus its versions.
4. Delete the Cognito user. After this the subject is unlinkable even to the key holder.
5. CloudWatch lines carrying the subject expire when the log group gains a retention (a follow-up).

Steps 2 and 3 are a short script, run with the deploy credentials rather than the investigator role, which holds no delete permission.

## Write path options

All options assemble the run in the agent; the run's data is complete only when the stream ends (`RUN_FINISHED`, `RUN_ERROR`, or the client disconnecting), so nothing can be written earlier and nothing about streaming changes. The thread record is a read-modify-write of one object, which rules out any path that only appends.

| | Runtime reads and writes S3 | Runtime puts to Firehose, S3 delivery | CloudWatch Logs subscription to Firehose | EventBridge to Firehose |
| --- | --- | --- | --- | --- |
| Produces one object per thread | Yes, by design | No. Firehose appends records into batch objects. A thread object would need a consumer to fold runs together: a Lambda on delivery, or a scheduled Athena CTAS rewrite | No, as Firehose | No, as Firehose |
| Lambda in the request path | None | None, but a Lambda after delivery to build thread objects | Same | Same |
| Scale to zero | Yes | Yes; no hourly charge, per GB with 5 KB rounding per record | Yes | Yes |
| Cost at 300 runs a day | 9,000 GETs and 9,000 PUTs a month, about USD 0.05; storage under 100 MB a month including versions | Under USD 0.01 ingestion, plus whatever folds the runs | CloudWatch ingestion at USD 0.50 per GB rises with the content; Firehose as left | USD 1.00 per million events; Firehose as left |
| Failure isolation | Background task after the stream ends, own timeout; a failure is a warning. The CloudWatch line is written before the S3 calls | The agent only puts a record; delivery failures are Firehose's, retried for 24 hours | Complete: the agent only logs | Same as Firehose |
| Delivery delay | Seconds | Buffer interval, 0 to 900 s, plus the fold | More | More |
| Per-user deletion | Delete each thread key with its versions | Rewrite every batch object holding one of the user's runs | Same, and content also sits in CloudWatch Logs | Same as Firehose |
| Content also lands in | Nowhere else | Nowhere else | CloudWatch Logs, a second store to protect and expire | The EventBridge archive, if one is on |
| New stack resources | Bucket, key, secret, investigator role, Glue table, Athena workgroup | The same plus a delivery stream, its role, and the fold | The same plus a stream, a log group created ahead of the runtime, a subscription filter, and the fold | The same plus a stream, a rule, its role, and the fold |
| New runtime IAM | `GetObject` and `PutObject` on `threads/*`, `Encrypt` and `Decrypt` on the key, `GetSecretValue` | `firehose:PutRecord`, `GetSecretValue` | `GetSecretValue` | `events:PutEvents`, `GetSecretValue` |

With the thread as the unit of record the comparison is short. The three streaming paths deliver per-run records and would need a second component to fold them into thread objects; that component is a Lambda function or a scheduled query, and either is more machinery than the one GET and one PUT the runtime can do itself. They stay as the answer if the unit of record ever returns to the run at a volume where a million small objects a year matter.

### Recommendation

The runtime reads and writes the thread object directly. The write happens in a background task scheduled when the stream has finished, with a ten-second timeout covering the key read, the GET, and the PUT; its failure is a warning in CloudWatch and never an error on the stream. The agent's sink is a two-method object (`get`, `put`), so the storage can change without touching record assembly.

## S3 layout, Athena table, and bucket settings

### Object layout

One key per thread, `threads/<threadId>.json`, one JSON object per file, terminated by a newline. The thread id is a page-generated UUID, so the key space cannot be enumerated without `ListBucket`.

No date partition. Partition projection on a `dt=` prefix helps Athena skip objects when a store holds hundreds of thousands of them; at a few hundred threads a month the whole prefix is a few thousand objects after a year and Athena lists it in well under a second, and a full scan of 50 MB a year costs less than the 10 MB per-query minimum. A partition would also put the created date into the key, so the agent would need it before the GET, and queries by updated date would still scan everything. Dates live inside the record as `created_at` and `updated_at`; when the object count nears 100,000, a `dt=` prefix by created date can be added and the table redefined without touching the record.

### Athena table

Neither the Glue table nor the Athena workgroup is a stack resource. A store that holds nothing yet does not need a query engine, the workgroup would put a second copy of the content into an `athena/` prefix with its own lifecycle and its own policy statements, and a first investigation reads a handful of objects by key. The DDL below is what to create when the store is large enough that reading objects one at a time stops working; it matches the record the agent writes today. The database is `guppi_gpt` and the table `threads`:

    CREATE EXTERNAL TABLE guppi_gpt.threads (
      schema_version int,
      thread         string,
      session        string,
      subject        string,
      subject_key    int,
      created_at     string,
      updated_at     string,
      messages       array<struct<id:string, role:string, content:string>>,
      runs           array<struct<
                       run:string, started_at:string, model:string,
                       input_tokens:int, output_tokens:int,
                       first_delta_ms:int, total_ms:int,
                       tool_calls:int, outcome:string>>
    )
    ROW FORMAT SERDE 'org.openx.data.jsonserde.JsonSerDe'
    WITH SERDEPROPERTIES ('ignore.malformed.json' = 'true')
    LOCATION 's3://<log bucket>/threads/';

Queries the investigator will run most:

    SELECT t.thread, t.updated_at, r.run, r.outcome, r.total_ms
    FROM guppi_gpt.threads t CROSS JOIN UNNEST(t.runs) AS x(r)
    WHERE t.updated_at >= '2026-09-03' AND r.outcome <> 'finished';

    SELECT thread, created_at, cardinality(runs) AS runs,
           messages[1].content AS first_question
    FROM guppi_gpt.threads
    WHERE subject = '<subject>' ORDER BY created_at;

    SELECT m.role, m.content
    FROM guppi_gpt.threads t CROSS JOIN UNNEST(t.messages) WITH ORDINALITY AS x(m, i)
    WHERE t.thread = '<thread>' ORDER BY i;

### Bucket settings

| Setting | Choice | Reason |
| --- | --- | --- |
| Block public access | All four on | As the other two buckets |
| Encryption | SSE-KMS with a stack-created customer managed key, S3 bucket keys on | A second gate on reads: a principal with `s3:GetObject` but no `kms:Decrypt` on this key gets nothing, and every decrypt is a CloudTrail event. Bucket keys keep the KMS request cost at a few cents. SSE-S3 would cost nothing extra but gives no gate beyond the bucket policy |
| Enforce SSL | On | As the other buckets |
| Versioning | On | Each run overwrites the thread object; the versions are the turn-by-turn history and the record of what a user saw after each run. A deletion is one key plus its versions |
| Object lock | Off | Retention is a policy choice, not a compliance hold |
| Lifecycle | `threads/`: expire current versions after 30 days, noncurrent versions 30 days after they became noncurrent; abort incomplete multipart uploads after 1 day | Transition to Glacier Instant Retrieval is possible, but at under 100 MB a month it saves under a cent a year. Expiry is the rule that matters |
| Removal policy | Retain | As the other buckets |
| Access logging | CloudTrail S3 data events for this bucket only | Records every read and write by principal. Not in this stack; a follow-up |

Bucket policy statements as built, in the order CDK renders them:

1. `enforce_ssl` denies any request without TLS (`aws:SecureTransport` false).
2. `RuntimeThreadRecords` allows `GetObject` and `PutObject` on `threads/*` to the runtime role. No `ListBucket`, no delete, no version reads.
3. `InvestigatorReads` allows `GetObject`, `GetObjectVersion`, `ListBucket`, and `ListBucketVersions` to the investigator role.
4. `DenyOtherReaders` denies `GetObject` and `GetObjectVersion` on `threads/*` to every principal whose `aws:PrincipalArn` is neither of those two roles. The deny is scoped to reads, so an administrator keeps the ability to repair the policy and to delete objects for a data subject deletion.

There is no separate deny on unencrypted puts: the bucket's default encryption is the KMS key, and the agent sends `ServerSideEncryption: aws:kms` on every put in any case.

The KMS key policy mirrors the bucket policy: decrypt and data key generation for the runtime role, decrypt for the investigator role, administration for the account root.

## Changes to the design document, the page, and the decision log

The design document and the decision log are the source of truth and change in the same commit as the code (AGENTS.md). This branch leaves both untouched: the edits are listed here to be integrated on merge. Each requirement below describes the system with the runtime switch on, which is the state the design document should describe once the switch is flipped.

### Design document

| Section | Change |
| --- | --- |
| 2, Conversation | The requirement "When the page is reloaded or closed, the system shall discard the conversation. No message shall be persisted on the server or in browser storage." splits. The page half stays: "When the page is reloaded or closed, the system shall discard the conversation; no message shall be persisted in browser storage." The server half is replaced by a new group. |
| 2, new group Logging | "When a run ends, the agent shall write the thread's record (the messages as the page sent them plus the reply, the pseudonymous subject, created and updated times, and one entry per run with its model, token counts, latencies, tool call count, and outcome) to the conversation log bucket, keeping the previous version." "The record shall identify the user only by a keyed hash of the subject claim and shall not contain the bearer token, the email address, or the subject claim." "If the record cannot be read or written, then the agent shall log the failure and shall not change the run's events or outcome." "The system shall let only the investigator role read the conversation log and the subject key; the runtime shall read and write thread records and shall not list them." "The system shall expire thread records 30 days after their last write." |
| 4, component notes | New rows: Conversation log bucket (SSE-KMS, versioned, one object per thread under `threads/`, lifecycle, bucket policy), Subject key (Secrets Manager, generated by the stack, read once per container), Investigator role (trust, permissions, CloudTrail as audit). The runtime row gains the three environment variables and the grants. Figure 3 gains a dashed edge from the runtime to the log bucket; the caption's "No Lambda function is present" stays true. |
| 5, Request flow | One sentence after the paragraph on the JWT: when the stream has ended the agent reads the thread's object, merges the run, and writes it back from a background task; nothing on the stream waits for it. The paragraph "The browser owns the conversation" gains: the server keeps a pseudonymous copy for troubleshooting, which the browser never reads. |
| 9, Agent design | Step 6 becomes: log the run line (now with the start time, the model id, and the HMAC subject) and, when the stream has ended, read, merge, and write the thread record from a background task with a ten-second timeout. The paragraph on the system prompt notes that the sentence about saving follows the logging switch. |
| 11, Security and cost limits | New control row: Conversation log access, bounding who can read stored threads, at the bucket policy, the KMS key policy, and the investigator role. A paragraph after the table stating that the log bucket is now the most sensitive data in the system, that the runtime reads and writes single thread objects by key and cannot list them, and that the key and the pool together are what re-identify a record. |
| 12, Decisions and alternatives | New rows: Unit of record (one object per thread, versioned; alternative one object per run; reason: a thread is what a troubleshooter reads, versions keep the turns, deletion is one key; reversibility: moderate, the record shape and the table change). Write path (direct read-modify-write from a background task; alternatives Firehose, CloudWatch subscription, EventBridge, all of which need a fold step; reversibility: easy, the sink is one class). Carrying the thread's history (GET before PUT; alternative the page returning it; reason: the agent is the only source of the run entries). Subject pseudonym (HMAC with a Secrets Manager key; alternatives plain sha256, KMS HMAC). Subject mapping (pool enumeration by the investigator; alternative DynamoDB table). |
| 14, Risks | New row: the log bucket holds every conversation; effect, a read by the wrong principal exposes users' questions; mitigation, SSE-KMS, the bucket policy deny, no `ListBucket` for the runtime, the investigator role, expiry at 30 days, and CloudTrail data events once they are added. |
| 15, Next steps | The item is removed once built. The README backlog item 6 is marked done in the same commit. |

### The page's privacy notice

Two strings said nothing is kept: the empty state's "Ask anything. This conversation is not saved." and the composer hint's "Nothing is saved." Both would be wrong the moment the runtime switch flips, so the wording now comes from `web/src/copy.js` and follows two page flags. `logging` is this feature. `history`, which would keep chats in browser storage, is not built; its wording is decided here so that the two sentences do not have to be rewritten when it is.

| `history` | `logging` | Composer hint, after "Enter to send, Shift+Enter for a new line." | Empty state, after "Ask anything." |
| --- | --- | --- | --- |
| off | off | Nothing is saved. | This conversation is not saved. |
| off | on | Conversations are logged for troubleshooting. | Conversations are logged for troubleshooting. |
| on | off | Chats are saved on this device. | Chats are saved on this device. |
| on | on | Chats are saved on this device and logged for troubleshooting. | Chats are saved on this device and logged for troubleshooting. |

The two rows with `history` on were not part of the answer to the open question; they follow its sentence for the combined case, so that every combination of the flags reads as one voice. The sign-in screen gets no extra sentence: any Google account can sign in and is recorded from the first run, but the notice a person needs is the one beside the box they type into.

The page copy addresses the user directly, which the writing rule for docs and comments does not cover.

The system prompt in `agent.py` says "Nothing is saved between page loads, and you cannot recall earlier sessions." The model may repeat that as a privacy claim, so the sentence now follows the runtime switch: with logging on it reads "Conversations are logged for troubleshooting; nothing is saved between page loads, and you cannot recall earlier sessions." The prompt keeps its length.

The README's opening line and the design document's problem statement describe the chat as stateless with no history. Both remain true for the user experience; the README gains one sentence under Status naming the log bucket and the investigator role.

### Decision log entry

Revision history row, to be numbered at build time:

> Conversation logging built behind two switches, both off: a KMS-encrypted, versioned log bucket with one JSON object per thread under `threads/`, read, merged, and written by the runtime from a background task after each run's stream ends; the subject pseudonym changed from a truncated sha256 to an HMAC keyed by a stack-generated secret whenever the runtime switch is on; per-run detail kept on the CloudWatch line; an investigator role as the only reader of the bucket, the key, and the user pool; expiry 30 days after the last write. The page's notice moved into `web/src/copy.js` behind a `logging` flag, and the system prompt's sentence about saving follows the runtime switch. Trigger: backlog item 6, proposal in `docs/proposals/conversation-logging.md`. One object per run was the first draft and was replaced by the thread on review; Firehose, a CloudWatch Logs subscription, and EventBridge were set aside because each would need a fold step to produce thread objects; a DynamoDB subject mapping was set aside in favor of enumerating the user pool with the key; Athena and a Glue table were set aside until the store is large enough to need them, with the DDL kept in the proposal.

Entry under Decisions that were reversed:

> No message persisted on the server. Revisions 1 to 12 required that no message be persisted on the server; the page's empty state and hint said so. Revision N records every thread in S3 for troubleshooting, pseudonymously, with re-identification confined to the investigator role. The browser-side half of the requirement (nothing in browser storage, the page forgets on reload) is unchanged. The earlier position stays the right one for a deployment that must make no record at all; this one trades it for the ability to see what a user saw when a reply was wrong.


## What the build added

Committed on `backlog/conversation-logging-impl`. `uv run -- pytest`, `uv run -- ruff check .`, `cd infra && uv run -- cdk synth -c image_uri=...` without Docker, and `cd web && npm ci && npm test && npm run build` all pass.

### Stack resources

`infra/guppi_gpt_infra/stack.py`, a section between the knowledge base and the tools gateway.

| Logical id | Type | What it is |
| --- | --- | --- |
| `ConversationLogKey` | `AWS::KMS::Key` | Customer managed key, rotation on, alias `guppi-gpt-conversations`, retained |
| `ConversationLogBucket` | `AWS::S3::Bucket` | Versioned, SSE-KMS with that key and bucket keys on, block all public access, TLS enforced, lifecycle `ExpireThreadRecords` at 30 days, retained |
| `ConversationLogKeySecret` | `AWS::SecretsManager::Secret` | The HMAC key: 32 random characters, no JSON template, so the secret value is the key |
| `ConversationInvestigatorRole` | `AWS::IAM::Role` | The reader, one hour maximum session, trust from the account root or from `InvestigatorPrincipalArn` |

Constants at the top of the file: `CONVERSATION_LOG_ENABLED = True` (since 4 Sep 2026), `CONVERSATION_RETENTION_DAYS = 30`, `THREADS_PREFIX = "threads/"`. Outputs: `ConversationLogBucketName`, `ConversationLogKeySecretArn`, `InvestigatorRoleArn`. The parameter `InvestigatorPrincipalArn` defaults to the empty string, and `scripts/deploy.sh` passes it from `GUPPI_INVESTIGATOR_ARN` when that variable is set, the way it passes `AlarmEmail` from `GUPPI_ALARM_EMAIL`.

Encryption is SSE-KMS rather than SSE-S3. The key is a second gate on reads: a principal holding `s3:GetObject` and nothing else reads nothing, and every decrypt is a CloudTrail event. It costs the investigator role one `kms:Decrypt` grant, which is not a harder path.

The runtime role gains `s3:GetObject` and `s3:PutObject` on `threads/*` with no `ListBucket` and no delete, `kms:Decrypt` and `kms:GenerateDataKey` on the key, and `secretsmanager:GetSecretValue` on the one secret.

### Agent

`agent/src/guppi_agent/conversation_log.py` holds the record and the write. `subject_from_token` decodes the unverified claims and returns the first 32 hex characters of the HMAC; `load_key` reads the secret once per container and caches it; `merge` builds the record from the existing object, the run input, and the run's log record; `write_record` does the GET, the merge, and the conditional PUT, retrying once when another writer won; `schedule_write` starts the whole thing as an independent task with a ten second timeout, and `drain` exists for the tests.

`app.py` collects the reply text and the reply message id under keys beginning with an underscore, records `started_at` and `model`, and in the `finally` of `event_stream` either schedules the write (switch on) or logs the run line as it always did (switch off). The background task writes the line itself once it holds the pseudonym, so a line and a record always carry the same subject. `subject_hash` stays for the switched-off path.

The ten second timeout is longer than a key read, a GET, and a PUT of a small object should take, and far shorter than the runtime's idle timeout, so a write completes or fails before the session can be reclaimed. A `client_disconnected` run is logged and not merged.

Tests are in `agent/tests/test_conversation_log.py`, against a fake S3 object with `get_object` and `put_object` rather than a new dependency: the first write and its `IfNoneMatch`, the merge and its `IfMatch`, one retry after a conflict and then the warning, a read failure, a write that outlives the timeout, the switch off writing nothing and keeping the twelve character hash, a disconnect, the key read happening once, and the prompt sentence following the switch.

### Page

`web/features.json` holds the page switches. `web/src/copy.js` holds the hint and empty state wording for every combination of them, `web/src/features.js` reads the flags and returns the two strings, and `app.js` writes them into the page at boot. The HTML still carries the switches-off wording, so the page reads correctly before the script runs. `npm test` runs the copy through `node --test`.

## Switches and variables

| Switch | Where | Default | Effect |
| --- | --- | --- | --- |
| `CONVERSATION_LOG_ENABLED` | `stack.py` constant, reaching the container as an environment variable | `False` | Whether the agent writes thread records, uses the HMAC pseudonym on the run line, and tells the model that conversations are logged |
| `logging` | `web/features.json` | `false` | Whether the page's hint and empty state say that conversations are logged |
| `history` | `web/features.json` | `false` | Reserved for browser-stored chats, which are not built; the copy for it is decided |

Environment variables on the runtime, all three set whether the switch is on or off:

| Variable | Value |
| --- | --- |
| `CONVERSATION_LOG_ENABLED` | `"true"` or `"false"` from the stack constant |
| `CONVERSATION_LOG_BUCKET` | The log bucket's name |
| `CONVERSATION_LOG_KEY_SECRET_ARN` | The HMAC secret's ARN |

## Turning logging on

The runtime switch goes first and the page switch second, so that the page never promises a record that does not exist. The reverse order would leave a window in which the page says conversations are logged while nothing writes them.

1. Set `CONVERSATION_LOG_ENABLED = True` in `infra/guppi_gpt_infra/stack.py`.
2. Run `scripts/deploy.sh`. The runtime takes the new environment variable, and the next run writes `threads/<threadId>.json`.
3. Confirm the write: a run line in the runtime's log group whose `sub` is 32 characters rather than 12, and an object under `threads/` (read it as the investigator role, below).
4. Set `"logging": true` in `web/features.json`.
5. Run `scripts/deploy.sh` again, or build and sync the page alone. The hint and the empty state name the logging.

Turning it off is the same two edits in the other order: the page first, then the runtime.

## Re-identifying a subject

The investigator role is not assumed by anything automatically; every step below is a person at a terminal, and every step is in CloudTrail. `ROLE` is the `InvestigatorRoleArn` output, `BUCKET` the `ConversationLogBucketName` output, `SECRET` the `ConversationLogKeySecretArn` output, and `POOL` the `UserPoolId` output.

1. Assume the role.

        creds=$(aws sts assume-role --role-arn "$ROLE" \
          --role-session-name conversation-investigation --duration-seconds 3600)
        export AWS_ACCESS_KEY_ID=$(jq -r .Credentials.AccessKeyId <<<"$creds")
        export AWS_SECRET_ACCESS_KEY=$(jq -r .Credentials.SecretAccessKey <<<"$creds")
        export AWS_SESSION_TOKEN=$(jq -r .Credentials.SessionToken <<<"$creds")

2. Read the thread. The run line in CloudWatch names the thread, so the key is known; `aws s3 ls` finds it when only the subject is known.

        aws s3api get-object --bucket "$BUCKET" --key "threads/$THREAD.json" thread.json
        jq '{subject, created_at, updated_at, runs: [.runs[].run]}' thread.json

   For the thread as it stood after an earlier run:

        aws s3api list-object-versions --bucket "$BUCKET" --prefix "threads/$THREAD.json"
        aws s3api get-object --bucket "$BUCKET" --key "threads/$THREAD.json" \
          --version-id "$VERSION" earlier.json

3. Read the key.

        key=$(aws secretsmanager get-secret-value --secret-id "$SECRET" \
          --query SecretString --output text)

4. Find the account behind the subject by enumerating the pool. There is no mapping table: the pool is the mapping, and the key is what turns one into the other.

        subject=$(jq -r .subject thread.json)
        aws cognito-idp list-users --user-pool-id "$POOL" --max-results 60 \
          --query 'Users[].Attributes' --output json > users.json
        python3 - "$key" "$subject" <<'PY'
        import hashlib, hmac, json, sys
        key, subject = sys.argv[1].encode(), sys.argv[2]
        for attributes in json.load(open("users.json")):
            values = {a["Name"]: a["Value"] for a in attributes}
            sub = values.get("sub", "")
            if hmac.new(key, sub.encode(), hashlib.sha256).hexdigest()[:32] == subject:
                print(values.get("email"), sub)
        PY

   `list-users` pages: pass `--pagination-token` from the previous answer until it stops coming back. At a few thousand users this is seconds.

5. The reverse direction, from a person to their records, filters the pool first and then matches the objects.

        sub=$(aws cognito-idp list-users --user-pool-id "$POOL" \
          --filter "email = \"$EMAIL\"" \
          --query 'Users[0].Attributes[?Name==`sub`].Value' --output text)
        subject=$(python3 -c 'import hashlib,hmac,sys;
        print(hmac.new(sys.argv[1].encode(), sys.argv[2].encode(), hashlib.sha256).hexdigest()[:32])' \
          "$key" "$sub")
        aws s3api list-objects-v2 --bucket "$BUCKET" --prefix threads/ \
          --query 'Contents[].Key' --output text | tr '\t' '\n' | while read -r key_name; do
          aws s3api get-object --bucket "$BUCKET" --key "$key_name" /dev/stdout \
            | jq -r --arg s "$subject" 'select(.subject == $s) | .thread'
        done

   The last loop is the step a Glue table and Athena would replace once the store is large; the DDL is above.

6. Unset the three environment variables when finished, or open a new shell. The session expires in an hour in any case.

## Verifying the first deploy by hand

1. Deploy with the switch off. Confirm the bucket, the key, the secret, and the role exist, that the runtime carries the three variables, and that no object appears under `threads/`.
2. Flip the runtime switch and deploy. In the browser, run one thread of three turns including a question that triggers retrieval, then New chat and one more turn.
3. As the investigator role, read both objects. Check `messages` (six entries for the first thread, the last of them the assistant reply), `runs` (three entries with tokens and timings), `subject` (32 hex characters), `created_at` before `updated_at`, and the absence of the token, the email, and the raw sub. Fetch the first version of the first object and confirm it holds two messages and one run.
4. As the deploy role, the same read is denied.
5. In the runtime's log group, the run lines carry the same `thread` and `sub` values as the objects and no message text.
6. Failure isolation: point `CONVERSATION_LOG_BUCKET` at a bucket the role cannot reach, run one turn, and confirm the reply streams normally and one warning appears. Restore it, run another turn, and confirm the object holds the restored run and not the failed one, whose log line exists.
7. Timing: compare `total_ms` for the same question before and after the flip. The difference should be noise, since the read and the write start after the last event.
8. Flip the page switch, deploy the page, and read the hint and the empty state.

## Decisions

The answers that settled the open questions, given 3 Sep 2026.

1. **Retention.** 730 days for current and noncurrent versions, in one lifecycle rule on `threads/`.
2. **Subject mapping.** No table. Re-identification enumerates the user pool and computes the HMAC of each sub with the key, as the procedure above spells out. A DynamoDB table stays available if the pool grows or lookups become frequent.
3. **Key custody.** A Secrets Manager secret generated by the stack, 32 random characters with no template, read once at container start and cached in the process. The variable naming it is `CONVERSATION_LOG_KEY_SECRET_ARN`, and the runtime role holds `secretsmanager:GetSecretValue` on that one secret.
4. **Page notice.** The wording table in the privacy notice section. The sign-in screen gets no extra sentence. The system prompt's claim becomes conditional on the runtime switch and stays one sentence.
5. **Investigator trust.** The stack defines the role. It trusts the account root by default, so any principal in the account with `sts:AssumeRole` can assume it; `InvestigatorPrincipalArn`, when set, narrows the trust to that ARN. `deploy.sh` passes it from `GUPPI_INVESTIGATOR_ARN`.
6. **CloudTrail data events.** Not in this stack. A follow-up.
7. **Disconnected runs.** A `client_disconnected` run is not merged into the thread. Its CloudWatch line already records it, and the messages it would carry arrive again with the next run.
8. **Runtime log group retention.** Skipped. The service creates the group, and taking it over from the stack is a follow-up.
9. **Retrieval queries.** They stay on the CloudWatch line and out of the thread object. The line does not carry them yet; adding them is the follow-up below.

## Follow-ups

| Follow-up | Why it was left out |
| --- | --- |
| CloudTrail data events for the log bucket | Neither a trail in this stack nor a selector on an account trail is decided; the bucket is empty until the switch flips |
| Retention on the runtime's CloudWatch log group | The service creates the group on first use; the stack would have to create it first under the service's naming pattern, which needs checking against a live deploy |
| The retrieval query on the run line, as `tool_calls` with names and arguments | It puts conversation content in a store with no retention; worth doing with the retention above |
| `error_code` and `dropped_messages` in the run entries | Neither is needed to read a thread, and both are recoverable from the stream and the message list |
| A Glue table and an Athena workgroup | Nothing to query yet; the DDL above is ready, and the investigator role gains the query permissions in the same change |
| A DynamoDB subject mapping | Enumeration is seconds at this pool size and adds no second copy of the sub |
| Removing expired delete markers | Two years away, and CloudFormation refuses the setting in the same rule as an expiration in days |
