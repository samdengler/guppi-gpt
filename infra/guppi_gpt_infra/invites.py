"""Invite requests for an invite-only chat.dengler.io (docs/proposals/invites.md).

A visitor who is not signed in posts a request to /api/invite. A REST API writes it
straight into the `invites` table, one item per lower-cased address, with no Lambda in
between, as the feedback API does. The table's stream feeds an EventBridge Pipe, and the
Pipe starts an Express state machine that emails Sam through SES: from
no-reply@dengler.io, with the requester as Reply-To and a link to the approval page. The
approval API, the requester's email and the sign-up gate come in later phases.
"""

from __future__ import annotations

import json

import aws_cdk as cdk
from aws_cdk import Duration, RemovalPolicy
from aws_cdk import aws_apigateway as apigateway
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_pipes as pipes
from aws_cdk import aws_route53 as route53
from aws_cdk import aws_ses as ses
from aws_cdk import aws_stepfunctions as sfn
from constructs import Construct

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


def _email_definition(site_url: str) -> str:
    """The state machine, in JSONata: a new pending item mails Sam."""
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
    definition = {
        "Comment": "Invite request emails (docs/proposals/invites.md)",
        "QueryLanguage": "JSONata",
        "StartAt": "Route",
        "States": {
            "Route": {
                "Type": "Choice",
                "Choices": [
                    {
                        "Condition": "{% $states.input[0].eventName = 'INSERT' %}",
                        "Next": "MailSam",
                    }
                ],
                "Default": "Done",
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
            # The table is who may sign in once the gate is on; it outlives the stack.
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
        machine = sfn.StateMachine(
            self,
            "Mailer",
            state_machine_type=sfn.StateMachineType.EXPRESS,
            definition_body=sfn.DefinitionBody.from_string(_email_definition(site_url)),
            definition_substitutions={"InviteEmail": invite_email.value_as_string},
            # Errors only, without the execution data: inputs and outputs hold addresses.
            logs=sfn.LogOptions(
                destination=machine_logs, level=sfn.LogLevel.ERROR, include_execution_data=False
            ),
            timeout=Duration.minutes(1),
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
                        )
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
            iam.PolicyStatement(actions=["dynamodb:PutItem"], resources=[self.table.table_arn])
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
                    )
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
