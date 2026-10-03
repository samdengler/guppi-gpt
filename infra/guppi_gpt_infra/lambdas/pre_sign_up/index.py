"""Cognito pre sign-up trigger: chat.dengler.io is invite only (docs/proposals/invites.md).

Cognito runs this once, when it is about to create a user: on a first Google sign-in
(PreSignUp_ExternalProvider), and on any other way into the pool. The sign-up goes ahead
only when the `invites` table holds an approved request for the address; otherwise the
function raises, Cognito creates no user, and the browser comes back to the site with an
error the page recognises by MARKER. It never runs on a chat request. Sam approved this
function on 3 October 2026 (AGENTS.md).
"""

from __future__ import annotations

import os

import boto3

# The page looks for this in Cognito's error_description; the address stays out of it,
# since Cognito puts the message in the redirect URL.
MARKER = "not-invited"

_table_resource = None


class NotInvited(Exception):
    """Refuses the sign-up; Cognito shows the message as 'PreSignUp failed with error ...'."""


def _table():
    global _table_resource
    if _table_resource is None:
        _table_resource = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
    return _table_resource


def handler(event, _context):
    email = (event.get("request", {}).get("userAttributes", {}).get("email") or "").strip().lower()
    if email:
        item = _table().get_item(Key={"email": email}, ConsistentRead=True).get("Item")
        if item and item.get("status") == "approved":
            return event
    raise NotInvited(MARKER)
