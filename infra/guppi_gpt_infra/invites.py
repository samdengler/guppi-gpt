"""Invite requests for an invite-only chat.dengler.io (docs/proposals/invites.md).

A visitor who is not signed in posts a request to /api/invite. A REST API writes it
straight into the `invites` table, one item per lower-cased address, with no Lambda in
between, as the feedback API does. The table's stream feeds an EventBridge Pipe, and the
Pipe starts an Express state machine that emails Sam through SES: from
no-reply@dengler.io, with the requester as Reply-To and a link to the approval page. The
approval page approves with a conditional update. An approval adds the requester to Okta
(guppi-hr D46): the state machine creates the Okta user in the group `chat-users`, which is
who may sign in, through Okta's API with an EventBridge connection holding the API token,
and Okta emails them a link to set up their sign-in; then it mails the requester. Someone
already in Okta is added to the group instead. No Lambda function: the Cognito pre sign-up
trigger that once enforced the invite is gone with Cognito.
"""

from __future__ import annotations

import json
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import Duration, RemovalPolicy
from aws_cdk import aws_apigateway as apigateway
from aws_cdk import aws_events as events
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_pipes as pipes
from aws_cdk import aws_route53 as route53
from aws_cdk import aws_ses as ses
from aws_cdk import aws_stepfunctions as sfn
from constructs import Construct

MAILER_NAME = "guppi-gpt-invite-mailer"
TABLE_NAME = "guppi-gpt-invites"
API_NAME = "guppi-gpt-invites"
STAGE_NAME = "prod"
SENDER = "no-reply@dengler.io"
SENDER_DOMAIN = "dengler.io"
NAME_MAX_LENGTH = 100
EMAIL_MAX_LENGTH = 254
NOTE_MAX_LENGTH = 500
# A few requests a minute is plenty for a personal site, and each new address costs Sam an
# email; SES's own daily quota is the second limit.
REQUEST_RATE_PER_SECOND = 0.1
REQUEST_BURST = 3
EMAIL_PATTERN = "^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$"
UUID_PATTERN = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
# The approval page's two calls are Sam's alone; a handful a minute is generous.
APPROVAL_RATE_PER_SECOND = 0.5
APPROVAL_BURST = 5

# The PutItem body. Every value comes from the request the validator already accepted,
# escaped for JSON with escapeJavaScript, whose \' JSON does not accept, so replaceAll puts
# the apostrophe back (the feedback API does the same). The address is lower-cased so one
# person is one item however they type it. The token Sam's approval link carries is this
# request's id, a UUID API Gateway generates; the condition makes a repeat request a
# no-op, so it neither overwrites a decision nor mails Sam again.
_REQUEST_TEMPLATE = r"""
#set($email = $util.escapeJavaScript($input.path('$.email').trim().toLowerCase()).replaceAll("\\'", "'"))
#set($name = $util.escapeJavaScript($input.path('$.name').trim()).replaceAll("\\'", "'"))
#set($note = $input.path('$.note'))
#if(!$note)#set($note = "")#end
#set($note = $util.escapeJavaScript($note.trim()).replaceAll("\\'", "'"))
{
  "TableName": "TABLE",
  "Item": {
    "email": {"S": "$email"},
    "name": {"S": "$name"},
    "note": {"S": "$note"},
    "status":{"S":"pending"},
    "token": {"S": "$context.requestId"},
    "requestedAt": {"N": "$context.requestTimeEpoch"}
  },
  "ConditionExpression": "attribute_not_exists(email)"
}
""".strip().replace("TABLE", TABLE_NAME)

# A repeat request answers like a new one: the item exists, nothing changed, and the
# visitor learns nothing about who else asked.
_REJECTED_TEMPLATE = r"""
#set($type = $input.path('$.__type'))
#if($type && $type.contains("ConditionalCheckFailedException"))
#set($context.responseOverride.status = 202)
{"status":"requested"}
#else
{"message":"The request was rejected."}
#end
""".strip()


# The approval page's lookup. The address and token come from the link's query string; the
# details come back only when the stored token is the link's, so a guessed address alone
# reveals nothing, and a miss looks the same whether or not the address asked.
_LOOKUP_TEMPLATE = r"""
{
  "TableName": "TABLE",
  "Key": {"email": {"S": "$util.escapeJavaScript($input.params('email').trim().toLowerCase()).replaceAll("\\'", "'")"}},
  "ConsistentRead": true
}
""".strip().replace("TABLE", TABLE_NAME)

_LOOKUP_RESPONSE = r"""
#set($stored = $input.path('$.Item.token.S'))
#if($stored && $stored == $input.params('token'))
#set($note = $input.path('$.Item.note.S'))
#if(!$note)#set($note = "")#end
{"name":"$util.escapeJavaScript($input.path('$.Item.name.S')).replaceAll("\\'", "'")","email":"$util.escapeJavaScript($input.path('$.Item.email.S')).replaceAll("\\'", "'")","note":"$util.escapeJavaScript($note).replaceAll("\\'", "'")","requestedAt":$input.path('$.Item.requestedAt.N'),"status":"$input.path('$.Item.status.S')"}
#else
#set($context.responseOverride.status = 404)
{"message":"No request matches this link."}
#end
""".strip()

# Approval flips a pending request to approved, and only with the token the request was
# created with: a wrong token, an approved or revoked request, or a missing one all fail
# the condition, and the page says the link can't approve.
_APPROVE_TEMPLATE = r"""
#set($email = $util.escapeJavaScript($input.path('$.email').trim().toLowerCase()).replaceAll("\\'", "'"))
#set($token = $util.escapeJavaScript($input.path('$.token')).replaceAll("\\'", "'"))
{
  "TableName": "TABLE",
  "Key": {"email": {"S": "$email"}},
  "UpdateExpression": "SET #s = :approved, decidedAt = :now",
  "ConditionExpression": "#s = :pending AND #t = :token",
  "ExpressionAttributeNames": {"#s": "status", "#t": "token"},
  "ExpressionAttributeValues": {
    ":approved":{"S":"approved"},
    ":pending": {"S": "pending"},
    ":token": {"S": "$token"},
    ":now": {"N": "$context.requestTimeEpoch"}
  }
}
""".strip().replace("TABLE", TABLE_NAME)

_APPROVE_REJECTED = r"""
#set($type = $input.path('$.__type'))
#if($type && $type.contains("ConditionalCheckFailedException"))
#set($context.responseOverride.status = 409)
{"message":"This link can't approve: the request was already decided, or the link is wrong."}
#else
{"message":"The approval was rejected."}
#end
""".strip()


def _email_definition(site_url: str) -> str:
    """The state machine, in JSONata: a new pending item mails Sam; an item that turns
    approved is added to Okta's chat-users, then mails the requester, with replies going to
    Sam."""
    image = "$states.input[0].dynamodb.NewImage"
    # \n is two characters here; JSONata reads the escape inside its string literals.
    body = (
        "{% 'Someone asked for access to chat.dengler.io.\\n\\n'"
        f" & 'Name: ' & {image}.name.S & '\\n'"
        f" & 'Email: ' & {image}.email.S & '\\n'"
        f" & 'Note: ' & {image}.note.S & '\\n'"
        f" & 'Requested: ' & $fromMillis($number({image}.requestedAt.N)) & '\\n\\n'"
        f" & 'Approve: {site_url}approve.html?email=' & $encodeUrlComponent({image}.email.S)"
        f" & '&token=' & {image}.token.S & '\\n\\n'"
        " & 'Reply to this email to write to them.' %}"
    )
    welcome = (
        "{% 'Hi ' & $name & ',\\n\\n'"
        " & 'Sam approved your request for chat.dengler.io. Okta will email you a link to set'"
        " & ' up your sign-in; then sign in at'"
        f" & ' {site_url} with ' & $email & '. If you already have an Okta account there,'"
        " & ' just sign in.\\n\\n'"
        " & 'Reply to this email to reach Sam.' %}"
    )
    okta_headers = {"Accept": "application/json", "Content-Type": "application/json"}
    okta_auth = {"ConnectionArn": "${OktaConnectionArn}"}
    definition = {
        "Comment": "Invite request emails (docs/proposals/invites.md)",
        "QueryLanguage": "JSONata",
        "StartAt": "Route",
        "States": {
            "Route": {
                "Type": "Choice",
                "Choices": [
                    # Approved by the email's link (a change) or granted directly with
                    # scripts/invite.sh (a new item, or a revoked one approved again).
                    {
                        "Condition": f"{{% {image}.status.S = 'approved' %}}",
                        "Next": "Remember",
                    },
                    {
                        "Condition": "{% $states.input[0].eventName = 'INSERT' %}",
                        "Next": "MailSam",
                    },
                ],
                "Default": "Done",
            },
            # The task results below replace the input, so the requester is kept in variables.
            "Remember": {
                "Type": "Pass",
                # A direct grant has no name ("(granted)"); the address stands in for one.
                "Assign": {
                    "email": f"{{% {image}.email.S %}}",
                    "name": (f"{{% {image}.name.S = '(granted)' ? $substringBefore({image}.email.S, '@')"
                             f" : {image}.name.S %}}"),
                },
                "Next": "AddToOkta",
            },
            # A new Okta user in chat-users, activated: Okta mails the set-up link.
            "AddToOkta": {
                "Type": "Task",
                "Resource": "arn:aws:states:::http:invoke",
                "Arguments": {
                    "ApiEndpoint": "${OktaOrgUrl}/api/v1/users",
                    "Method": "POST",
                    "QueryParameters": {"activate": "true"},
                    "Authentication": okta_auth,
                    "Headers": okta_headers,
                    "RequestBody": {
                        "profile": {
                            "firstName": "{% $contains($name, ' ') ? $substringBefore($name, ' ') : $name %}",
                            "lastName": "{% $contains($name, ' ') ? $substringAfter($name, ' ') : '-' %}",
                            "email": "{% $email %}",
                            "login": "{% $email %}",
                        },
                        "groupIds": ["${OktaGroupId}"],
                    },
                },
                "Retry": [{"ErrorEquals": ["States.Http.StatusCode.429", "States.Http.StatusCode.500",
                                           "States.Http.StatusCode.502", "States.Http.StatusCode.503"],
                           "IntervalSeconds": 2, "MaxAttempts": 2, "BackoffRate": 2}],
                # Okta answers 400 when the login exists: add that user to the group instead.
                "Catch": [{"ErrorEquals": ["States.Http.StatusCode.400"], "Next": "FindOktaUser"}],
                "Next": "MailRequester",
            },
            "FindOktaUser": {
                "Type": "Task",
                "Resource": "arn:aws:states:::http:invoke",
                # A search by login, as a query parameter the task encodes once; an address in
                # the path was encoded twice and answered 404 (observed 4 Oct 2026).
                "Arguments": {
                    "ApiEndpoint": "${OktaOrgUrl}/api/v1/users",
                    "Method": "GET",
                    "QueryParameters": {"search": "{% 'profile.login eq \"' & $email & '\"' %}"},
                    "Authentication": okta_auth,
                    "Headers": okta_headers,
                },
                "Output": {"userId": "{% $states.result.ResponseBody[0].id %}"},
                "Next": "AddToGroup",
            },
            "AddToGroup": {
                "Type": "Task",
                "Resource": "arn:aws:states:::http:invoke",
                "Arguments": {
                    "ApiEndpoint": "{% '${OktaOrgUrl}/api/v1/groups/${OktaGroupId}/users/' & $states.input.userId %}",
                    "Method": "PUT",
                    "Authentication": okta_auth,
                    "Headers": okta_headers,
                },
                "Next": "MailRequester",
            },
            "MailSam": {
                "Type": "Task",
                "Resource": "arn:aws:states:::aws-sdk:sesv2:sendEmail",
                "Arguments": {
                    "FromEmailAddress": f"GuppiGPT invites <{SENDER}>",
                    "Destination": {"ToAddresses": ["${InviteEmail}"]},
                    "ReplyToAddresses": [f"{{% {image}.email.S %}}"],
                    "Content": {
                        "Simple": {
                            "Subject": {"Data": f"{{% 'Invite request: ' & {image}.name.S %}}"},
                            "Body": {"Text": {"Data": body}},
                        }
                    },
                },
                "End": True,
            },
            "MailRequester": {
                "Type": "Task",
                "Resource": "arn:aws:states:::aws-sdk:sesv2:sendEmail",
                "Arguments": {
                    "FromEmailAddress": f"GuppiGPT <{SENDER}>",
                    "Destination": {"ToAddresses": ["{% $email %}"]},
                    "ReplyToAddresses": ["${InviteEmail}"],
                    "Content": {
                        "Simple": {
                            "Subject": {"Data": "You're in: chat.dengler.io"},
                            "Body": {"Text": {"Data": welcome}},
                        }
                    },
                },
                "End": True,
            },
            "Done": {"Type": "Succeed"},
        },
    }
    return json.dumps(definition)


class Invites(Construct):
    """The request API, the table and the email to Sam."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        zone: route53.IHostedZone,
        site_url: str,
        invite_email: cdk.CfnParameter,
        has_invite_email: cdk.CfnCondition,
        okta_org_url: str,
        okta_group_id: str,
        okta_api_token: cdk.CfnParameter,
    ) -> None:
        super().__init__(scope, construct_id)
        stack = cdk.Stack.of(self)

        self.table = dynamodb.Table(
            self,
            "Table",
            table_name=TABLE_NAME,
            partition_key=dynamodb.Attribute(name="email", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            stream=dynamodb.StreamViewType.NEW_AND_OLD_IMAGES,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            # The table is the record of every request and decision; it outlives the stack.
            removal_policy=RemovalPolicy.RETAIN,
        )

        # ---- SES ------------------------------------------------------------------------
        # The domain verifies itself through the DKIM records CDK writes into the zone.
        # Sam's address is verified too: while the account's SES is in the sandbox it sends
        # only to verified addresses, and SES mails Sam a link to click once.
        ses.EmailIdentity(
            self,
            "DomainIdentity",
            identity=ses.Identity.public_hosted_zone(zone),
        )
        sam_identity = ses.CfnEmailIdentity(
            self, "InviteEmailIdentity", email_identity=invite_email.value_as_string
        )
        sam_identity.cfn_options.condition = has_invite_email

        # ---- The email ------------------------------------------------------------------
        machine_logs = logs.LogGroup(
            self,
            "MailerLogs",
            # Under the vended-logs prefix, as Step Functions recommends and as the stack's
            # other vended log groups are.
            log_group_name="/aws/vendedlogs/states/guppi-gpt-invite-mailer",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
        # Okta's API, with the token in Okta's own header scheme; the connection keeps it in
        # a Secrets Manager secret of its own, never in the definition or the logs.
        okta = events.Connection(
            self,
            "OktaConnection",
            connection_name="guppi-gpt-okta-invites",
            description="Okta API token for adding an approved person to chat-users",
            authorization=events.Authorization.api_key(
                "Authorization",
                cdk.SecretValue.unsafe_plain_text(cdk.Fn.join("", ["SSWS ", okta_api_token.value_as_string])),
            ),
        )
        machine = sfn.StateMachine(
            self,
            "Mailer",
            state_machine_name=MAILER_NAME,
            state_machine_type=sfn.StateMachineType.EXPRESS,
            definition_body=sfn.DefinitionBody.from_string(_email_definition(site_url)),
            definition_substitutions={
                "InviteEmail": invite_email.value_as_string,
                "OktaConnectionArn": okta.connection_arn,
                "OktaOrgUrl": okta_org_url,
                "OktaGroupId": okta_group_id,
            },
            # Errors only, without the execution data: inputs and outputs hold addresses.
            logs=sfn.LogOptions(
                destination=machine_logs, level=sfn.LogLevel.ERROR, include_execution_data=False
            ),
            timeout=Duration.minutes(1),
        )
        machine_arn = f"arn:aws:states:{stack.region}:{stack.account}:stateMachine:{MAILER_NAME}"
        machine.add_to_role_policy(
            iam.PolicyStatement(
                actions=["states:InvokeHTTPEndpoint"],
                resources=[machine_arn],
                conditions={"StringLike": {"states:HTTPEndpoint": f"{okta_org_url}/api/v1/*"}},
            )
        )
        machine.add_to_role_policy(
            iam.PolicyStatement(actions=["events:RetrieveConnectionCredentials"], resources=[okta.connection_arn])
        )
        machine.add_to_role_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
                resources=[okta.connection_secret_arn],
            )
        )
        machine.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ses:SendEmail"],
                resources=[f"arn:aws:ses:{stack.region}:{stack.account}:identity/*"],
                conditions={"StringEquals": {"ses:FromAddress": SENDER}},
            )
        )

        pipe_role = iam.Role(
            self,
            "PipeRole",
            assumed_by=iam.ServicePrincipal("pipes.amazonaws.com"),
            description="Reads the invites table's stream and starts the mailer",
        )
        self.table.grant_stream_read(pipe_role)
        machine.grant_start_execution(pipe_role)
        pipes.CfnPipe(
            self,
            "Pipe",
            role_arn=pipe_role.role_arn,
            source=self.table.table_stream_arn,
            source_parameters=pipes.CfnPipe.PipeSourceParametersProperty(
                dynamo_db_stream_parameters=pipes.CfnPipe.PipeSourceDynamoDBStreamParametersProperty(
                    starting_position="LATEST", batch_size=1
                ),
                filter_criteria=pipes.CfnPipe.FilterCriteriaProperty(
                    filters=[
                        pipes.CfnPipe.FilterProperty(
                            pattern=json.dumps(
                                {
                                    "eventName": ["INSERT"],
                                    "dynamodb": {"NewImage": {"status": {"S": ["pending"]}}},
                                }
                            )
                        ),
                        pipes.CfnPipe.FilterProperty(
                            pattern=json.dumps(
                                {
                                    "eventName": ["MODIFY"],
                                    "dynamodb": {
                                        "OldImage": {"status": {"S": ["pending", "revoked"]}},
                                        "NewImage": {"status": {"S": ["approved"]}},
                                    },
                                }
                            )
                        ),
                        # scripts/invite.sh grant for an address with no request.
                        pipes.CfnPipe.FilterProperty(
                            pattern=json.dumps(
                                {
                                    "eventName": ["INSERT"],
                                    "dynamodb": {"NewImage": {"status": {"S": ["approved"]}}},
                                }
                            )
                        ),
                    ]
                ),
            ),
            target=machine.state_machine_arn,
            target_parameters=pipes.CfnPipe.PipeTargetParametersProperty(
                step_function_state_machine_parameters=pipes.CfnPipe.PipeTargetStateMachineParametersProperty(
                    invocation_type="FIRE_AND_FORGET"
                )
            ),
        )

        # ---- The request API ------------------------------------------------------------
        api_role = iam.Role(
            self,
            "ApiRole",
            assumed_by=iam.ServicePrincipal("apigateway.amazonaws.com"),
            description="Lets the invites REST API write a request into the invites table",
        )
        api_role.add_to_policy(
            iam.PolicyStatement(
                actions=["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:UpdateItem"],
                resources=[self.table.table_arn],
            )
        )
        self.api = apigateway.RestApi(
            self,
            "InvitesApi",
            rest_api_name=API_NAME,
            description="Invite requests from the sign-in screen, written straight to DynamoDB",
            endpoint_types=[apigateway.EndpointType.REGIONAL],
            deploy_options=apigateway.StageOptions(
                stage_name=STAGE_NAME,
                method_options={
                    "/api/invite/POST": apigateway.MethodDeploymentOptions(
                        throttling_rate_limit=REQUEST_RATE_PER_SECOND,
                        throttling_burst_limit=REQUEST_BURST,
                    ),
                    "/api/invite/request/GET": apigateway.MethodDeploymentOptions(
                        throttling_rate_limit=APPROVAL_RATE_PER_SECOND,
                        throttling_burst_limit=APPROVAL_BURST,
                    ),
                    "/api/invite/approve/POST": apigateway.MethodDeploymentOptions(
                        throttling_rate_limit=APPROVAL_RATE_PER_SECOND,
                        throttling_burst_limit=APPROVAL_BURST,
                    ),
                },
            ),
            # The account-level API Gateway CloudWatch role is not this stack's; execution
            # logging stays off, as on the feedback API.
            cloud_watch_role=False,
        )
        validator = self.api.add_request_validator(
            "InviteBodyValidator",
            request_validator_name="invite-body",
            validate_request_body=True,
            validate_request_parameters=False,
        )
        model = self.api.add_model(
            "InviteRequestModel",
            model_name="InviteRequest",
            content_type="application/json",
            description="One invite request from the sign-in screen",
            schema=apigateway.JsonSchema(
                schema=apigateway.JsonSchemaVersion.DRAFT4,
                title="InviteRequest",
                type=apigateway.JsonSchemaType.OBJECT,
                required=["name", "email"],
                additional_properties=False,
                properties={
                    "name": apigateway.JsonSchema(
                        type=apigateway.JsonSchemaType.STRING,
                        min_length=1,
                        max_length=NAME_MAX_LENGTH,
                    ),
                    "email": apigateway.JsonSchema(
                        type=apigateway.JsonSchemaType.STRING,
                        max_length=EMAIL_MAX_LENGTH,
                        pattern=EMAIL_PATTERN,
                    ),
                    "note": apigateway.JsonSchema(
                        type=apigateway.JsonSchemaType.STRING, max_length=NOTE_MAX_LENGTH
                    ),
                },
            ),
        )
        integration = apigateway.AwsIntegration(
            service="dynamodb",
            action="PutItem",
            integration_http_method="POST",
            options=apigateway.IntegrationOptions(
                credentials_role=api_role,
                passthrough_behavior=apigateway.PassthroughBehavior.NEVER,
                request_templates={"application/json": _REQUEST_TEMPLATE},
                integration_responses=[
                    apigateway.IntegrationResponse(
                        status_code="202",
                        selection_pattern="200",
                        response_templates={"application/json": '{"status":"requested"}'},
                    ),
                    apigateway.IntegrationResponse(
                        status_code="400",
                        selection_pattern="4\\d{2}",
                        response_templates={"application/json": _REJECTED_TEMPLATE},
                    ),
                ],
            ),
        )
        # CloudFront forwards the viewer path (/api/invite) under the stage, so the
        # resource tree mirrors it, as the feedback API's does.
        invite = self.api.root.add_resource("api").add_resource("invite")
        invite.add_method(
            "POST",
            integration,
            authorization_type=apigateway.AuthorizationType.NONE,
            request_validator=validator,
            request_models={"application/json": model},
            method_responses=[
                apigateway.MethodResponse(status_code="202"),
                apigateway.MethodResponse(status_code="400"),
            ],
        )

        # ---- The approval page's calls ------------------------------------------------
        params_validator = self.api.add_request_validator(
            "InviteParamsValidator",
            request_validator_name="invite-params",
            validate_request_body=False,
            validate_request_parameters=True,
        )
        invite.add_resource("request").add_method(
            "GET",
            apigateway.AwsIntegration(
                service="dynamodb",
                action="GetItem",
                integration_http_method="POST",
                options=apigateway.IntegrationOptions(
                    credentials_role=api_role,
                    passthrough_behavior=apigateway.PassthroughBehavior.NEVER,
                    request_templates={"application/json": _LOOKUP_TEMPLATE},
                    integration_responses=[
                        apigateway.IntegrationResponse(
                            status_code="200",
                            selection_pattern="200",
                            response_templates={"application/json": _LOOKUP_RESPONSE},
                        ),
                        apigateway.IntegrationResponse(
                            status_code="400",
                            selection_pattern="4\\d{2}",
                            response_templates={
                                "application/json": '{"message":"The lookup was rejected."}'
                            },
                        ),
                    ],
                ),
            ),
            authorization_type=apigateway.AuthorizationType.NONE,
            request_validator=params_validator,
            request_parameters={
                "method.request.querystring.email": True,
                "method.request.querystring.token": True,
            },
            method_responses=[
                apigateway.MethodResponse(status_code="200"),
                apigateway.MethodResponse(status_code="400"),
                apigateway.MethodResponse(status_code="404"),
            ],
        )
        approval_model = self.api.add_model(
            "InviteApprovalModel",
            model_name="InviteApproval",
            content_type="application/json",
            description="Sam's approval of one request, with the token from its email",
            schema=apigateway.JsonSchema(
                schema=apigateway.JsonSchemaVersion.DRAFT4,
                title="InviteApproval",
                type=apigateway.JsonSchemaType.OBJECT,
                required=["email", "token"],
                additional_properties=False,
                properties={
                    "email": apigateway.JsonSchema(
                        type=apigateway.JsonSchemaType.STRING,
                        max_length=EMAIL_MAX_LENGTH,
                        pattern=EMAIL_PATTERN,
                    ),
                    "token": apigateway.JsonSchema(
                        type=apigateway.JsonSchemaType.STRING, pattern=UUID_PATTERN
                    ),
                },
            ),
        )
        invite.add_resource("approve").add_method(
            "POST",
            apigateway.AwsIntegration(
                service="dynamodb",
                action="UpdateItem",
                integration_http_method="POST",
                options=apigateway.IntegrationOptions(
                    credentials_role=api_role,
                    passthrough_behavior=apigateway.PassthroughBehavior.NEVER,
                    request_templates={"application/json": _APPROVE_TEMPLATE},
                    integration_responses=[
                        apigateway.IntegrationResponse(
                            status_code="200",
                            selection_pattern="200",
                            response_templates={"application/json": '{"status":"approved"}'},
                        ),
                        apigateway.IntegrationResponse(
                            status_code="400",
                            selection_pattern="4\\d{2}",
                            response_templates={"application/json": _APPROVE_REJECTED},
                        ),
                    ],
                ),
            ),
            authorization_type=apigateway.AuthorizationType.NONE,
            request_validator=validator,
            request_models={"application/json": approval_model},
            method_responses=[
                apigateway.MethodResponse(status_code="200"),
                apigateway.MethodResponse(status_code="400"),
                apigateway.MethodResponse(status_code="409"),
            ],
        )
        self.api.add_gateway_response(
            "InviteBadRequestParametersResponse",
            type=apigateway.ResponseType.BAD_REQUEST_PARAMETERS,
            templates={"application/json": '{"message":$context.error.messageString}'},
        )
        self.api.add_gateway_response(
            "InviteBadRequestBodyResponse",
            type=apigateway.ResponseType.BAD_REQUEST_BODY,
            templates={
                "application/json": (
                    '{"message":$context.error.messageString,'
                    '"detail":"$context.error.validationErrorString"}'
                )
            },
        )
        self.origin_domain = f"{self.api.rest_api_id}.execute-api.{stack.region}.amazonaws.com"
        self.origin_path = f"/{STAGE_NAME}"
