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
    method = only(
        template,
        "AWS::ApiGateway::Method",
        Properties={"HttpMethod": "POST", "RestApiId": {"Ref": Match.string_like_regexp("InvitesApi")}},
    )
    props = method["Properties"]
    assert props["AuthorizationType"] == "NONE"
    assert "RequestValidatorId" in props
    integration = props["Integration"]
    assert integration["Uri"]["Fn::Join"][1][-1].endswith(":dynamodb:action/PutItem")
    template_text = integration["RequestTemplates"]["application/json"]
    # One item per lower-cased address, created once; the approval token is the request id.
    assert "toLowerCase()" in template_text
    assert "attribute_not_exists(email)" in template_text
    assert "$context.requestId" in template_text
    assert '"status":{"S":"pending"}' in template_text


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
    method = only(
        template,
        "AWS::ApiGateway::Method",
        Properties={"HttpMethod": "POST", "RestApiId": {"Ref": Match.string_like_regexp("InvitesApi")}},
    )
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
