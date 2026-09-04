# Conversation logging

Proposal for backlog item 6: every conversation recorded for long term storage in S3, pseudonymous to the user, with a way for a privileged person to correlate a record back to an account when troubleshooting requires it. Status: proposal, nothing built. Written 3 Sep 2026 against commit 186b01d; revised after review to make the thread the unit of record.

Vocabulary, used throughout: a thread is one conversation, the AG-UI `threadId` the page generates on load and on New chat; a run is one turn, the AG-UI `runId` the page generates on each send; a session is the AgentCore runtime session, the `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` header the page generates once per page load.

The backlog states a preference for AWS native services, serverless where possible (scale to zero, pay per use, automatic scaling). AGENTS.md adds the rule that no Lambda function sits in the request path or the content sync path. Both hold throughout this proposal.

## Summary of the recommendation

The agent keeps one object per thread at `threads/<threadId>.json` in a dedicated, versioned, SSE-KMS bucket. At the end of each run, from a background task with its own timeout, it reads the thread's current object, merges in the run, and writes the object back: the full thread as the page sent it plus the new reply, the pseudonymous subject, created and updated timestamps, and a compact list of the runs so far. Object versions give the turn-by-turn history. Per-run detail (tool calls and their queries, error codes, the trim count) stays on the CloudWatch log line the agent already writes, now keyed by the same pseudonym.

The user is identified only by an HMAC-SHA256 of the Cognito sub claim, keyed with a secret the stack generates in Secrets Manager. Re-identification is done by a separate investigator role that can read the bucket, the key, and the user pool. Athena over the `threads/` prefix, with no partition, is the query path. No Firehose, no DynamoDB, no Lambda, and no change to the request path's latency.

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
| `runs` | one entry per run, appended | `run`, `started_at`, `model`, `input_tokens`, `output_tokens`, `first_delta_ms`, `total_ms`, `tool_calls` (a count), `outcome`, `error_code` (when the outcome is `error`), `dropped_messages` (how many leading messages the trim removed; the model saw `messages[dropped_messages:]` of that run's input). |

Size: a thread object is the thread's text plus a few hundred bytes per run, 2 to 20 KB for a typical thread and about 170 KB at the validation limits (40 messages under 4,000 characters). Every run rewrites the object and versioning keeps the previous body, so a twenty-run thread stores twenty versions whose text overlaps. At a few hundred threads a month that is under 100 MB a month including versions.

### The CloudWatch line, and what moves to it

The agent keeps logging one JSON line per run to the runtime's CloudWatch log group, as section 9 of the design document describes. It gains the fields that are per-run detail and would bloat the thread record if repeated in every run entry:

| Field | Status |
| --- | --- |
| `subject` | Changed from the truncated sha256 to the HMAC, so a CloudWatch line and a thread object share one pseudonym. |
| `thread`, `run`, `session`, `messages` (count), `input_tokens`, `output_tokens`, `first_delta_ms`, `total_ms`, `outcome` | As today. |
| `started_at`, `model`, `prompt_sha`, `error_code`, `dropped_messages`, `reply_chars` | New. `prompt_sha` is the first 12 hex characters of the sha256 of `SYSTEM_PROMPT`, so a change in behavior can be matched to a change in the prompt. |
| `tool_calls` | Changes from a count to a list of `{name, args, result_chars}`: the tool name, the concatenated `TOOL_CALL_ARGS` deltas parsed as JSON when they parse and capped at 1,000 characters, and the length of the result. The passages are not logged. |

The retrieval query in `args` is derived from the user's text, so the log group holds a fragment of conversation content. That is the one field with content that lives outside the bucket, and it is why the log group gets a 30-day retention (retention table below). Keeping the tool call list in the thread record's run entries instead is a small change if that fragment should stay in the bucket; open question 7.

Nothing moves from the CloudWatch line to S3: the line is written first, before the read and the write of the thread object, so it exists even when the S3 write fails.

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

A stack-defined IAM role, `ConversationInvestigatorRole`, is the only principal outside the runtime that can read thread objects. It is assumable by one principal supplied at deploy time (a CloudFormation parameter holding the ARN of Sam's SSO permission set role, blank to create the role with no trust), with a one-hour maximum session. Every assumption and every read is in CloudTrail, which is the audit trail of re-identification.

| Service | Actions | Resource |
| --- | --- | --- |
| S3 | `GetObject`, `GetObjectVersion`, `ListBucket`, `ListBucketVersions` | The log bucket, `threads/` and `athena/`; `PutObject` on `athena/` for query results |
| KMS | `Decrypt`, `GenerateDataKey` | The log bucket key |
| Secrets Manager | `GetSecretValue` | The subject key secret |
| Cognito | `ListUsers`, `AdminGetUser` | The user pool |
| Athena | `StartQueryExecution`, `GetQueryExecution`, `GetQueryResults` | One workgroup |
| Glue | `GetDatabase`, `GetTable` | The `guppi_gpt` database and `threads` table |
| DynamoDB (if built) | `GetItem`, `Query` | The mapping table |

The role has no delete permission. A data subject deletion is an administrative act carried out with the deploy credentials, so that a stolen investigator session cannot erase history.

### From a run id to a person, and back

Run id to person, when a user reports a bad reply and the CloudWatch line or the page names the run:

1. Assume the investigator role.
2. Athena: `SELECT t.thread, t.subject FROM guppi_gpt.threads t CROSS JOIN UNNEST(t.runs) AS x(r) WHERE r.run = '<run id>'`. The CloudWatch line for the run also carries `thread` and `subject`, which skips the query.
3. Read the subject key secret. List the pool's users and compute `hmac.new(key, sub.encode(), sha256).hexdigest()[:32]` for each until it matches. With the DynamoDB table this step is one `GetItem`.
4. The matching user carries the email and Google name.

Person to records, when someone asks what was recorded about them:

1. Assume the investigator role.
2. `ListUsers --filter 'email = "<address>"'` gives the sub.
3. Compute the subject from the sub and the key.
4. Athena: `SELECT thread, created_at, updated_at, cardinality(runs) FROM guppi_gpt.threads WHERE subject = '<subject>' ORDER BY created_at`.

To see a thread as it stood after a particular run, `aws s3api list-object-versions --prefix threads/<thread>.json` lists one version per run, and `get-object --version-id` fetches it. Athena reads current versions only.

### Retention per store

| Store | Holds | Retention | Mechanism |
| --- | --- | --- | --- |
| Log bucket, `threads/` | Thread objects and their versions | 730 days after the last write (open question 1) | Lifecycle: expire current versions after 730 days, expire noncurrent versions 730 days after they became noncurrent, remove expired delete markers |
| Log bucket, `athena/` | Query results, which contain content | 7 days | Lifecycle rule on the prefix |
| CloudWatch runtime log group | The line per run: identifiers, counts, latencies, tool calls with their queries | 30 days | Retention on the log group. The runtime creates it today with no expiry; the stack can create it ahead of time under the runtime's naming pattern and set retention, to be confirmed at build time |
| Subject key secret | The key | Life of the system | Destroying it is the emergency anonymization of every record at once |
| DynamoDB mapping (if built) | subject to sub | Until the user is deleted | Deleted as part of a data subject deletion |
| Cognito user pool | sub, email, name | Until the user is deleted | Existing |

### What a data subject deletion takes

1. Find the sub from the email (`ListUsers`), compute the subject.
2. List the person's thread ids with the Athena query above.
3. For each thread, delete every version of `threads/<thread>.json` (`list-object-versions` on the key, then `delete-objects` with the version ids). One key per thread, plus its versions.
4. Delete the mapping item, if the table exists.
5. Delete the Cognito user. After this the subject is unlinkable even to the key holder.
6. CloudWatch lines carrying the subject expire with the 30-day retention.

Step 3 is a short script over the query result.

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

Glue database `guppi_gpt`, table `threads`, both stack resources (`CfnDatabase`, `CfnTable`), and one Athena workgroup with its result location fixed to `s3://<log bucket>/athena/` and enforced. Sketch of the equivalent DDL:

    CREATE EXTERNAL TABLE guppi_gpt.threads (
      schema_version int,
      thread         string,
      session        string,
      subject        string,
      subject_key    string,
      created_at     string,
      updated_at     string,
      messages       array<struct<id:string, role:string, content:string>>,
      runs           array<struct<
                       run:string, started_at:string, model:string,
                       input_tokens:int, output_tokens:int,
                       first_delta_ms:int, total_ms:int,
                       tool_calls:int, outcome:string, error_code:string,
                       dropped_messages:int>>
    )
    ROW FORMAT SERDE 'org.openx.data.jsonserde.JsonSerDe'
    WITH SERDEPROPERTIES ('ignore.malformed.json' = 'true')
    LOCATION 's3://<log bucket>/threads/';

Queries the investigator will run most:

    SELECT t.thread, t.updated_at, r.run, r.outcome, r.error_code, r.total_ms
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
| Lifecycle | `threads/`: expire current versions after 730 days, noncurrent versions 730 days after they became noncurrent, expired delete markers removed; `athena/`: expire after 7 days; abort incomplete multipart uploads after 1 day | Transition to Glacier Instant Retrieval is possible and Athena still reads that class, but at under 100 MB a month it saves under a cent a year; Glacier Flexible Retrieval and Deep Archive would remove the objects from Athena. Expiry is the rule that matters, and its length is open question 1 |
| Removal policy | Retain | As the other buckets |
| Access logging | CloudTrail S3 data events for this bucket only | Records every read and write by principal: the audit trail for re-identification, and the signal for a runtime read with no matching write. About USD 0.10 per 100,000 events |

Bucket policy statements:

1. Deny any request without TLS (`aws:SecureTransport` false).
2. Deny `PutObject` unless `s3:x-amz-server-side-encryption` is `aws:kms` with this bucket's key.
3. Allow `GetObject` and `PutObject` on `threads/*` to the runtime role. No `ListBucket`, no delete, no version reads.
4. Deny `GetObject` and `GetObjectVersion` on `threads/*` and `athena/*` to every principal whose `aws:PrincipalArn` is neither the investigator role, the runtime role, nor the account's CDK deploy roles (which never read objects but must be able to update the policy). The deny is scoped to reads so an administrator retains the ability to fix the policy and to delete objects.
5. Allow `GetObject`, `ListBucket`, `ListBucketVersions`, `GetObjectVersion`, and `PutObject` on `athena/*` to the investigator role.

The KMS key policy mirrors this: encrypt and decrypt for the runtime role, decrypt and data key generation for the investigator role, administration for the account root.

## Changes to the design document, the page, and the decision log

The design document and the decision log are the source of truth and change in the same commit as the code (AGENTS.md). The edits are listed here for review before the build; the documents themselves are untouched by this proposal.

### Design document

| Section | Change |
| --- | --- |
| 2, Conversation | The requirement "When the page is reloaded or closed, the system shall discard the conversation. No message shall be persisted on the server or in browser storage." splits. The page half stays: "When the page is reloaded or closed, the system shall discard the conversation; no message shall be persisted in browser storage." The server half is replaced by a new group. |
| 2, new group Logging | "When a run ends, the agent shall write the thread's record (the messages as the page sent them plus the reply, the pseudonymous subject, created and updated times, and one entry per run with its model, token counts, latencies, tool call count, and outcome) to the conversation log bucket, keeping the previous version." "The record shall identify the user only by a keyed hash of the subject claim and shall not contain the bearer token, the email address, or the subject claim." "If the record cannot be read or written, then the agent shall log the failure and shall not change the run's events or outcome." "The system shall let only the investigator role read the conversation log, the subject key, and the query results; the runtime shall read and write thread records and shall not list them." "The system shall expire thread records 730 days after their last write." |
| 4, component notes | New rows: Conversation log bucket (SSE-KMS, versioned, one object per thread under `threads/`, lifecycle, bucket policy), Subject key (Secrets Manager, generated by the stack, read once per container), Investigator role (trust, permissions, CloudTrail as audit), Athena and Glue (workgroup, database, `threads` table). The runtime row gains the environment variables and the grants. Figure 3 gains a dashed edge from the runtime to the log bucket; the caption's "No Lambda function is present" stays true. |
| 5, Request flow | One sentence after the paragraph on the JWT: when the stream has ended the agent reads the thread's object, merges the run, and writes it back from a background task; nothing on the stream waits for it. The paragraph "The browser owns the conversation" gains: the server keeps a pseudonymous copy for troubleshooting, which the browser never reads. |
| 9, Agent design | Step 6 becomes: log the run line (now with the tool calls and their queries, the error code, the trim count, and the HMAC subject) and, when the stream has ended, read, merge, and write the thread record from a background task with a ten-second timeout. The paragraph on the system prompt notes that it tells the model conversations are logged without the user's identity. |
| 11, Security and cost limits | New control row: Conversation log access, bounding who can read stored threads, at the bucket policy, the KMS key policy, and the investigator role. A paragraph after the table stating that the log bucket is now the most sensitive data in the system, that the runtime reads and writes single thread objects by key and cannot list them, and that the key and the pool together are what re-identify a record. |
| 12, Decisions and alternatives | New rows: Unit of record (one object per thread, versioned; alternative one object per run; reason: a thread is what a troubleshooter reads, versions keep the turns, deletion is one key; reversibility: moderate, the record shape and the table change). Write path (direct read-modify-write from a background task; alternatives Firehose, CloudWatch subscription, EventBridge, all of which need a fold step; reversibility: easy, the sink is one class). Carrying the thread's history (GET before PUT; alternative the page returning it; reason: the agent is the only source of the run entries). Subject pseudonym (HMAC with a Secrets Manager key; alternatives plain sha256, KMS HMAC). Subject mapping (pool enumeration by the investigator; alternative DynamoDB table). |
| 14, Risks | New row: the log bucket holds every conversation; effect, a read by the wrong principal exposes users' questions; mitigation, SSE-KMS, the bucket policy deny, no `ListBucket` for the runtime, the investigator role, CloudTrail data events, expiry. |
| 15, Next steps | The item is removed once built. The README backlog item 6 is marked done in the same commit. |

### The page's privacy notice

Two strings in `web/src/index.html` say nothing is kept: the empty state's "Ask anything. This conversation is not saved." and the composer hint's "Nothing is saved." After the build both are wrong. Proposed wording:

| Element | Today | Proposed |
| --- | --- | --- |
| Empty state copy | Ask anything. This conversation is not saved. | Ask anything. Conversations are logged for troubleshooting without your name or email. This page forgets them when it closes. |
| Composer hint | Enter to send, Shift+Enter for a new line. Nothing is saved. | Enter to send, Shift+Enter for a new line. Conversations are logged; this page keeps none. |

The page copy addresses the user directly, which the writing rule for docs and comments does not cover. The wording is open question 4.

The system prompt in `agent.py` says "Nothing is saved between page loads, and you cannot recall earlier sessions." The model may repeat that as a privacy claim. Proposed: "You have no memory beyond the conversation on the current page and cannot recall earlier sessions. Conversations are logged for troubleshooting without the user's name or email; say so if asked."

The README's opening line and the design document's problem statement describe the chat as stateless with no history. Both remain true for the user experience; the README gains one sentence under Status naming the log bucket and the investigator role.

### Decision log entry

Revision history row, to be numbered at build time:

> Conversation logging built: a KMS-encrypted, versioned log bucket with one JSON object per thread under `threads/`, read, merged, and written by the runtime from a background task after each run's stream ends; the subject pseudonym changed from a truncated sha256 to an HMAC keyed by a stack-generated secret; per-run detail kept on the CloudWatch line; an investigator role as the only reader of the bucket, the key, and the Athena workgroup; a Glue table over the prefix with no partition; expiry 730 days after the last write. The page's "Nothing is saved" became a logging notice and the system prompt no longer claims nothing is saved. Trigger: backlog item 6, proposal in `docs/proposals/conversation-logging.md`. One object per run was the first draft and was replaced by the thread on review; Firehose, a CloudWatch Logs subscription, and EventBridge were set aside because each would need a fold step to produce thread objects; a DynamoDB subject mapping was set aside in favor of enumerating the user pool with the key.

Entry under Decisions that were reversed:

> No message persisted on the server. Revisions 1 to 12 required that no message be persisted on the server; the page's empty state and hint said so. Revision N records every thread in S3 for troubleshooting, pseudonymously, with re-identification confined to the investigator role. The browser-side half of the requirement (nothing in browser storage, the page forgets on reload) is unchanged. The earlier position stays the right one for a deployment that must make no record at all; this one trades it for the ability to see what a user saw when a reply was wrong.

## Implementation plan

Steps in order. Each step keeps `uv run -- pytest` green and `cdk synth` working without Docker.

### Step 1: stack resources

In `infra/guppi_gpt_infra/stack.py`, a new section after the knowledge base:

| Resource | Construct | Notes |
| --- | --- | --- |
| Log bucket key | `kms.Key` | Rotation on, alias `guppi-gpt-conversations`, removal policy retain |
| Log bucket | `s3.Bucket` | `encryption=KMS`, `encryption_key`, `bucket_key_enabled=True`, `enforce_ssl=True`, `versioned=True`, block all public access, lifecycle rules for `threads/` (current and noncurrent expiry at `CONVERSATION_RETENTION_DAYS`, expired delete markers) and `athena/` (7 days), removal policy retain; the deny statements added with `add_to_resource_policy` |
| Subject key secret | `secretsmanager.Secret` | `generate_secret_string(password_length=64, exclude_punctuation=True)`; the same pattern as `OriginVerifySecret` |
| Investigator role | `iam.Role` | Trusted by the ARN in a new `InvestigatorPrincipalArn` parameter, with a condition so a blank parameter yields a role nobody can assume; `max_session_duration` one hour; the permissions table above |
| Glue database and table | `glue.CfnDatabase`, `glue.CfnTable` | The DDL above as table input; no partition keys |
| Athena workgroup | `athena.CfnWorkGroup` | Result location `athena/` on the log bucket, SSE-KMS with the key, `enforce_work_group_configuration=True` |
| CloudTrail data events | `cloudtrail.Trail` with a data event selector for the bucket | Or a note to add the bucket to an existing account trail; open question 6 |
| Runtime role grants | `GetObject` and `PutObject` on `threads/*` (a hand-written statement, since `grant_read_write` would add `ListBucket` and delete); `key.grant_encrypt_decrypt(runtime_role)`; `secret.grant_read(runtime_role)` | |
| Runtime environment | `CONVERSATION_LOG_BUCKET`, `SUBJECT_KEY_SECRET_ARN` | Added to the `runtime.environment_variables` assignment at the end of the file |
| Outputs | `ConversationLogBucketName`, `InvestigatorRoleArn`, `AthenaWorkGroup` | Read by the verification steps |

Constants at the top of the file: `CONVERSATION_RETENTION_DAYS = 730`, `THREADS_PREFIX = "threads/"`, `ATHENA_PREFIX = "athena/"`.

Stack tests in `infra/tests/test_stack.py`: the bucket is KMS encrypted with the stack key and versioned; its lifecycle expires current and noncurrent versions under `threads/` and results under `athena/`; its policy denies reads to principals other than the investigator role, the runtime role, and the deploy roles; the runtime role has `GetObject` and `PutObject` on `threads/*` and neither `ListBucket` nor `DeleteObject` on the bucket; the secret exists and the runtime role can read it; the runtime environment carries the two new variables; the Glue table has the `runs` array column and no partition keys; `test_no_lambda_functions` still passes.

### Step 2: agent record and sink

A new module `agent/src/guppi_agent/conversation_log.py`:

| Piece | Responsibility |
| --- | --- |
| `subject_from_token(token, key) -> str` | Decode the unverified claims as `subject_hash` does today, return the first 32 hex characters of `hmac.new(key, sub, sha256)`, or `unknown` when there is no sub |
| `KeyProvider` | Reads the secret named by `SUBJECT_KEY_SECRET_ARN` once and caches it; returns `None` when the variable is unset or the read fails, logging a warning once |
| `Sink` protocol with `S3Sink` | `get(key) -> (body, etag) or None` (`NoSuchKey` is `None`); `put(key, body, etag)` calls `put_object` with `IfMatch=etag` when there was an existing object and `IfNoneMatch="*"` otherwise, and `ServerSideEncryption="aws:kms"`; one client per process. A `NullSink` when `CONVERSATION_LOG_BUCKET` is unset, for local runs |
| `run_entry(record)` | The compact entry for the `runs` list |
| `merge(existing, run_input, record)` | Builds the new thread body: `created_at` and `runs` from the existing body or fresh, `messages` from the run input plus the reply, `updated_at`, `subject` checked against the existing value |
| `schedule_write(run_input, record, token)` | `asyncio.create_task` of `write(...)`, held in a module-level set so it is not garbage collected. `write` does, under `asyncio.wait_for(..., timeout=10)`: fetch the key, compute the subject, `log.info` the run line, then `await asyncio.to_thread(...)` for the GET, the merge, and the PUT. Any exception becomes one `log.warning` with the thread and run ids |
| `drain()` | Awaits pending write tasks; used by tests and by nothing else |

Changes in `app.py`:

| Place | Change |
| --- | --- |
| `run_agent` | Keep the untrimmed run input for the record and store `dropped_messages`. On `TOOL_CALL_START` append `{id, name}` to `record["tool_calls"]`; on `TOOL_CALL_ARGS` append the delta to that entry's buffer; on `TOOL_CALL_END` parse it; on `TOOL_CALL_RESULT` set `result_chars`. On `TEXT_MESSAGE_START` keep the message id; on `TEXT_MESSAGE_CONTENT` append the delta to `record["reply"]`. On `RUN_ERROR` keep the event's `code` as `error_code`. |
| `event_stream` | Record `started_at` before the loop. In the `finally`, replace `log.info(json.dumps(record))` with `conversation_log.schedule_write(run, record, token)`. The `finally` runs after the last event has been yielded, including after `RUN_FINISHED`, and on `CancelledError` when the client disconnected, so the write never precedes the end of the stream. `asyncio.create_task` inside a `finally` during cancellation creates an independent task the cancellation does not reach. |
| `invocations` | Unchanged apart from passing the token through; the token is used only to derive the subject inside the background task and is never stored. |
| `subject_hash` | Removed with its test; `conversation_log.subject_from_token` replaces it. |

The ten-second timeout is longer than a key read, a GET, and a PUT of a small object should take and far shorter than the runtime's fifteen-minute idle timeout, so a write completes or fails before the session can be reclaimed. The AgentCore Runtime keeps the container alive between the runs of a session, so a task that outlives the response by a second is normal.

### Step 3: agent tests

In `agent/tests/test_app.py`, with a `FakeSink` holding a dictionary of key to `(body, etag)` and a monkeypatched `KeyProvider` returning a fixed key, plus `await conversation_log.drain()` after each post:

1. The first run of a thread writes `threads/t1.json` whose body has `created_at` equal to `updated_at`, `messages` equal to the run input plus the reply `Hello there` as an assistant message, one entry in `runs` with `run` `r1`, `tool_calls` `1`, the token counts and timings, and `outcome` `finished`.
2. A second post with `runId` `r2` and the thread's two earlier messages plus a new user turn rewrites the object: `created_at` unchanged, `updated_at` later, `messages` replaced by the new input plus the new reply, `runs` now two entries, and the fake sink saw `IfMatch` with the first version's ETag.
3. The body and the CloudWatch line contain neither the token nor `user-1`; `subject` equals the HMAC of `user-1` with the test key, 32 hex characters.
4. The CloudWatch line carries `tool_calls` as a list whose first entry's `name` is `docs___Retrieve` and whose `args.query` is what `FakeRun` sent (the fake gains a `TOOL_CALL_ARGS` event), plus `error_code`, `dropped_messages`, and `reply_chars`; it carries no `messages` content.
5. A sink whose `get` raises leaves the stream identical, writes nothing, and produces one warning with the thread and run ids; the same for a `put` that raises a precondition failure.
6. A sink that sleeps past the timeout produces the timeout warning and does not delay `RUN_FINISHED` (the response is complete before `drain` is awaited).
7. A missing key writes the record with `subject_key` `none` and no `subject`, with one warning; a later run whose subject differs from the stored one is skipped with a warning.
8. The trimmed-thread test also asserts `dropped_messages` in the run entry and that `messages` in the object is the untrimmed input.
9. A failing agent writes a run entry with `outcome` `error`, `error_code` `AGENT_ERROR`, and appends whatever partial reply was streamed.

### Step 4: page, prompt, documents

The two strings in `web/src/index.html`, the system prompt sentence in `agent.py`, the design document edits in the table above, the decision log entry, the README status sentence and backlog mark, and the AGENTS.md line that lists the runtime's environment variables. One commit with step 2 so that the code and the design agree at every commit.

### Step 5: deploy and verify by hand

1. `scripts/deploy.sh` with `InvestigatorPrincipalArn` supplied (open question 5 decides how; a `GUPPI_INVESTIGATOR_ARN` environment variable read by the script, matching `GUPPI_ALARM_EMAIL`).
2. In the browser, one thread of three runs including a question that triggers retrieval; then New chat and one run that produces an error (post a malformed body with `curl` through the gateway using the page's token, since the page refuses a 4,000-character message itself).
3. As the investigator role: `aws s3 ls s3://<bucket>/threads/` shows two objects; `aws s3api list-object-versions --prefix threads/<thread>.json` shows three versions for the first thread; `aws s3 cp` the current object and check `messages` (six entries), `runs` (three entries with tokens and timings), `subject` (32 hex), `created_at` before `updated_at`, and the absence of the token and email. Fetch the first version and confirm it holds two messages and one run.
4. As the deploy role: the same `aws s3 cp` is denied.
5. In CloudWatch Logs for the runtime, the run lines carry the same `thread` and `subject` values, the retrieval query under `tool_calls`, and no message text.
6. In Athena, as the investigator role, run the three queries above and confirm the rows, including the error run.
7. Re-identification: run the enumeration script for the subject and confirm it names the signed-in Google account; run it in reverse from the email.
8. Failure isolation: hotswap `CONVERSATION_LOG_BUCKET` to a bucket the role cannot reach, run one turn, confirm the reply streams normally and the warning appears; restore and run another turn, and confirm the object now has the restored run's messages and that its `runs` list lacks the run that failed to write (its CloudWatch line exists).
9. Timing: compare `total_ms` in the run line before and after the change on the same question; the difference should be noise, since the read and write start after the last event.
10. CloudTrail: confirm the `GetObject` and `PutObject` data events name the runtime role in pairs seconds apart, and the reads in step 3 name the investigator role.

## Open questions for Sam

1. Retention period for thread objects and their versions. The proposal writes 730 days after the last write into the lifecycle rules; the backlog says long term without a number.
2. Subject mapping: enumerate the user pool with the key (no new store, the investigator holds the key) or a DynamoDB table written on first run (the investigator resolves subjects without the key; a second copy of the sub to protect and delete). The proposal starts with enumeration.
3. Key custody: Secrets Manager secret read once per container (recommended) or a KMS HMAC key so the key never enters the container's memory, at one KMS call per run in the background task.
4. Wording of the page's notice and the system prompt sentence, and whether the sign-in screen gets a sentence too, since any Google account can sign in and is recorded from the first run.
5. Which principal the investigator role trusts: Sam's SSO permission set role ARN as a stack parameter supplied by `deploy.sh` from an environment variable, or a hardcoded ARN in the stack.
6. CloudTrail data events for the log bucket: a trail in this stack (about USD 2 a month for the trail's own bucket and delivery) or an existing account trail with the bucket added as a data event selector.
7. Whether the retrieval queries belong on the CloudWatch line, as proposed, or in the thread record's run entries, which would keep every fragment of conversation content inside the bucket at the cost of a few hundred bytes per run.
8. Whether a `client_disconnected` run should be merged into the thread when it streamed no reply text. The proposal merges every run so the `runs` list is complete.
9. Retention for the runtime's CloudWatch log group, which has none today. Thirty days is proposed, and creating the group from the stack under the runtime's naming pattern needs to be confirmed at build time.
