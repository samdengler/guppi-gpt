"""Invite requests (docs/proposals/invites.md), phase 1: the request API and the email to Sam."""

import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template
from guppi_gpt_infra.stack import GuppiGptStack

ACCOUNT = "123456789012"
REGION = "us-east-1"
ZONE_CONTEXT_KEY = f"hosted-zone:account={ACCOUNT}:domainName=dengler.io:region={REGION}"


@pytest.fixture(scope="module")
def template() -> Template:
    app = cdk.App(
        context={
            ZONE_CONTEXT_KEY: {"Id": "/hostedzone/Z0000000000000", "Name": "dengler.io."},
            "image_uri": f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/guppi-gpt:test",
            # No asset is built in tests (the Rust issuer); the template is what is checked.
            "aws:cdk:bundling-stacks": [],
        }
    )
    stack = GuppiGptStack(app, "GuppiGpt", env=cdk.Environment(account=ACCOUNT, region=REGION))
    return Template.from_stack(stack)


def only(template: Template, resource_type: str, **match) -> dict:
    found = template.find_resources(resource_type, match or None)
    assert len(found) == 1, f"{resource_type}: {list(found)}"
    return next(iter(found.values()))


def test_invite_email_is_a_parameter_with_no_default_address(template):
    template.has_parameter("InviteEmail", {"Type": "String", "Default": ""})


def test_invites_table_is_keyed_by_email_streams_and_is_retained(template):
    table = only(
        template, "AWS::DynamoDB::Table", Properties={"TableName": "guppi-gpt-invites"}
    )
    props = table["Properties"]
    assert props["KeySchema"] == [{"AttributeName": "email", "KeyType": "HASH"}]
    assert props["StreamSpecification"] == {"StreamViewType": "NEW_AND_OLD_IMAGES"}
    assert props["BillingMode"] == "PAY_PER_REQUEST"
    assert table["DeletionPolicy"] == "Retain"


def test_request_api_takes_an_unauthenticated_validated_post(template):
    method = _method(template, "POST", "invite")
    props = method["Properties"]
    assert props["AuthorizationType"] == "NONE"
    assert "RequestValidatorId" in props
    integration = props["Integration"]
    assert integration["Uri"]["Fn::Join"][1][-1].endswith(":dynamodb:action/UpdateItem")
    template_text = integration["RequestTemplates"]["application/json"]
    # One item per lower-cased address, created once; the approval token is the request id.
    assert "toLowerCase()" in template_text
    # A repeat request keeps the first request's fields and decision and only stamps the
    # time, so an approved address that asks again reaches the mailer (no Okta account yet).
    assert "#s = if_not_exists(#s, :pending)" in template_text
    assert "lastRequestedAt = :now" in template_text
    assert "$context.requestId" in template_text
    assert '":pending": {"S": "pending"}' in template_text


def test_request_model_bounds_every_field(template):
    model = only(
        template, "AWS::ApiGateway::Model", Properties={"Name": "InviteRequest"}
    )
    schema = model["Properties"]["Schema"]
    assert schema["required"] == ["name", "email"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["name"]["maxLength"] == 100
    assert schema["properties"]["email"]["maxLength"] == 254
    assert schema["properties"]["note"]["maxLength"] == 500


def test_a_repeat_request_answers_like_the_first(template):
    method = _method(template, "POST", "invite")
    responses = method["Properties"]["Integration"]["IntegrationResponses"]
    rejected = [r for r in responses if r.get("SelectionPattern") == "4\\d{2}"]
    assert len(rejected) == 1
    body = rejected[0]["ResponseTemplates"]["application/json"]
    assert "ConditionalCheckFailedException" in body
    assert "responseOverride.status = 202" in body


def test_request_api_is_throttled(template):
    stage = only(template, "AWS::ApiGateway::Stage", Properties={"StageName": "prod", "RestApiId": {"Ref": Match.string_like_regexp("InvitesApi")}})
    settings = stage["Properties"]["MethodSettings"]
    invite = [s for s in settings if s["ResourcePath"] == "/~1api~1invite" and s["HttpMethod"] == "POST"]
    assert len(invite) == 1
    assert invite[0]["ThrottlingBurstLimit"] <= 5
    assert invite[0]["ThrottlingRateLimit"] <= 1


def test_cloudfront_sends_invite_paths_to_the_invites_api_before_the_gateway(template):
    distribution = only(template, "AWS::CloudFront::Distribution")
    patterns = [b["PathPattern"] for b in distribution["Properties"]["DistributionConfig"]["CacheBehaviors"]]
    assert "/api/invite*" in patterns
    assert patterns.index("/api/invite*") < patterns.index("/api/*")


def test_ses_sends_from_dengler_io_and_verifies_sams_address_when_given(template):
    domain = only(
        template, "AWS::SES::EmailIdentity", Properties={"EmailIdentity": "dengler.io"}
    )
    # DKIM signing is SES's default; nothing turns it off.
    assert domain["Properties"].get("DkimAttributes", {}).get("SigningEnabled", True) is True
    sam = only(
        template,
        "AWS::SES::EmailIdentity",
        Properties={"EmailIdentity": {"Ref": "InviteEmail"}},
    )
    assert sam["Condition"] == "HasInviteEmail"
    # The DKIM records land in the zone, so the domain verifies on its own.
    records = template.find_resources("AWS::Route53::RecordSet", {"Properties": {"Type": "CNAME"}})
    assert len([r for r in records if "Dkim" in r]) == 3


def test_new_requests_reach_an_express_state_machine_through_a_pipe(template):
    machine = only(template, "AWS::StepFunctions::StateMachine", Properties={"StateMachineType": "EXPRESS"})
    pipe = only(template, "AWS::Pipes::Pipe")
    props = pipe["Properties"]
    assert props["SourceParameters"]["DynamoDBStreamParameters"]["BatchSize"] == 1
    filters = [json.loads(f["Pattern"]) for f in props["SourceParameters"]["FilterCriteria"]["Filters"]]
    assert {"eventName": ["INSERT"], "dynamodb": {"NewImage": {"status": {"S": ["pending"]}}}} in filters
    assert props["TargetParameters"]["StepFunctionStateMachineParameters"]["InvocationType"] == "FIRE_AND_FORGET"
    assert machine["Properties"]["DefinitionSubstitutions"]["InviteEmail"] == {"Ref": "InviteEmail"}


def test_the_email_to_sam_comes_from_no_reply_and_replies_go_to_the_requester(template):
    machine = only(template, "AWS::StepFunctions::StateMachine", Properties={"StateMachineType": "EXPRESS"})
    definition = machine["Properties"]["DefinitionString"]
    text = definition if isinstance(definition, str) else json.dumps(definition)
    assert "no-reply@dengler.io" in text
    assert "${InviteEmail}" in text
    assert "ReplyToAddresses" in text
    assert "approve.html?email=" in text


def test_only_no_reply_may_send(template):
    policies = json.dumps(template.find_resources("AWS::IAM::Policy"))
    assert '"ses:SendEmail"' in policies
    assert '"ses:FromAddress": "no-reply@dengler.io"' in policies


# ---- Phase 2: the approval page's two calls -------------------------------------------


def _method(template: Template, http_method: str, path_part: str) -> dict:
    resources = template.find_resources(
        "AWS::ApiGateway::Resource", {"Properties": {"PathPart": path_part}}
    )
    (resource_id,) = [
        rid for rid, r in resources.items() if "InvitesApi" in json.dumps(r["Properties"]["RestApiId"])
    ]
    return only(
        template,
        "AWS::ApiGateway::Method",
        Properties={"HttpMethod": http_method, "ResourceId": {"Ref": resource_id}},
    )


def test_request_details_need_the_links_email_and_token(template):
    props = _method(template, "GET", "request")["Properties"]
    assert props["AuthorizationType"] == "NONE"
    assert props["RequestParameters"] == {
        "method.request.querystring.email": True,
        "method.request.querystring.token": True,
    }
    integration = props["Integration"]
    assert integration["Uri"]["Fn::Join"][1][-1].endswith(":dynamodb:action/GetItem")
    (ok,) = [r for r in integration["IntegrationResponses"] if r.get("SelectionPattern") == "200"]
    body = ok["ResponseTemplates"]["application/json"]
    # The details come back only when the stored token matches the link's.
    assert "$input.params('token')" in body
    assert "responseOverride.status = 404" in body


def test_approval_is_a_conditional_update_on_a_pending_request_with_its_token(template):
    props = _method(template, "POST", "approve")["Properties"]
    assert props["AuthorizationType"] == "NONE"
    assert "RequestValidatorId" in props
    integration = props["Integration"]
    assert integration["Uri"]["Fn::Join"][1][-1].endswith(":dynamodb:action/UpdateItem")
    text = integration["RequestTemplates"]["application/json"]
    assert "#s = :pending AND #t = :token" in text
    assert '":approved":{"S":"approved"}' in text
    (rejected,) = [r for r in integration["IntegrationResponses"] if r.get("SelectionPattern") == "4\\d{2}"]
    assert "responseOverride.status = 409" in rejected["ResponseTemplates"]["application/json"]


def test_approval_model_takes_only_an_email_and_a_uuid_token(template):
    schema = only(template, "AWS::ApiGateway::Model", Properties={"Name": "InviteApproval"})[
        "Properties"
    ]["Schema"]
    assert schema["required"] == ["email", "token"]
    assert schema["additionalProperties"] is False
    assert "pattern" in schema["properties"]["token"]


def test_the_api_role_may_read_and_update_but_not_delete(template):
    roles = template.find_resources("AWS::IAM::Policy")
    (policy,) = [p for k, p in roles.items() if k.startswith("InvitesApiRoleDefaultPolicy")]
    actions = set()
    for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
        listed = statement["Action"]
        actions.update([listed] if isinstance(listed, str) else listed)
    assert actions == {"dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:UpdateItem"}


def test_every_invite_method_is_throttled(template):
    stage = only(
        template,
        "AWS::ApiGateway::Stage",
        Properties={"StageName": "prod", "RestApiId": {"Ref": Match.string_like_regexp("InvitesApi")}},
    )
    throttled = {(s["ResourcePath"], s["HttpMethod"]) for s in stage["Properties"]["MethodSettings"]}
    assert throttled >= {
        ("/~1api~1invite", "POST"),
        ("/~1api~1invite~1request", "GET"),
        ("/~1api~1invite~1approve", "POST"),
    }


# ---- Phase 3: the requester's email ---------------------------------------------------


def test_an_approval_reaches_the_mailer_too(template):
    pipe = only(template, "AWS::Pipes::Pipe")
    filters = [
        json.loads(f["Pattern"])
        for f in pipe["Properties"]["SourceParameters"]["FilterCriteria"]["Filters"]
    ]
    assert {
        "eventName": ["MODIFY"],
        "dynamodb": {
            "OldImage": {"status": {"S": ["pending", "revoked"]}},
            "NewImage": {"status": {"S": ["approved"]}},
        },
    } in filters
    # A repeat request or re-grant for an approved address.
    assert {"eventName": ["MODIFY"], "dynamodb": {"OldImage": {"status": {"S": ["approved"]}},
                                                  "NewImage": {"status": {"S": ["approved"]}}}} in filters
    # A direct grant (scripts/invite.sh grant) for an address with no request.
    assert {"eventName": ["INSERT"], "dynamodb": {"NewImage": {"status": {"S": ["approved"]}}}} in filters


def machine_definition(template) -> dict:
    """The mailer's definition, with each substituted token read as a placeholder."""
    machine = only(template, "AWS::StepFunctions::StateMachine", Properties={"StateMachineType": "EXPRESS"})
    definition = machine["Properties"]["DefinitionString"]
    if isinstance(definition, str):
        return json.loads(definition)
    parts = definition["Fn::Join"][1]
    return json.loads("".join(p if isinstance(p, str) else "TOKEN" for p in parts))


def test_an_approval_adds_the_requester_to_okta_then_mails_them(template):
    states = machine_definition(template)["States"]
    route = states["Route"]["Choices"]
    # Any item that becomes approved goes to Okta, before a new pending request mails Sam.
    assert route[0]["Next"] == "Remember" and "'approved'" in route[0]["Condition"]
    assert route[1]["Next"] == "MailSam"
    assigned = states["Remember"]["Assign"]
    assert assigned["email"] == "{% $states.input[0].dynamodb.NewImage.email.S %}"
    assert "(granted)" in assigned["name"]
    add = states["AddToOkta"]
    assert add["Resource"] == "arn:aws:states:::http:invoke"
    args = add["Arguments"]
    assert args["Method"] == "POST" and args["ApiEndpoint"].endswith("/api/v1/users")
    assert args["QueryParameters"] == {"activate": "true"}
    assert args["RequestBody"]["profile"]["login"] == "{% $email %}"
    assert len(args["RequestBody"]["groupIds"]) == 1
    # An existing Okta user is found and added to the group instead.
    assert add["Catch"][0] == {"ErrorEquals": ["States.Http.StatusCode.400"], "Next": "FindOktaUser"}
    assert states["FindOktaUser"]["Next"] == "AddToGroup"
    assert states["AddToGroup"]["Arguments"]["Method"] == "PUT"
    # A new Okta account is always mailed; an existing user re-added only on a new approval.
    assert add["Next"] == "MailRequester"
    assert states["AddToGroup"]["Next"] == "MailIfFresh"
    assert states["MailIfFresh"]["Choices"] == [{"Condition": "{% $fresh %}", "Next": "MailRequester"}]
    assert states["MailIfFresh"]["Default"] == "Done"
    assert "OldImage" in states["Remember"]["Assign"]["fresh"]
    mail = states["MailRequester"]["Arguments"]
    assert mail["Destination"]["ToAddresses"] == ["{% $email %}"]
    assert mail["ReplyToAddresses"] == ["${InviteEmail}"]
    text = mail["Content"]["Simple"]["Body"]["Text"]["Data"]
    assert "https://chat.dengler.io/" in text and "Okta" in text and "Google" not in text


def test_the_okta_token_is_a_no_echo_parameter_inside_a_connection(template):
    template.has_parameter("OktaApiToken", {"Type": "String", "NoEcho": True})
    connection = only(template, "AWS::Events::Connection", Properties={"Name": "guppi-gpt-okta-invites"})
    auth = connection["Properties"]["AuthParameters"]["ApiKeyAuthParameters"]
    assert auth["ApiKeyName"] == "Authorization"
    assert "OktaApiToken" in json.dumps(auth["ApiKeyValue"])


def test_no_cognito_and_no_lambda_gate(template):
    assert template.find_resources("AWS::Cognito::UserPool") == {}
    assert template.find_resources("AWS::Lambda::Function", {"Properties": {"FunctionName": "guppi-gpt-pre-sign-up"}}) == {}


def test_the_mailer_may_call_only_okta_through_its_connection(template):
    policies = template.find_resources("AWS::IAM::Policy")
    (policy,) = [p for k, p in policies.items() if k.startswith("InvitesMailerRoleDefaultPolicy")]
    statements = policy["Properties"]["PolicyDocument"]["Statement"]
    by_action = {}
    for statement in statements:
        listed = statement["Action"]
        for action in [listed] if isinstance(listed, str) else listed:
            by_action[action] = statement
    http = by_action["states:InvokeHTTPEndpoint"]
    assert http["Resource"].endswith(":stateMachine:guppi-gpt-invite-mailer") or "guppi-gpt-invite-mailer" in json.dumps(http["Resource"])
    assert "/api/v1/*" in json.dumps(http["Condition"])
    assert "events:RetrieveConnectionCredentials" in by_action
    assert "secretsmanager:GetSecretValue" in by_action


