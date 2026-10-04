# Invite-only sign-in

**Since 4 October 2026 (guppi-hr D46): Okta.** Sign-in moved from Cognito with Google to
the Okta org, and Cognito is gone, with its pre sign-up Lambda. The request form, the
`guppi-gpt-invites` table and the email to Sam are unchanged. Who may sign in is now the
Okta group `chat-users`: approving a request (the email's link, or `scripts/invite.sh
approve` or `grant`) makes the mailer state machine create the Okta user in that group
through Okta's API (an EventBridge connection holding the API token), and Okta emails them
a link to set up their sign-in; someone already in Okta is added to the group instead.
Then the requester gets the "You're in" email. `scripts/invite.sh revoke` removes the
person from the group. An Okta account outside the group is refused at sign-in ("not
assigned to the application"), and the page shows the invite form. The people approved
before the move were not migrated (Sam, 3 October); instead a request from an address that
is already approved re-runs the Okta step, so one of them who uses the form gets an
account and the two emails (4 October). A repeat request from someone already in Okta, or
a re-grant, only confirms the group membership and sends nothing, so the form cannot be
used to flood an approved person's inbox. The rest of this document is the
Cognito design as it was.

Status: built and deployed, 3 October 2026 (phases 1 to 5 below). Two things wait: AWS's
review of SES production access, and a sign-in check with a Google account that has no
invite.

Today any Google account can sign in to chat.dengler.io: Cognito creates a user on the
first Google sign-in, and the only limit on a stranger is the edge gateway's per-user rate
limit (the stack's comment: "while any Google account is admitted (Cognito has no
allow-list yet)"). The pool holds 4 users, all from Google. This proposal makes the site
invite only: a visitor who is not signed in can ask for an invite, Sam gets an email with
the request, and an approved address can then sign in with Google. Every project under
`/p/<name>/` shares the one sign-in, so the gate covers all of them.

## What a visitor sees

The sign-in screen keeps "Continue with Google" for people who already have access, and
adds a short form under it:

- Name, the Google account email the person will sign in with, and an optional note
  ("why I'd like to try it").
- "Request an invite". On success the form is replaced by "Thanks. Sam will look at your
  request and email you when you're in."
- A second request for the same address says the request is already in, without sending
  Sam another email.

A Google sign-in by an address that is not approved does not create an account. Cognito
sends the browser back to the site with an error, and the page shows the sign-in screen
with "This Google account doesn't have access yet" above the form, the email filled in
where the page can tell it.

## What Sam sees

One email per new request, to samdengler@gmail.com: the name, the address, the note, the
time, and a link to an approval page on the site. The approval page shows the request
and one button, "Approve". A button rather than an approving link, because mail scanners
open links in messages and would approve every request on their own.

Sam's email comes from `no-reply@dengler.io` with the requester as Reply-To, so answering a
requester is a reply. Approving sends the requester a short "you're in" email with the
site's address (see "Email through SES").

A script in the repository covers the rest: `scripts/invite.sh list` (pending and
approved), `approve <email>` (the same as the button), `revoke <email>` (marks the
request revoked and deletes the Cognito user, which ends their access when their tokens
expire, within an hour).

## How it fits together

```
sign-in screen ── POST /api/invite ──> API Gateway ──(direct)──> DynamoDB table "invites"
                                                                   │ stream
                                                                   v
                         EventBridge Pipe ──> Step Functions (Express) ──> SES: email to Sam (new request)
                                                                        └─> SES: "you're in" (approved)
approval page ── POST /api/invite/approve ──> API Gateway ──(direct)──> DynamoDB update
Google sign-in ──> Cognito ── Pre sign-up trigger (Lambda) ── reads "invites" ──> allow or refuse
```

| Piece | Choice | Why |
| --- | --- | --- |
| Requests | DynamoDB table `invites`, key = lower-cased email; fields name, note, status (pending, approved, revoked), created and decided times, an approval token | One item per person makes a repeat request a no-op (conditional put) |
| Request API | `POST /api/invite` on a REST API behind CloudFront, as `/api/feedback` is: a mapping template writes the item, no Lambda | The pattern the feedback API already uses |
| Email | DynamoDB stream, an EventBridge Pipe, an Express state machine that calls SES `SendEmail` directly: a new pending item mails Sam (Reply-To the requester, with the approval link), an item turning `approved` mails the requester | No code and no second Lambda; one sending path for both emails |
| Approval | `POST /api/invite/approve` with the email and the item's token; a conditional update sets `approved` only when the token matches and the status is `pending` | The token is the API request id the item was created with, sent only in Sam's email; the button page avoids link scanners |
| The gate | A Cognito pre sign-up trigger: a small Lambda function that reads the item and refuses a sign-up whose email is not approved | Cognito runs code at sign-up only through Lambda. It runs once per new user, at their first sign-in, never on a chat request |
| Abuse | Per-method throttling on `/api/invite` (a few requests a minute), length limits in the request model, one email per address | Each request costs Sam an email; the web ACL is still in COUNT |

The pre sign-up function is the only Lambda function, and AGENTS.md asks for Sam's
approval before one is built. Without it the gate would have to sit in every agent's
authorizer, as a claim check, and an uninvited Google account would still get a Cognito
user.

## The 4 users already in the pool

The trigger fires only when Cognito creates a user, so the 4 existing users keep signing
in as before. Phase 4 writes an `approved` item for each, so `invite.sh list` shows them
and a later revoke works the same way. If any of them should lose access, that is a
`revoke` after phase 4.

## Email through SES

The stack verifies `dengler.io` as an SES identity (DKIM records in the Route 53 zone) and
sends from `no-reply@dengler.io`. A new account's SES is in the sandbox, which sends only to
verified addresses: the stack also verifies `samdengler@gmail.com` (SES mails a link Sam
clicks once), so the email to Sam works from phase 1. Mail to requesters needs production
access, which Sam requests once (`aws sesv2 put-account-details`, reviewed by AWS, usually
within a day); until then an approval still approves, and the state machine's failed send
shows in its execution history.

## Decisions, 3 October 2026

1. Sam approved the one Lambda function, the Cognito pre sign-up trigger (AGENTS.md).
2. The 4 existing users stay; phase 4 records them as approved.
3. Approval from the email's link and button, and from `scripts/invite.sh`.
4. Requesters get a "you're in" email through SES on approval.

## Plan

Each phase ends with a deploy and a check, as in the other proposals. The page change and
the gate land last, so nobody is locked out while the request path is being built.

1. Requests and the email to Sam. The SES identities (`dengler.io`, `samdengler@gmail.com`),
   the `invites` table, `POST /api/invite` behind CloudFront at `/api/invite`, the stream,
   the Pipe and the state machine's new-request branch. Check: a request made with curl
   produces one email from `no-reply@dengler.io` with the requester as Reply-To; the same
   request again produces none.
2. Approval. The approval page (`/approve.html`, plain text and one button) and
   `POST /api/invite/approve`; `scripts/invite.sh` with list, approve and revoke. Check:
   the email's link approves a request once; a wrong token and a second approval are
   refused.
3. The requester's email. The state machine's approved branch; Sam requests SES production
   access. Check: approving a request to a verified address sends "you're in"; after
   production access, any address.
4. The gate. The pre sign-up function and its trigger; `approved` items for the existing
   users. Check: an approved test address signs in; an unapproved one is refused and gets
   no Cognito user; the 4 existing users still sign in.
5. The page. The request form on the sign-in screen, the "doesn't have access yet" state
   from Cognito's error, tests for both, and this document's status. Check in a browser:
   both states, a request from the page reaching Sam's inbox.

## What the checks showed

| Phase | Commit | Check |
| --- | --- | --- |
| 1 | `4866667` | A request through CloudFront wrote one item (address lower-cased, the note's apostrophe kept, a 36 character token) and mailed Sam from `no-reply@dengler.io`, into the inbox; a repeat answered 202 and mailed nothing; a bad address and an extra `status` field answered 400 |
| 2 | `fd7bdea` | The email's link opened the approval page; Approve approved; a second approval answered 409 from the page and failed from the script; a reload said "Already approved." A wrong token answered 404 on the lookup and 409 on approve |
| 3 | `405aebb` | Approving a request to a verified address sent "You're in: chat.dengler.io" with replies to Sam. The first try sent nothing: the approval came within a minute of the Pipe's update, before its new filter applied |
| 4 | `def08ee` | With the trigger live, creating a user for an address with no invite failed with "PreSignUp failed with error not-invited."; a granted address was created (then removed). The 4 existing users were granted first and kept signing in |
| 5 | `b5b8990` | The not-invited return URL showed the notice and the form; a request from the form showed "Request sent" and mailed Sam. Sam's own signed-in session was untouched |

Test requests and the test address's SES identity were removed afterwards; the table holds
the 4 existing users.

## What is not settled

- A real refused Google sign-in. Phase 4 checked the trigger through `AdminCreateUser`,
  which gave the message above; the page looks for `not-invited` in `error_description`,
  on the assumption that a federated refusal carries the same text. A sign-in with a
  Google account that has no invite confirms it.
- SES production access, requested on 3 October 2026 and pending AWS's review. Until it
  is granted, the "you're in" email reaches only verified addresses; approval works either
  way.
- Throttle numbers for `/api/invite` (one request every 10 seconds, bursts of 3); the first
  values are a guess.
