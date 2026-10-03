"""The GuppiGpt stack.

DNS and certificates, Cognito with Google federation, the agent runtime, the edge
gateway with a runtime target, the tools gateway in front of the knowledge base,
CloudFront serving the page and proxying /api/* to the gateway, a regional web ACL on
the edge gateway, and the billing and WAF alarms. Two CloudFront Functions route project
paths (/p/<name>/ to the page, /api/<name>/invocations to the gateway target <name>), and
/guppi/platform/... SSM parameters publish the identifiers a project stack reads.

Resource ordering that matters:
  apex A record -> user pool custom domain (Cognito refuses the domain without an A record)
  gateway -> runtime (runtime authorizer names the gateway as its allowed workload)
  runtime -> gateway role policy -> gateway target
  tools gateway -> runtime environment variables (the runtime needs the tools gateway url)
  feedback API -> CloudFront (its /api/feedback behavior is listed before /api/*, and
                  CloudFront matches behaviors in the order they appear)
"""

from __future__ import annotations

import json
from pathlib import Path

import aws_cdk as cdk
import jsii
from aws_cdk import (
    Duration,
    Fn,
    RemovalPolicy,
    SecretValue,
    Size,
)
from aws_cdk import (
    aws_apigateway as apigateway,
)
from aws_cdk import (
    aws_bedrock as bedrock,
)
from aws_cdk import (
    aws_bedrockagentcore as agentcore,
)
from aws_cdk import (
    aws_certificatemanager as acm,
)
from aws_cdk import (
    aws_cloudfront as cloudfront,
)
from aws_cdk import (
    aws_cloudfront_origins as origins,
)
from aws_cdk import (
    aws_cloudwatch as cloudwatch,
)
from aws_cdk import (
    aws_cloudwatch_actions as cloudwatch_actions,
)
from aws_cdk import (
    aws_cognito as cognito,
)
from aws_cdk import (
    aws_ecr_assets as ecr_assets,
)
from aws_cdk import (
    aws_events as events,
)
from aws_cdk import (
    aws_events_targets as events_targets,
)
from aws_cdk import (
    aws_iam as iam,
)
from aws_cdk import (
    aws_kinesisfirehose as firehose,
)
from aws_cdk import (
    aws_kms as kms,
)
from aws_cdk import (
    aws_logs as logs,
)
from aws_cdk import (
    aws_logs_destinations as logs_destinations,
)
from aws_cdk import (
    aws_route53 as route53,
)
from aws_cdk import (
    aws_route53_targets as targets,
)
from aws_cdk import (
    aws_s3 as s3,
)
from aws_cdk import (
    aws_scheduler as scheduler,
)
from aws_cdk import (
    aws_scheduler_targets as scheduler_targets,
)
from aws_cdk import (
    aws_secretsmanager as secretsmanager,
)
from aws_cdk import (
    aws_sns as sns,
)
from aws_cdk import (
    aws_sqs as sqs,
)
from aws_cdk import (
    aws_ssm as ssm,
)
from aws_cdk import (
    aws_wafv2 as wafv2,
)
from aws_cdk import (
    aws_xray as xray,
)
from constructs import Construct

from guppi_gpt_infra.invites import Invites

ZONE_NAME = "dengler.io"
CHAT_HOST = f"chat.{ZONE_NAME}"
AUTH_HOST = f"auth.{ZONE_NAME}"
SITE_URL = f"https://{CHAT_HOST}/"

# RFC 5737 TEST-NET-1: reserved for documentation, never routed. Cognito only needs the
# parent domain to resolve before it will create the custom domain.
APEX_PLACEHOLDER_IP = "192.0.2.1"

RUNTIME_NAME = "guppi_gpt"
GATEWAY_NAME = "guppi-gpt-edge"
TARGET_NAME = "api"  # makes the gateway path /api/invocations, matching the /api/* behavior
SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"
# W3C trace context minted by the page per run (docs/proposals/traceability.md). Both the
# edge gateway target and the runtime drop request headers they were not told to keep,
# so the header is named in both allowlists. The X-Ray form (X-Amzn-Trace-Id) cannot be
# used here: the runtime allowlist refuses every x-amzn- header except its own custom
# prefix ("Pass custom headers to Amazon Bedrock AgentCore Runtime", devguide).
TRACE_HEADER = "traceparent"
TOOLS_GATEWAY_NAME = "guppi-gpt-tools"
KB_TARGET_NAME = "docs"  # tools are named docs___Retrieve and docs___AgenticRetrieveStream
KB_NAME = "guppi-gpt-docs"
CONTENT_PREFIX = "docs/"  # scripts/seed-content.sh writes docs/<source>/... to the content bucket
ORIGIN_RESPONSE_TIMEOUT = Duration.seconds(60)
CLOUDFRONT_HOSTED_ZONE_ID = "Z2FDTNDATAQYW2"  # the same for every CloudFront distribution
RETRIEVE_TOOL = f"{KB_TARGET_NAME}___Retrieve"

MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

# Environment the runtime hands the container before the tools gateway exists. The
# container starts under opentelemetry-instrument (agent/Dockerfile); the runtime itself
# supplies the ADOT exporter settings (AGENT_OBSERVABILITY_ENABLED and the OTEL_* values,
# devguide "Add observability to your Amazon Bedrock AgentCore resources"), so only the
# app's own knobs live here. The health check is kept out of the trace stream: the
# runtime pings /ping often and a span per ping would only add log volume.
RUNTIME_BASE_ENVIRONMENT = {
    "LOG_LEVEL": "INFO",
    "OTEL_PYTHON_EXCLUDED_URLS": "/ping$",
}

ORIGIN_HEADER_NAME = "X-Origin-Verify"

# Dynatrace RUM, shipped dark behind the `rum` flag (web/features.json). The script
# itself is not part of this stack: docs/proposals/dynatrace.md documents uploading it
# to the site bucket at this path (by hand, or the optional deploy.sh step that copies
# web/vendor/ruxitagentjs.js into web/dist/dt/ before the sync) once Sam has a tenant.
# A stack constant, not a parameter, because the path is ours to choose and does not
# depend on any Dynatrace tenant detail.
RUM_SCRIPT_PATH = "/dt/ruxitagentjs.js"

# The WAF rules ran in COUNT from 3 Sep 2026 until real prompts, including code-heavy
# replies, produced no counts on any rule; they block since 4 Sep 2026. Set False to
# return to watching.
WAF_BLOCK = True

# Conversation logging (docs/proposals/conversation-logging.md). The bucket, the key, the
# HMAC secret, and the investigator role are always created; this switch decides whether the
# agent writes thread records, so turning logging on is this line plus a deploy. The page
# has its own switch in web/features.json and is flipped after this one.
CONVERSATION_LOG_ENABLED = True
CONVERSATION_RETENTION_DAYS = 30
THREADS_PREFIX = "threads/"

# Twice the expected monthly figure (design section 11).
BILLING_ALARM_USD = 50

# The CloudFront behavior for /api/* gives the gateway origin sixty seconds
# (ORIGIN_RESPONSE_TIMEOUT below) before it gives up on a response. Half of that is the
# point past which a run is already close to being cut off by CloudFront, not merely slow.
RUNTIME_LATENCY_P90_THRESHOLD_MS = 30_000

# WAF stays in COUNT (see WAF_BLOCK), so a 4xx from the edge gateway itself, not the web
# ACL, means a token expired or a request was malformed; ten percent is a starting point
# pending real traffic, to be tightened once section 15's WAF watch period is done.
EDGE_GATEWAY_4XX_RATE_THRESHOLD_PERCENT = 10

# Design section 11: bounds spend per signed-in user while any Google account is
# admitted (Cognito has no allow-list yet). Conservative starting values; the gateway
# rate limit dimension keys are documented at
# https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-rate-limits-dimensions.html
JWT_SUB_CLAIM_DIMENSION = "$.context.jwt.sub"
RATE_LIMIT_REQUESTS_PER_MINUTE = 30
RATE_LIMIT_CONCURRENT_CONNECTIONS = 2

# Vended log group naming and retention for the two gateways and the runtime
# (docs/proposals/operations.md).
VENDED_LOG_PREFIX = "/aws/vendedlogs/bedrock-agentcore"
VENDED_LOG_RETENTION = logs.RetentionDays.ONE_MONTH

# Dynatrace log ingest path for a Firehose "Dynatrace" destination, appended to the
# tenant's base URL (docs.dynatrace.com/docs/ingest-from/amazon-web-services/
# integrate-with-aws/aws-logs-ingest/lma-stream-logs-with-firehose): "use the full URL
# https://<environment_ID>.live.dynatrace.com/api/v2/logs/ingest/aws_firehose in the
# Firehose HTTP endpoint destination configuration."
DYNATRACE_LOGS_INGEST_PATH = "/api/v2/logs/ingest/aws_firehose"
# The fixed suffix of DynatraceOtlpEndpoint (docs/proposals/dynatrace.md: "no trailing
# slash, no /v1/traces suffix"), split off to recover the tenant's base URL.
DYNATRACE_OTLP_SUFFIX = "/api/v2/otlp"
# Dynatrace business events ingest, on the same tenant host as the two paths above
# ("Ingest business events via API", docs.dynatrace.com/docs/observe/business-analytics/
# ba-api-ingest, which redirects to .../observe/business-observability/bo-events-capturing/
# bo-events-capturing-external-sources): "Endpoint URL: https://{your-environment-id}.live.
# dynatrace.com/api/v2/bizevents/ingest", method POST, Content-Type application/json for
# the pure JSON format, and the token attached as "Authorization: Api-Token <token>" with
# the Ingest bizevents scope. Pure JSON has no mandatory fields; Grail stores every
# top-level attribute as a top-level field, and event.type and event.provider are the two
# attributes the ingest guidance asks a caller to set so the events can be told apart.
DYNATRACE_BIZEVENTS_INGEST_PATH = "/api/v2/bizevents/ingest"

# ---- Reply feedback ------------------------------------------------------------------
# A vote is a business event, not a turn: it travels its own path (REST API to EventBridge
# to a Dynatrace business event) rather than through the chat runtime or the trace
# (docs/proposals/feedback.md).
# CloudFront Functions for project routing (docs/proposals/platform.md): page-path.js
# serves /p/<name>/ from /index.html, agent-path.js maps /api/<name>/invocations to the
# gateway target <name>. Viewer request functions, not Lambda.
FUNCTIONS_DIR = Path(__file__).resolve().parent / "functions"

# The MCP Apps sandbox (docs/proposals/platform-phase-3.md). An app's HTML runs in an
# iframe sandboxed without allow-same-origin, loaded from /sandbox/frame.html because a
# srcdoc frame would inherit the page's CSP, which forbids inline scripts. The page and
# the sandbox share the origin here, the proof of concept arrangement; the production
# answer is a separate origin for the sandbox. The inner srcdoc frame that holds the app
# inherits this policy: inline script and style, data: images, nothing fetched, no
# connect-src, and framed only by this site.
SANDBOX_PATH_PATTERN = "/sandbox/*"
SANDBOX_CSP = (
    "default-src 'none'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'unsafe-inline'; "
    "img-src data:; "
    "frame-ancestors 'self'"
)

# The platform contract (docs/proposals/platform.md): identifiers a project's own stack
# reads at deploy time with ssm.StringParameter.value_for_string_parameter. SSM rather
# than CloudFormation exports, so a project never blocks a platform deploy.
PLATFORM_PARAMETER_PREFIX = "/guppi/platform"
PARAM_SITE_BUCKET_NAME = f"{PLATFORM_PARAMETER_PREFIX}/site-bucket-name"
PARAM_DISTRIBUTION_ID = f"{PLATFORM_PARAMETER_PREFIX}/distribution-id"
PARAM_SITE_URL = f"{PLATFORM_PARAMETER_PREFIX}/site-url"
PARAM_EDGE_GATEWAY_ID = f"{PLATFORM_PARAMETER_PREFIX}/edge-gateway-id"
PARAM_EDGE_GATEWAY_ARN = f"{PLATFORM_PARAMETER_PREFIX}/edge-gateway-arn"
PARAM_EDGE_GATEWAY_ROLE_ARN = f"{PLATFORM_PARAMETER_PREFIX}/edge-gateway-role-arn"
PARAM_TOOLS_GATEWAY_ID = f"{PLATFORM_PARAMETER_PREFIX}/tools-gateway-id"
PARAM_TOOLS_GATEWAY_URL = f"{PLATFORM_PARAMETER_PREFIX}/tools-gateway-url"
# A project's MCP server Runtime grants InvokeAgentRuntime to this role.
PARAM_TOOLS_GATEWAY_ROLE_ARN = f"{PLATFORM_PARAMETER_PREFIX}/tools-gateway-role-arn"
PARAM_USER_POOL_CLIENT_ID = f"{PLATFORM_PARAMETER_PREFIX}/user-pool-client-id"
PARAM_JWT_DISCOVERY_URL = f"{PLATFORM_PARAMETER_PREFIX}/jwt-discovery-url"
PARAM_CONVERSATION_LOG_BUCKET_NAME = f"{PLATFORM_PARAMETER_PREFIX}/conversation-log-bucket-name"
PARAM_CONVERSATION_LOG_KEY_SECRET_ARN = (
    f"{PLATFORM_PARAMETER_PREFIX}/conversation-log-key-secret-arn"
)
PARAM_ALARM_TOPIC_ARN = f"{PLATFORM_PARAMETER_PREFIX}/alarm-topic-arn"
# Where a project runtime sends its traces and which secret holds the token, so every
# runtime on the platform reports to the same Dynatrace tenant without a copy of the token.
# The endpoint is "none" while the Dynatrace parameters are blank (SSM takes no empty value).
PARAM_DYNATRACE_TRACES_ENDPOINT = f"{PLATFORM_PARAMETER_PREFIX}/dynatrace-traces-endpoint"
PARAM_DYNATRACE_TOKEN_SECRET_ARN = f"{PLATFORM_PARAMETER_PREFIX}/dynatrace-token-secret-arn"

FEEDBACK_API_NAME = "guppi-gpt-feedback"
FEEDBACK_STAGE_NAME = "prod"
FEEDBACK_PATH = "feedback"  # under /api on the API, so /api/feedback through CloudFront lands on it
FEEDBACK_BUS_NAME = "guppi-gpt-feedback"
FEEDBACK_EVENT_SOURCE = "guppigpt.feedback"
FEEDBACK_DETAIL_TYPE = "reply-feedback"
FEEDBACK_ARCHIVE_RETENTION_DAYS = 30
# What the Dynatrace business event carries as its two identifying attributes.
DYNATRACE_FEEDBACK_EVENT_TYPE = "guppigpt.reply-feedback"
DYNATRACE_EVENT_PROVIDER = "guppigpt"

# The page mints run ids with crypto.randomUUID (web/src/app.js), so the request validator
# can hold runId to that shape; the trace id is the 16 byte W3C value as 32 hex digits
# (docs/proposals/traceability.md).
UUID_PATTERN = "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
TRACE_ID_PATTERN = "^[0-9a-f]{32}$"
FEEDBACK_ID_MAX_LENGTH = 200

# The body mapping template for the PutEvents integration. Every value comes from the
# request body the validator already accepted, escaped for JSON with escapeJavaScript.
# escapeJavaScript also escapes an apostrophe as \', which JSON does not accept, so
# replaceAll puts it back. A missing optional field leaves its Velocity reference unset
# (Velocity skips a #set whose right side is null), so each optional field is given an
# empty string before it is escaped. PutEvents takes Detail as a string rather than an
# object, which is what the escaped braces below build. receivedAt does not come from the
# body: it is the epoch millisecond API Gateway received the request. The caller's
# Cognito sub claim is left out on purpose. The template has no HMAC, so the claim could
# only travel raw, and a raw subject in Dynatrace is what the conversation log's
# pseudonym exists to avoid; a vote joins its conversation through threadId, and the
# thread record in the conversation log bucket holds the pseudonym.
_FEEDBACK_TEMPLATE_SETUP = r"""
#set($vote = $util.escapeJavaScript($input.path('$.vote')).replaceAll("\\'", "'"))
#set($runId = $util.escapeJavaScript($input.path('$.runId')).replaceAll("\\'", "'"))
#set($threadId = $util.escapeJavaScript($input.path('$.threadId')).replaceAll("\\'", "'"))
#set($traceId = $input.path('$.traceId'))
#if(!$traceId)#set($traceId = "")#end
#set($traceId = $util.escapeJavaScript($traceId).replaceAll("\\'", "'"))
#set($requestId = $input.path('$.requestId'))
#if(!$requestId)#set($requestId = "")#end
#set($requestId = $util.escapeJavaScript($requestId).replaceAll("\\'", "'"))
#set($messageId = $input.path('$.messageId'))
#if(!$messageId)#set($messageId = "")#end
#set($messageId = $util.escapeJavaScript($messageId).replaceAll("\\'", "'"))
""".lstrip()
_FEEDBACK_TEMPLATE_DETAIL = (
    r"{\"vote\":\"$vote\",\"runId\":\"$runId\",\"threadId\":\"$threadId\","
    r"\"traceId\":\"$traceId\",\"requestId\":\"$requestId\",\"messageId\":\"$messageId\","
    r"\"receivedAt\":$context.requestTimeEpoch}"
)
FEEDBACK_REQUEST_TEMPLATE = (
    _FEEDBACK_TEMPLATE_SETUP
    + '{"Entries":[{"Source":"'
    + FEEDBACK_EVENT_SOURCE
    + '","DetailType":"'
    + FEEDBACK_DETAIL_TYPE
    + '","EventBusName":"'
    + FEEDBACK_BUS_NAME
    + '","Detail":"'
    + _FEEDBACK_TEMPLATE_DETAIL
    + '"}]}'
)


@jsii.implements(route53.IAliasRecordTarget)
class CognitoDomainAlias:
    """Alias to the CloudFront distribution behind a Cognito custom domain.

    The CDK's UserPoolDomainTarget resolves the distribution through an AwsCustomResource,
    which is a Lambda function. The CloudFormation resource exposes the same value as an
    attribute, so this target reads it directly and the stack stays Lambda free.
    """

    def __init__(self, domain: cognito.UserPoolDomain) -> None:
        cfn_domain = domain.node.default_child
        assert isinstance(cfn_domain, cognito.CfnUserPoolDomain)
        self._dns_name = cfn_domain.attr_cloud_front_distribution

    def bind(self, _record, _zone=None) -> route53.AliasRecordTargetConfig:
        return route53.AliasRecordTargetConfig(
            dns_name=self._dns_name, hosted_zone_id=CLOUDFRONT_HOSTED_ZONE_ID
        )


def _apply_condition(construct: Construct, condition: cdk.CfnCondition) -> None:
    """Set condition on every CloudFormation resource nested under construct.

    An L2 construct can create more than one underlying resource (a role's default
    policy, a bucket's SSL-enforcement policy); node.default_child only reaches the
    first. Walking the whole subtree catches every one of them, so a conditional L2
    construct never leaves a resource behind that CloudFormation would try to create
    unconditionally.
    """
    for child in construct.node.find_all():
        if isinstance(child, cdk.CfnResource):
            child.cfn_options.condition = condition


def _waf_rule_action() -> wafv2.CfnWebACL.RuleActionProperty:
    """The action for the byte-match and rate-based WAF rules: Count until WAF_BLOCK flips."""
    if WAF_BLOCK:
        return wafv2.CfnWebACL.RuleActionProperty(block=wafv2.CfnWebACL.BlockActionProperty())
    return wafv2.CfnWebACL.RuleActionProperty(count=wafv2.CfnWebACL.CountActionProperty())


def _waf_override_action() -> wafv2.CfnWebACL.OverrideActionProperty:
    """The override for the managed rule group: Count every finding until WAF_BLOCK flips."""
    if WAF_BLOCK:
        return wafv2.CfnWebACL.OverrideActionProperty(none={})
    return wafv2.CfnWebACL.OverrideActionProperty(count={})


class GuppiGptStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        google_client_id = cdk.CfnParameter(
            self,
            "GoogleClientId",
            type="String",
            description="OAuth client id from the guppi-gpt Google Cloud project",
        )
        google_client_secret = cdk.CfnParameter(
            self,
            "GoogleClientSecret",
            type="String",
            no_echo=True,
            description="OAuth client secret; supplied by scripts/deploy.sh from 1Password",
        )
        alarm_email = cdk.CfnParameter(
            self,
            "AlarmEmail",
            type="String",
            default="",
            description="Address subscribed to the alarm topic; left blank to subscribe no one",
        )
        has_alarm_email = cdk.CfnCondition(
            self,
            "HasAlarmEmail",
            expression=cdk.Fn.condition_not(
                cdk.Fn.condition_equals(alarm_email.value_as_string, "")
            ),
        )
        # Invite requests mail this address (docs/proposals/invites.md); scripts/deploy.sh
        # passes GUPPI_INVITE_EMAIL, so the address never sits in the repository.
        invite_email = cdk.CfnParameter(
            self,
            "InviteEmail",
            type="String",
            default="",
            description="Address invite requests are sent to; blank sends them nowhere",
        )
        has_invite_email = cdk.CfnCondition(
            self,
            "HasInviteEmail",
            expression=cdk.Fn.condition_not(
                cdk.Fn.condition_equals(invite_email.value_as_string, "")
            ),
        )
        investigator_principal_arn = cdk.CfnParameter(
            self,
            "InvestigatorPrincipalArn",
            type="String",
            default="",
            description=(
                "ARN allowed to assume the conversation investigator role; "
                "left blank the role trusts the account root"
            ),
        )
        has_investigator_principal = cdk.CfnCondition(
            self,
            "HasInvestigatorPrincipal",
            expression=cdk.Fn.condition_not(
                cdk.Fn.condition_equals(investigator_principal_arn.value_as_string, "")
            ),
        )

        # ---- Dynatrace, shipped dark ----------------------------------------------------
        # Every value here defaults empty, so every conditional below renders to the
        # stack's current behavior (no beacon origin in the CSP, no OTLP export env vars)
        # until Sam supplies real tenant details (docs/proposals/dynatrace.md). Nothing in
        # this stack depends on a real value.
        dynatrace_beacon_origin = cdk.CfnParameter(
            self,
            "DynatraceBeaconOrigin",
            type="String",
            default="",
            description=(
                "Origin the self-hosted RUM script sends its beacon to (for example "
                "https://bfxxxxxx.bf.dynatrace.com), added to the page's connect-src; "
                "left blank to leave the CSP unchanged"
            ),
        )
        has_dynatrace_beacon_origin = cdk.CfnCondition(
            self,
            "HasDynatraceBeaconOrigin",
            expression=cdk.Fn.condition_not(
                cdk.Fn.condition_equals(dynatrace_beacon_origin.value_as_string, "")
            ),
        )
        dynatrace_otlp_endpoint = cdk.CfnParameter(
            self,
            "DynatraceOtlpEndpoint",
            type="String",
            default="",
            description=(
                "Dynatrace OTLP base endpoint, for example "
                "https://<tenant>.live.dynatrace.com/api/v2/otlp (no trailing slash, no "
                "/v1/traces suffix); left blank to send no traces to Dynatrace"
            ),
        )
        dynatrace_api_token = cdk.CfnParameter(
            self,
            "DynatraceApiToken",
            type="String",
            no_echo=True,
            default="",
            description=(
                "Dynatrace API token with the openTelemetryTrace.ingest scope; supplied "
                "by scripts/deploy.sh from 1Password when both this and "
                "DynatraceOtlpEndpoint are set"
            ),
        )
        has_dynatrace_otlp = cdk.CfnCondition(
            self,
            "HasDynatraceOtlp",
            expression=cdk.Fn.condition_and(
                cdk.Fn.condition_not(
                    cdk.Fn.condition_equals(dynatrace_otlp_endpoint.value_as_string, "")
                ),
                cdk.Fn.condition_not(
                    cdk.Fn.condition_equals(dynatrace_api_token.value_as_string, "")
                ),
            ),
        )
        # Backend log forwarding shares the trace export parameters rather than asking for
        # the same tenant a third time: once Sam has set the OTLP endpoint and the API
        # token, both trace export and log forwarding turn on together.
        has_dynatrace_logs = cdk.CfnCondition(
            self,
            "HasDynatraceLogs",
            expression=cdk.Fn.condition_and(
                cdk.Fn.condition_not(
                    cdk.Fn.condition_equals(dynatrace_otlp_endpoint.value_as_string, "")
                ),
                cdk.Fn.condition_not(
                    cdk.Fn.condition_equals(dynatrace_api_token.value_as_string, "")
                ),
            ),
        )
        # The tenant's base URL, recovered by splitting the OTLP endpoint on its fixed
        # suffix rather than naming the same tenant in a second parameter. Log forwarding
        # and the feedback API destination each append their own ingest path to it.
        dynatrace_base_url = cdk.Fn.select(
            0, cdk.Fn.split(DYNATRACE_OTLP_SUFFIX, dynatrace_otlp_endpoint.value_as_string)
        )
        zone = route53.HostedZone.from_lookup(self, "Zone", domain_name=ZONE_NAME)

        # ---- Transaction Search ----------------------------------------------------------
        # Account-wide, and the one account-level switch this stack owns: the instrumented
        # container sends spans to CloudWatch's OTLP endpoint, which answers 400 until the
        # trace segment destination is CloudWatch Logs (observed 3 Sep 2026). The resource
        # policy is the one the AgentCore observability guide gives for X-Ray to write the
        # span log groups; the config resource flips the destination and indexes 1 percent.
        span_policy = logs.CfnResourcePolicy(
            self,
            "TransactionSearchLogsPolicy",
            policy_name="GuppiGptTransactionSearchXRayAccess",
            policy_document=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": "TransactionSearchXRayAccess",
                            "Effect": "Allow",
                            "Principal": {"Service": "xray.amazonaws.com"},
                            "Action": "logs:PutLogEvents",
                            "Resource": [
                                f"arn:aws:logs:{self.region}:{self.account}:log-group:aws/spans:*",
                                f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/application-signals/data:*",
                            ],
                            "Condition": {
                                "ArnLike": {
                                    "aws:SourceArn": f"arn:aws:xray:{self.region}:{self.account}:*"
                                },
                                "StringEquals": {"aws:SourceAccount": self.account},
                            },
                        }
                    ],
                }
            ),
        )
        transaction_search = xray.CfnTransactionSearchConfig(
            self, "TransactionSearch", indexing_percentage=1
        )
        transaction_search.node.add_dependency(span_policy)

        # ---- Alerting --------------------------------------------------------------------
        alarm_topic = sns.Topic(self, "AlarmTopic", display_name="GuppiGPT alarms")
        email_subscription = sns.CfnSubscription(
            self,
            "AlarmEmailSubscription",
            protocol="email",
            topic_arn=alarm_topic.topic_arn,
            endpoint=alarm_email.value_as_string,
        )
        email_subscription.cfn_options.condition = has_alarm_email

        # Billing metrics exist only in us-east-1, which is also where this stack deploys.
        billing_alarm = cloudwatch.Alarm(
            self,
            "BillingAlarm",
            alarm_description="Estimated month-to-date charges crossed the cost limit",
            metric=cloudwatch.Metric(
                namespace="AWS/Billing",
                metric_name="EstimatedCharges",
                dimensions_map={"Currency": "USD"},
                region="us-east-1",
                statistic="Maximum",
                period=Duration.hours(6),
            ),
            threshold=BILLING_ALARM_USD,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        billing_alarm.add_alarm_action(cloudwatch_actions.SnsAction(alarm_topic))

        # ---- DNS and certificates ------------------------------------------------------
        apex_record = route53.ARecord(
            self,
            "ApexPlaceholder",
            zone=zone,
            target=route53.RecordTarget.from_ip_addresses(APEX_PLACEHOLDER_IP),
            ttl=Duration.hours(1),
            comment=(
                "Placeholder so Cognito will issue auth.dengler.io; "
                "192.0.2.1 is RFC 5737 TEST-NET-1 and never routes"
            ),
        )
        chat_cert = acm.Certificate(
            self,
            "ChatCertificate",
            domain_name=CHAT_HOST,
            validation=acm.CertificateValidation.from_dns(zone),
        )
        auth_cert = acm.Certificate(
            self,
            "AuthCertificate",
            domain_name=AUTH_HOST,
            validation=acm.CertificateValidation.from_dns(zone),
        )

        # ---- Cognito -------------------------------------------------------------------
        user_pool = cognito.UserPool(
            self,
            "UserPool",
            user_pool_name="guppi-gpt",
            self_sign_up_enabled=False,  # users arrive only through Google federation
            sign_in_aliases=cognito.SignInAliases(email=True),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=True)
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )
        google = cognito.UserPoolIdentityProviderGoogle(
            self,
            "Google",
            user_pool=user_pool,
            client_id=google_client_id.value_as_string,
            client_secret_value=SecretValue.cfn_parameter(google_client_secret),
            scopes=["openid", "email", "profile"],
            attribute_mapping=cognito.AttributeMapping(
                email=cognito.ProviderAttribute.GOOGLE_EMAIL,
                fullname=cognito.ProviderAttribute.GOOGLE_NAME,
            ),
        )
        client = user_pool.add_client(
            "Web",
            user_pool_client_name="guppi-gpt-web",
            generate_secret=False,
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[
                    cognito.OAuthScope.OPENID,
                    cognito.OAuthScope.EMAIL,
                    cognito.OAuthScope.PROFILE,
                ],
                callback_urls=[SITE_URL],
                logout_urls=[SITE_URL],
            ),
            supported_identity_providers=[cognito.UserPoolClientIdentityProvider.GOOGLE],
            access_token_validity=Duration.minutes(60),
            id_token_validity=Duration.minutes(60),
            refresh_token_validity=Duration.days(30),
            prevent_user_existence_errors=True,
        )
        client.node.add_dependency(google)
        # Refresh token rotation is not yet an L2 property (aws-cdk-lib 2.268.0); set it on
        # the underlying CfnUserPoolClient. Rotation issues a new refresh token on every use
        # so a stolen token is good for one refresh; the grace period covers a client retry
        # of the same request racing the rotation.
        cfn_client = client.node.default_child
        assert isinstance(cfn_client, cognito.CfnUserPoolClient)
        cfn_client.refresh_token_rotation = cognito.CfnUserPoolClient.RefreshTokenRotationProperty(
            feature="ENABLED",
            retry_grace_period_seconds=30,
        )

        domain = user_pool.add_domain(
            "Domain",
            custom_domain=cognito.CustomDomainOptions(domain_name=AUTH_HOST, certificate=auth_cert),
            managed_login_version=cognito.ManagedLoginVersion.NEWER_MANAGED_LOGIN,
        )
        domain.node.add_dependency(apex_record)
        cognito.CfnManagedLoginBranding(
            self,
            "Branding",
            user_pool_id=user_pool.user_pool_id,
            client_id=client.user_pool_client_id,
            use_cognito_provided_values=True,
        )
        route53.ARecord(
            self,
            "AuthRecord",
            zone=zone,
            record_name="auth",
            target=route53.RecordTarget.from_alias(CognitoDomainAlias(domain)),
        )

        discovery_url = (
            f"https://cognito-idp.{self.region}.amazonaws.com/"
            f"{user_pool.user_pool_id}/.well-known/openid-configuration"
        )
        jwt_allowed_clients = [client.user_pool_client_id]

        # ---- Agent image ---------------------------------------------------------------
        image_uri = self.node.try_get_context("image_uri")
        runtime_role = self._runtime_role()
        if image_uri is None:
            # The context is the repository root so agent/Dockerfile can read uv.lock;
            # .dockerignore at the root keeps the context and the asset hash to the agent
            # files, the lockfile, and the workspace pyprojects.
            repo_root = Path(__file__).resolve().parents[2]
            asset = ecr_assets.DockerImageAsset(
                self,
                "AgentImage",
                directory=str(repo_root),
                file="agent/Dockerfile",
                ignore_mode=cdk.IgnoreMode.DOCKER,
                platform=ecr_assets.Platform.LINUX_ARM64,
            )
            asset.repository.grant_pull(runtime_role)
            image_uri = asset.image_uri
        else:
            runtime_role.add_to_policy(
                iam.PolicyStatement(
                    actions=["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
                    resources=[f"arn:aws:ecr:{self.region}:{self.account}:repository/*"],
                )
            )

        # ---- Edge gateway --------------------------------------------------------------
        gateway_role = iam.Role(
            self,
            "GatewayRole",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            description="Lets the edge gateway invoke the GuppiGPT runtime",
        )
        gateway = agentcore.CfnGateway(
            self,
            "EdgeGateway",
            name=GATEWAY_NAME,
            description="GuppiGPT edge: JWT check, per-user limits, runtime target",
            role_arn=gateway_role.role_arn,
            authorizer_type="CUSTOM_JWT",
            authorizer_configuration=agentcore.CfnGateway.AuthorizerConfigurationProperty(
                custom_jwt_authorizer=agentcore.CfnGateway.CustomJWTAuthorizerConfigurationProperty(
                    discovery_url=discovery_url,
                    allowed_clients=jwt_allowed_clients,
                )
            ),
            # protocol_type is left unset on purpose: runtime targets cannot be added to
            # MCP protocol gateways.
            exception_level="DEBUG",
            # waf_configuration is left unset: the CfnGateway default failure mode is
            # FAIL_CLOSE (AWS WAF docs, "Configuring the AWS WAF failure mode"), which is
            # the fail-closed behavior design section 11 asks for.
        )

        # ---- WAF -------------------------------------------------------------------------
        # The shared value goes into the template only as a secretsmanager dynamic reference
        # (through unsafe_unwrap() below), never as a literal, so it stays out of both the
        # WAF rule and the CloudFront origin header in plain text. This is a deploy-time
        # value the stack itself generates rather than a CloudFormation parameter, which
        # departs from the AGENTS.md line on secrets; recorded in the decision log.
        origin_secret = secretsmanager.Secret(
            self,
            "OriginVerifySecret",
            description=f"Value CloudFront sends as the {ORIGIN_HEADER_NAME} header to the gateway",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                exclude_punctuation=True, password_length=40
            ),
        )
        origin_secret_value = origin_secret.secret_value.unsafe_unwrap()

        cloudfront_only_rule = wafv2.CfnWebACL.RuleProperty(
            name="CloudFrontOnly",
            priority=0,
            statement=wafv2.CfnWebACL.StatementProperty(
                not_statement=wafv2.CfnWebACL.NotStatementProperty(
                    statement=wafv2.CfnWebACL.StatementProperty(
                        byte_match_statement=wafv2.CfnWebACL.ByteMatchStatementProperty(
                            field_to_match=wafv2.CfnWebACL.FieldToMatchProperty(
                                # single_header is typed as Any, so CDK does not translate
                                # this dict's casing the way it does typed properties: the
                                # key must already match the CloudFormation shape.
                                single_header={"Name": ORIGIN_HEADER_NAME}
                            ),
                            positional_constraint="EXACTLY",
                            search_string=origin_secret_value,
                            text_transformations=[
                                wafv2.CfnWebACL.TextTransformationProperty(priority=0, type="NONE")
                            ],
                        )
                    )
                )
            ),
            action=_waf_rule_action(),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                sampled_requests_enabled=True,
                cloud_watch_metrics_enabled=True,
                metric_name="GuppiGptCloudFrontOnly",
            ),
        )
        common_rule_set_rule = wafv2.CfnWebACL.RuleProperty(
            name="AWSManagedRulesCommonRuleSet",
            priority=1,
            statement=wafv2.CfnWebACL.StatementProperty(
                managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                    vendor_name="AWS", name="AWSManagedRulesCommonRuleSet"
                )
            ),
            override_action=_waf_override_action(),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                sampled_requests_enabled=True,
                cloud_watch_metrics_enabled=True,
                metric_name="GuppiGptCommonRuleSet",
            ),
        )
        rate_limit_rule = wafv2.CfnWebACL.RuleProperty(
            name="RateLimit",
            priority=2,
            statement=wafv2.CfnWebACL.StatementProperty(
                rate_based_statement=wafv2.CfnWebACL.RateBasedStatementProperty(
                    limit=60,
                    evaluation_window_sec=300,
                    aggregate_key_type="FORWARDED_IP",
                    forwarded_ip_config=wafv2.CfnWebACL.ForwardedIPConfigurationProperty(
                        header_name="X-Forwarded-For", fallback_behavior="NO_MATCH"
                    ),
                )
            ),
            action=_waf_rule_action(),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                sampled_requests_enabled=True,
                cloud_watch_metrics_enabled=True,
                metric_name="GuppiGptRateLimit",
            ),
        )
        web_acl = wafv2.CfnWebACL(
            self,
            "EdgeWebAcl",
            scope="REGIONAL",
            default_action=wafv2.CfnWebACL.DefaultActionProperty(
                allow=wafv2.CfnWebACL.AllowActionProperty()
            ),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                sampled_requests_enabled=True,
                cloud_watch_metrics_enabled=True,
                metric_name="GuppiGptEdgeWebAcl",
            ),
            rules=[cloudfront_only_rule, common_rule_set_rule, rate_limit_rule],
        )
        wafv2.CfnWebACLAssociation(
            self,
            "EdgeWebAclAssociation",
            resource_arn=gateway.attr_gateway_arn,
            web_acl_arn=web_acl.attr_arn,
        )

        # The gateway-waf devguide page does not list a dimension for these three metrics
        # (unlike the general invocation metrics, which use "Resource"); GatewayId with the
        # gateway identifier is an assumption, not something the docs state outright.
        for metric_name in ("WafBlocks", "WafFailCloses", "WafFailOpens"):
            waf_alarm = cloudwatch.Alarm(
                self,
                f"{metric_name}Alarm",
                alarm_description=f"{metric_name} on the GuppiGPT edge gateway crossed zero",
                metric=cloudwatch.Metric(
                    namespace="AWS/Bedrock-AgentCore",
                    metric_name=metric_name,
                    dimensions_map={"GatewayId": gateway.attr_gateway_identifier},
                    statistic="Sum",
                    period=Duration.minutes(5),
                ),
                threshold=1,
                evaluation_periods=1,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )
            waf_alarm.add_alarm_action(cloudwatch_actions.SnsAction(alarm_topic))

        # ---- Per-user rate limits --------------------------------------------------------
        # Requests and concurrency share one entry: CreateGatewayRateLimit refuses two rate
        # limits on the same gateway with the same dimension keys, and a single dimension
        # key list (just the sub claim) is what "per signed-in user" needs. The "*" value
        # is the wildcard entry, which gives every distinct caller an independent bucket at
        # this rate rather than one shared bucket for all users.
        agentcore.CfnGatewayRateLimit(
            self,
            "EdgeGatewayPerUserRateLimit",
            gateway_identifier=gateway.attr_gateway_identifier,
            description="Per-user request rate and concurrency, keyed on the JWT sub claim",
            dimension_keys=[JWT_SUB_CLAIM_DIMENSION],
            entries=[
                agentcore.CfnGatewayRateLimit.LimitEntryProperty(
                    dimensions={JWT_SUB_CLAIM_DIMENSION: "*"},
                    requests=[
                        agentcore.CfnGatewayRateLimit.RateConfigProperty(
                            rate=RATE_LIMIT_REQUESTS_PER_MINUTE, period="minute"
                        )
                    ],
                    connections=[
                        agentcore.CfnGatewayRateLimit.RateConfigProperty(
                            rate=RATE_LIMIT_CONCURRENT_CONNECTIONS, period="second"
                        )
                    ],
                )
            ],
        )

        # ---- Runtime -------------------------------------------------------------------
        protocol = self.node.try_get_context("runtime_protocol") or "AGUI"
        runtime = agentcore.CfnRuntime(
            self,
            "Runtime",
            agent_runtime_name=RUNTIME_NAME,
            description="GuppiGPT agent (AG-UI over SSE)",
            role_arn=runtime_role.role_arn,
            agent_runtime_artifact=agentcore.CfnRuntime.AgentRuntimeArtifactProperty(
                container_configuration=agentcore.CfnRuntime.ContainerConfigurationProperty(
                    container_uri=image_uri
                )
            ),
            network_configuration=agentcore.CfnRuntime.NetworkConfigurationProperty(
                network_mode="PUBLIC"
            ),
            protocol_configuration=protocol,
            # Without this allowlist the runtime validates the bearer and drops it; the
            # container then has no token to present to the tools gateway (observed 3 Sep
            # 2026 as RUN_ERROR UNAUTHORIZED on the first run of the real agent).
            request_header_configuration=agentcore.CfnRuntime.RequestHeaderConfigurationProperty(
                request_header_allowlist=["Authorization", TRACE_HEADER]
            ),
            authorizer_configuration=agentcore.CfnRuntime.AuthorizerConfigurationProperty(
                custom_jwt_authorizer=agentcore.CfnRuntime.CustomJWTAuthorizerConfigurationProperty(
                    discovery_url=discovery_url,
                    allowed_clients=jwt_allowed_clients,
                    # Binding the runtime to the gateway is off by default. With it on, the
                    # runtime demands a transaction token, and the gateway only supplies
                    # one when it signs the request itself (GATEWAY_IAM_ROLE), which a JWT
                    # runtime then rejects as an authorization method mismatch. Token
                    # passthrough forwards the user JWT with no transaction token, so the
                    # two settings cannot be combined today (observed 2 Sep 2026).
                    allowed_workload_configuration=(
                        agentcore.CfnRuntime.AllowedWorkloadConfigurationProperty(
                            hosting_environments=[
                                agentcore.CfnRuntime.HostingEnvironmentProperty(
                                    arn=gateway.attr_gateway_arn
                                )
                            ]
                        )
                        if self.node.try_get_context("bind_runtime_to_gateway")
                        else None
                    ),
                )
            ),
            # TOOLS_GATEWAY_URL, MODEL_ID, and RETRIEVE_TOOL are set below, once the tools
            # gateway exists; it is defined later in this file.
            environment_variables=dict(RUNTIME_BASE_ENVIRONMENT),
        )

        invoke_policy = iam.Policy(
            self,
            "GatewayInvokePolicy",
            roles=[gateway_role],
            statements=[
                iam.PolicyStatement(
                    actions=["bedrock-agentcore:InvokeAgentRuntime"],
                    resources=[
                        runtime.attr_agent_runtime_arn,
                        f"{runtime.attr_agent_runtime_arn}/runtime-endpoint/*",
                    ],
                )
            ],
        )

        target = agentcore.CfnGatewayTarget(
            self,
            "RuntimeTarget",
            gateway_identifier=gateway.attr_gateway_identifier,
            name=TARGET_NAME,
            description="GuppiGPT runtime, token passthrough",
            target_configuration=agentcore.CfnGatewayTarget.TargetConfigurationProperty(
                http=agentcore.CfnGatewayTarget.HttpTargetConfigurationProperty(
                    agentcore_runtime=agentcore.CfnGatewayTarget.RuntimeTargetConfigurationProperty(
                        arn=runtime.attr_agent_runtime_arn,
                        qualifier="DEFAULT",
                    )
                )
            ),
            credential_provider_configurations=[
                agentcore.CfnGatewayTarget.CredentialProviderConfigurationProperty(
                    credential_provider_type=(
                        self.node.try_get_context("target_credentials") or "JWT_PASSTHROUGH"
                    )
                )
            ],
            metadata_configuration=agentcore.CfnGatewayTarget.MetadataConfigurationProperty(
                allowed_request_headers=[SESSION_HEADER, TRACE_HEADER]
            ),
        )
        target.node.add_dependency(invoke_policy)

        # ---- Invite requests ------------------------------------------------------------
        # Requests from the sign-in screen, the table of who may sign in, and the email
        # to Sam (docs/proposals/invites.md).
        invites = Invites(
            self,
            "Invites",
            zone=zone,
            site_url=SITE_URL,
            invite_email=invite_email,
            has_invite_email=has_invite_email,
            user_pool=user_pool,
        )

        # ---- Reply feedback --------------------------------------------------------------
        # A vote on a reply is a business event, not part of the chat, so it does not go
        # through the chat runtime and it is not attached to the turn's trace. The page
        # posts it to /api/feedback on the existing CloudFront domain; a REST API with a
        # Cognito authorizer validates the body and puts one event on a bus of its own,
        # with no compute in between. A rule forwards the event to Dynatrace as a business
        # event once the Dynatrace parameters are set, and an archive on the bus keeps
        # every vote for 30 days either way (docs/proposals/feedback.md).
        feedback_bus = events.EventBus(self, "FeedbackBus", event_bus_name=FEEDBACK_BUS_NAME)
        feedback_api_role = iam.Role(
            self,
            "FeedbackApiRole",
            assumed_by=iam.ServicePrincipal("apigateway.amazonaws.com"),
            description="Lets the feedback REST API put one event on the feedback bus",
            inline_policies={
                "PutFeedbackEvent": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            actions=["events:PutEvents"], resources=[feedback_bus.event_bus_arn]
                        )
                    ]
                )
            },
        )
        feedback_api = apigateway.RestApi(
            self,
            "FeedbackApi",
            rest_api_name=FEEDBACK_API_NAME,
            description="Reply votes from the page, put straight onto the feedback bus",
            endpoint_types=[apigateway.EndpointType.REGIONAL],
            deploy_options=apigateway.StageOptions(stage_name=FEEDBACK_STAGE_NAME),
            # The account-level API Gateway CloudWatch role is an account-wide setting this
            # stack does not own; execution logging stays off with it.
            cloud_watch_role=False,
            # No CORS: the page reaches this API through CloudFront on its own origin, so
            # the browser never sends a preflight.
        )
        feedback_authorizer = apigateway.CognitoUserPoolsAuthorizer(
            self,
            "FeedbackAuthorizer",
            authorizer_name="guppi-gpt-feedback",
            cognito_user_pools=[user_pool],
        )
        feedback_validator = feedback_api.add_request_validator(
            "FeedbackBodyValidator",
            request_validator_name="feedback-body",
            validate_request_body=True,
            validate_request_parameters=False,
        )
        feedback_model = feedback_api.add_model(
            "FeedbackVoteModel",
            model_name="FeedbackVote",
            content_type="application/json",
            description="One vote on one reply",
            schema=apigateway.JsonSchema(
                schema=apigateway.JsonSchemaVersion.DRAFT4,
                title="FeedbackVote",
                type=apigateway.JsonSchemaType.OBJECT,
                required=["vote", "runId", "threadId"],
                additional_properties=False,
                properties={
                    # "none" is a withdrawn vote: the page reports it so the withdrawal is
                    # itself a record rather than a gap.
                    "vote": apigateway.JsonSchema(
                        type=apigateway.JsonSchemaType.STRING, enum=["up", "down", "none"]
                    ),
                    "runId": apigateway.JsonSchema(
                        type=apigateway.JsonSchemaType.STRING, pattern=UUID_PATTERN
                    ),
                    "threadId": apigateway.JsonSchema(
                        type=apigateway.JsonSchemaType.STRING,
                        min_length=1,
                        max_length=FEEDBACK_ID_MAX_LENGTH,
                    ),
                    "traceId": apigateway.JsonSchema(
                        type=apigateway.JsonSchemaType.STRING, pattern=TRACE_ID_PATTERN
                    ),
                    "requestId": apigateway.JsonSchema(
                        type=apigateway.JsonSchemaType.STRING, max_length=FEEDBACK_ID_MAX_LENGTH
                    ),
                    "messageId": apigateway.JsonSchema(
                        type=apigateway.JsonSchemaType.STRING, max_length=FEEDBACK_ID_MAX_LENGTH
                    ),
                },
            ),
        )
        feedback_integration = apigateway.AwsIntegration(
            service="events",
            action="PutEvents",
            integration_http_method="POST",
            options=apigateway.IntegrationOptions(
                credentials_role=feedback_api_role,
                passthrough_behavior=apigateway.PassthroughBehavior.NEVER,
                request_parameters={
                    "integration.request.header.X-Amz-Target": "'AWSEvents.PutEvents'",
                    "integration.request.header.Content-Type": "'application/x-amz-json-1.1'",
                },
                request_templates={"application/json": FEEDBACK_REQUEST_TEMPLATE},
                integration_responses=[
                    apigateway.IntegrationResponse(
                        status_code="202",
                        selection_pattern="200",
                        # The page does not read a body, and there is nothing to say back:
                        # the vote is on the bus.
                        response_templates={"application/json": ""},
                    ),
                    apigateway.IntegrationResponse(
                        status_code="400",
                        selection_pattern="4\\d{2}",
                        response_templates={
                            "application/json": '{"message":"The vote was rejected."}'
                        },
                    ),
                ],
            ),
        )
        # CloudFront forwards the viewer path unchanged (/api/feedback) under the origin
        # path (/prod), so the API's resource tree mirrors it; a resource at /feedback alone
        # answered the doubled path /prod/api/feedback with "Missing Authentication Token"
        # (observed 5 Sep 2026).
        feedback_resource = feedback_api.root.add_resource("api").add_resource(FEEDBACK_PATH)
        feedback_resource.add_method(
            "POST",
            feedback_integration,
            authorization_type=apigateway.AuthorizationType.COGNITO,
            authorizer=feedback_authorizer,
            # The page holds the access token, not the id token, and sends it as the bearer
            # everywhere else. A Cognito authorizer with no authorization scopes reads the
            # bearer as an id token and rejects an access token, which carries client_id
            # rather than aud ("Integrate a REST API with an Amazon Cognito user pool",
            # API Gateway developer guide). Naming a scope switches the authorizer to
            # access token validation; "openid" is in the scope claim of every token this
            # app client issues, since the page asks for openid, email, and profile.
            authorization_scopes=["openid"],
            request_validator=feedback_validator,
            request_models={"application/json": feedback_model},
            method_responses=[
                apigateway.MethodResponse(status_code="202"),
                apigateway.MethodResponse(status_code="400"),
            ],
        )
        # The default gateway responses for a rejected token and a body the validator
        # refused are text; the page and anything else calling this API read JSON.
        feedback_api.add_gateway_response(
            "FeedbackUnauthorizedResponse",
            type=apigateway.ResponseType.UNAUTHORIZED,
            templates={"application/json": '{"message":$context.error.messageString}'},
        )
        feedback_api.add_gateway_response(
            "FeedbackBadRequestBodyResponse",
            type=apigateway.ResponseType.BAD_REQUEST_BODY,
            templates={
                "application/json": (
                    '{"message":$context.error.messageString,'
                    '"detail":"$context.error.validationErrorString"}'
                )
            },
        )

        # Every vote is kept on the bus itself for 30 days, whether or not Dynatrace is
        # configured, so the signal is not lost while the tenant details are missing and a
        # replay can refill Dynatrace afterwards. Long term storage is a Firehose stream to
        # S3, which is more than a vote a day needs (docs/proposals/feedback.md).
        events.Archive(
            self,
            "FeedbackArchive",
            source_event_bus=feedback_bus,
            archive_name="guppi-gpt-feedback",
            description="Reply votes, kept for replay",
            retention=Duration.days(FEEDBACK_ARCHIVE_RETENTION_DAYS),
            event_pattern=events.EventPattern(source=[FEEDBACK_EVENT_SOURCE]),
        )

        # Dynatrace business events, under the same switch as log forwarding: an API
        # destination posting to the tenant's /api/v2/bizevents/ingest endpoint with the
        # API token in the Authorization header, and a rule that reshapes the event into
        # the flat JSON object Grail stores as top-level fields.
        feedback_connection = events.Connection(
            self,
            "DynatraceBizeventsConnection",
            connection_name="guppi-gpt-dynatrace-bizevents",
            description="Api-Token header for the Dynatrace business events endpoint",
            # Connection takes a SecretValue. cfn_parameter would carry the bare token, and
            # the header needs the "Api-Token " realm in front of it, so the value is the
            # join of the two; it reaches the template as a Ref to the no_echo parameter.
            authorization=events.Authorization.api_key(
                "Authorization",
                SecretValue.unsafe_plain_text(
                    cdk.Fn.join("", ["Api-Token ", dynatrace_api_token.value_as_string])
                ),
            ),
        )
        feedback_destination = events.ApiDestination(
            self,
            "DynatraceBizeventsDestination",
            api_destination_name="guppi-gpt-dynatrace-bizevents",
            connection=feedback_connection,
            endpoint=cdk.Fn.join("", [dynatrace_base_url, DYNATRACE_BIZEVENTS_INGEST_PATH]),
            http_method=events.HttpMethod.POST,
            rate_limit_per_second=10,
            description="Dynatrace business events ingest",
        )
        # A vote that Dynatrace refuses is worth keeping: the queue holds it for two weeks
        # rather than letting EventBridge drop it after the retries below.
        feedback_dlq = sqs.Queue(
            self,
            "FeedbackDeadLetterQueue",
            retention_period=Duration.days(14),
            enforce_ssl=True,
        )
        feedback_rule = events.Rule(
            self,
            "FeedbackToDynatrace",
            rule_name="guppi-gpt-feedback-to-dynatrace",
            description="Reply votes to Dynatrace as business events",
            event_bus=feedback_bus,
            event_pattern=events.EventPattern(source=[FEEDBACK_EVENT_SOURCE]),
            targets=[
                events_targets.ApiDestination(
                    feedback_destination,
                    # Grail keeps every top-level attribute as a top-level field, so the
                    # body is flat and dotted names are field names, not nesting.
                    event=events.RuleTargetInput.from_object(
                        {
                            "event.type": DYNATRACE_FEEDBACK_EVENT_TYPE,
                            "event.provider": DYNATRACE_EVENT_PROVIDER,
                            "vote": events.EventField.from_path("$.detail.vote"),
                            "run.id": events.EventField.from_path("$.detail.runId"),
                            "trace.id": events.EventField.from_path("$.detail.traceId"),
                            "thread.id": events.EventField.from_path("$.detail.threadId"),
                            "message.id": events.EventField.from_path("$.detail.messageId"),
                            "request.id": events.EventField.from_path("$.detail.requestId"),
                            "received_at": events.EventField.from_path("$.detail.receivedAt"),
                        }
                    ),
                    dead_letter_queue=feedback_dlq,
                    retry_attempts=2,
                )
            ],
        )
        # A vote in the dead letter queue is a vote Dynatrace never saw. EventBridge
        # reports nothing when a target keeps failing, so the queue depth is the signal:
        # any message at all, and the alarm goes to the same topic as the rest.
        feedback_dlq_alarm = cloudwatch.Alarm(
            self,
            "FeedbackDeadLetterAlarm",
            alarm_description="A reply vote landed in the feedback dead letter queue",
            metric=feedback_dlq.metric_approximate_number_of_messages_visible(
                period=Duration.minutes(5), statistic="Maximum"
            ),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        feedback_dlq_alarm.add_alarm_action(cloudwatch_actions.SnsAction(alarm_topic))
        for construct in (
            feedback_connection,
            feedback_destination,
            feedback_dlq,
            feedback_rule,
            feedback_dlq_alarm,
        ):
            _apply_condition(construct, has_dynatrace_logs)

        # The origin the /api/feedback behavior below points at: the API's regional
        # endpoint, with the stage as the origin path so the browser's /api/feedback
        # reaches /prod/api/feedback. It carries no X-Origin-Verify header, unlike the edge
        # gateway origin, because this API authorizes every request itself; a caller who
        # finds the execute-api hostname is refused by the Cognito authorizer.
        feedback_api_origin = origins.HttpOrigin(
            f"{feedback_api.rest_api_id}.execute-api.{self.region}.amazonaws.com",
            origin_path=f"/{FEEDBACK_STAGE_NAME}",
            protocol_policy=cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
        )

        # The origin for /api/invite and /api/invite/approve: the invites API's regional
        # endpoint under its stage, as for the feedback API. These calls come from people
        # who are not signed in, so the API has no authorizer; its validator, throttling and
        # the table's conditions are what it has.
        invites_api_origin = origins.HttpOrigin(
            invites.origin_domain,
            origin_path=invites.origin_path,
            protocol_policy=cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
        )

        # ---- Site and CloudFront -------------------------------------------------------
        site_bucket = s3.Bucket(
            self,
            "SiteBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        gateway_host = Fn.select(2, Fn.split("/", gateway.attr_gateway_url))
        gateway_origin = origins.HttpOrigin(
            gateway_host,
            protocol_policy=cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
            read_timeout=ORIGIN_RESPONSE_TIMEOUT,
            keepalive_timeout=Duration.seconds(60),
            # Lets the CloudFrontOnly WAF rule tell CloudFront's traffic from anyone who
            # calls the gateway hostname directly; the value is a secretsmanager dynamic
            # reference (see OriginVerifySecret above), never a literal in the template.
            custom_headers={ORIGIN_HEADER_NAME: origin_secret_value},
        )
        # The beacon origin is a CloudFormation parameter, so its value is unknown at
        # synth time; Fn::If picks between two whole CSP strings at deploy time rather
        # than the Python code trying to interpolate it (the same Fn::If-plus-condition
        # pattern as has_alarm_email above, applied to a property value instead of a
        # resource's Condition). With DynatraceBeaconOrigin left blank (the default) this
        # renders to the exact CSP the stack already had.
        csp_without_dynatrace = (
            "default-src 'self'; "
            f"connect-src 'self' https://{AUTH_HOST}; "
            "img-src 'self' data:; "
            "style-src 'self'; "
            "script-src 'self'; "
            "frame-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'"
        )
        csp_with_dynatrace = (
            "default-src 'self'; "
            f"connect-src 'self' https://{AUTH_HOST} {dynatrace_beacon_origin.value_as_string}; "
            "img-src 'self' data:; "
            "style-src 'self'; "
            "script-src 'self'; "
            "frame-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'"
        )
        content_security_policy = cdk.Token.as_string(
            cdk.Fn.condition_if(
                has_dynatrace_beacon_origin.logical_id,
                csp_with_dynatrace,
                csp_without_dynatrace,
            )
        )
        security_headers_policy = cloudfront.ResponseHeadersPolicy(
            self,
            "SecurityHeadersPolicy",
            comment="CSP and security headers for the static page",
            security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                content_security_policy=cloudfront.ResponseHeadersContentSecurityPolicy(
                    content_security_policy=content_security_policy,
                    override=True,
                ),
                strict_transport_security=cloudfront.ResponseHeadersStrictTransportSecurity(
                    access_control_max_age=Duration.days(365),
                    include_subdomains=True,
                    override=True,
                ),
                content_type_options=cloudfront.ResponseHeadersContentTypeOptions(override=True),
                referrer_policy=cloudfront.ResponseHeadersReferrerPolicy(
                    referrer_policy=cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN,
                    override=True,
                ),
                frame_options=cloudfront.ResponseHeadersFrameOptions(
                    frame_option=cloudfront.HeadersFrameOption.DENY, override=True
                ),
            ),
        )
        # /sandbox/* gets its own headers: the app frame's CSP in place of the page's, and
        # no X-Frame-Options, since frame-ancestors 'self' is what lets the page frame it.
        sandbox_headers_policy = cloudfront.ResponseHeadersPolicy(
            self,
            "SandboxHeadersPolicy",
            comment="CSP and security headers for the MCP Apps sandbox frame",
            security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                content_security_policy=cloudfront.ResponseHeadersContentSecurityPolicy(
                    content_security_policy=SANDBOX_CSP,
                    override=True,
                ),
                strict_transport_security=cloudfront.ResponseHeadersStrictTransportSecurity(
                    access_control_max_age=Duration.days(365),
                    include_subdomains=True,
                    override=True,
                ),
                content_type_options=cloudfront.ResponseHeadersContentTypeOptions(override=True),
                referrer_policy=cloudfront.ResponseHeadersReferrerPolicy(
                    referrer_policy=cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN,
                    override=True,
                ),
            ),
        )
        page_path_function = cloudfront.Function(
            self,
            "PagePathFunction",
            code=cloudfront.FunctionCode.from_file(file_path=str(FUNCTIONS_DIR / "page-path.js")),
            runtime=cloudfront.FunctionRuntime.JS_2_0,
            comment="Serves /p/<name>/ from /index.html",
        )
        agent_path_function = cloudfront.Function(
            self,
            "AgentPathFunction",
            code=cloudfront.FunctionCode.from_file(file_path=str(FUNCTIONS_DIR / "agent-path.js")),
            runtime=cloudfront.FunctionRuntime.JS_2_0,
            comment="Maps /api/<name>/invocations to the gateway target <name>",
        )
        site_origin = origins.S3BucketOrigin.with_origin_access_control(site_bucket)
        distribution = cloudfront.Distribution(
            self,
            "Distribution",
            comment="GuppiGPT",
            domain_names=[CHAT_HOST],
            certificate=chat_cert,
            default_root_object="index.html",
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
            http_version=cloudfront.HttpVersion.HTTP2_AND_3,
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
            default_behavior=cloudfront.BehaviorOptions(
                origin=site_origin,
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                response_headers_policy=security_headers_policy,
                function_associations=[
                    cloudfront.FunctionAssociation(
                        function=page_path_function,
                        event_type=cloudfront.FunctionEventType.VIEWER_REQUEST,
                    )
                ],
            ),
            # CloudFront compares the request path against these patterns in the order
            # they are listed, so the feedback and invite paths come before the wildcard that
            # would otherwise swallow them and send them to the edge gateway.
            additional_behaviors={
                "/api/feedback": cloudfront.BehaviorOptions(
                    origin=feedback_api_origin,
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                    cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                    origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
                ),
                "/api/invite*": cloudfront.BehaviorOptions(
                    origin=invites_api_origin,
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                    cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                    origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
                ),
                "/api/*": cloudfront.BehaviorOptions(
                    origin=gateway_origin,
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                    cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                    # Forwarding Host breaks the gateway's TLS and routing.
                    origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
                    compress=False,
                    function_associations=[
                        cloudfront.FunctionAssociation(
                            function=agent_path_function,
                            event_type=cloudfront.FunctionEventType.VIEWER_REQUEST,
                        )
                    ],
                ),
                SANDBOX_PATH_PATTERN: cloudfront.BehaviorOptions(
                    origin=site_origin,
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                    response_headers_policy=sandbox_headers_policy,
                ),
            },
        )
        for record_type, record_class in (("A", route53.ARecord), ("AAAA", route53.AaaaRecord)):
            record_class(
                self,
                f"Chat{record_type}Record",
                zone=zone,
                record_name="chat",
                target=route53.RecordTarget.from_alias(targets.CloudFrontTarget(distribution)),
            )

        # ---- Knowledge base ------------------------------------------------------------
        content_bucket = s3.Bucket(
            self,
            "ContentBucket",
            versioned=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        kb_role = iam.Role(
            self,
            "KnowledgeBaseRole",
            assumed_by=iam.ServicePrincipal(
                "bedrock.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account},
                    "ArnLike": {
                        "aws:SourceArn": (
                            f"arn:aws:bedrock:{self.region}:{self.account}:knowledge-base/*"
                        )
                    },
                },
            ),
            description="Lets the managed knowledge base list and read the content bucket",
        )
        content_bucket.grant_read(kb_role)
        knowledge_base = bedrock.CfnKnowledgeBase(
            self,
            "KnowledgeBase",
            name=KB_NAME,
            description="MCP, Strands Agents, and AG-UI documentation",
            role_arn=kb_role.role_arn,
            knowledge_base_configuration=bedrock.CfnKnowledgeBase.KnowledgeBaseConfigurationProperty(
                type="MANAGED",
                managed_knowledge_base_configuration=(
                    bedrock.CfnKnowledgeBase.ManagedKnowledgeBaseConfigurationProperty(
                        embedding_model_type="MANAGED"
                    )
                ),
            ),
        )
        knowledge_base.node.add_dependency(kb_role)
        # Deletion protection is off so that a seed run that renames or removes many files
        # is mirrored by the next ingestion instead of being skipped past a threshold.
        data_source = bedrock.CfnDataSource(
            self,
            "ContentSource",
            name="content-bucket",
            description="Markdown synced by scripts/seed-content.sh",
            knowledge_base_id=knowledge_base.attr_knowledge_base_id,
            data_deletion_policy="DELETE",
            data_source_configuration=bedrock.CfnDataSource.DataSourceConfigurationProperty(
                type="MANAGED_KNOWLEDGE_BASE_CONNECTOR",
                managed_knowledge_base_connector_configuration=(
                    bedrock.CfnDataSource.ManagedKnowledgeBaseConnectorConfigurationProperty(
                        connector_parameters={
                            "type": "S3",
                            "version": "1",
                            "connectionConfiguration": {
                                "bucketName": content_bucket.bucket_name,
                                "bucketOwnerAccountId": self.account,
                            },
                            "filterConfiguration": {"inclusionPrefixes": [CONTENT_PREFIX]},
                        },
                        deletion_protection_configuration=(
                            bedrock.CfnDataSource.DeletionProtectionConfigurationProperty(
                                deletion_protection_status="DISABLED"
                            )
                        ),
                    )
                ),
            ),
        )

        # Nightly incremental ingestion as a scheduler universal target: the SDK call is
        # scheduler configuration, with no function between the schedule and the API.
        ingestion = scheduler.Schedule(
            self,
            "NightlyIngestion",
            description="Incremental ingestion of the content bucket into the knowledge base",
            schedule=scheduler.ScheduleExpression.cron(minute="0", hour="9"),
            target=scheduler_targets.Universal(
                service="bedrockagent",  # SDK client name: aws-sdk:bedrockagent:startIngestionJob
                action="startIngestionJob",
                input=scheduler.ScheduleTargetInput.from_object(
                    {
                        "KnowledgeBaseId": knowledge_base.attr_knowledge_base_id,
                        "DataSourceId": data_source.attr_data_source_id,
                    }
                ),
                policy_statements=[
                    iam.PolicyStatement(
                        actions=["bedrock:StartIngestionJob"],
                        resources=[knowledge_base.attr_knowledge_base_arn],
                    )
                ],
            ),
        )
        ingestion_alarm = cloudwatch.Alarm(
            self,
            "IngestionScheduleErrors",
            alarm_description="The nightly StartIngestionJob call failed",
            metric=scheduler.Schedule.metric_all_errors(period=Duration.days(1)),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        ingestion_alarm.add_alarm_action(cloudwatch_actions.SnsAction(alarm_topic))

        # ---- Conversation log ------------------------------------------------------------
        # One JSON object per thread under threads/, written by the runtime after a run's
        # stream ends. Encryption is SSE-KMS with a stack-created key rather than SSE-S3: the
        # key is a second gate, so a principal holding s3:GetObject but no kms:Decrypt on this
        # key reads nothing, and every decrypt is a CloudTrail event. The investigator path
        # costs one grant for that, which is cheaper than the gate is worth.
        conversation_key = kms.Key(
            self,
            "ConversationLogKey",
            description="Encrypts the GuppiGPT conversation log bucket",
            enable_key_rotation=True,
            alias="guppi-gpt-conversations",
            removal_policy=RemovalPolicy.RETAIN,
        )
        conversation_bucket = s3.Bucket(
            self,
            "ConversationLogBucket",
            versioned=True,  # one version per run, which is the turn-by-turn history
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=conversation_key,
            bucket_key_enabled=True,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="ExpireThreadRecords",
                    prefix=THREADS_PREFIX,
                    expiration=Duration.days(CONVERSATION_RETENTION_DAYS),
                    noncurrent_version_expiration=Duration.days(CONVERSATION_RETENTION_DAYS),
                    abort_incomplete_multipart_upload_after=Duration.days(1),
                )
            ],
        )
        # The secret value is the HMAC key itself, with no JSON template around it, so the
        # agent uses the bytes of the secret string as they come back.
        conversation_secret = secretsmanager.Secret(
            self,
            "ConversationLogKeySecret",
            description="HMAC key that turns a Cognito sub into the pseudonym in thread records",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                password_length=32, exclude_punctuation=True
            ),
        )

        # The investigator role is the only principal outside the runtime that can read a
        # thread record, and the only one that can read the key and list the user pool, which
        # is what re-identification takes. Trust is the account root by default, so any
        # principal in the account holding sts:AssumeRole can assume it; a non-blank
        # InvestigatorPrincipalArn narrows it to that one ARN.
        investigator_role = iam.Role(
            self,
            "ConversationInvestigatorRole",
            assumed_by=iam.AccountRootPrincipal(),
            max_session_duration=Duration.hours(1),
            description="Reads conversation records and resolves a subject to a Cognito user",
        )
        cfn_investigator_role = investigator_role.node.default_child
        assert isinstance(cfn_investigator_role, iam.CfnRole)
        cfn_investigator_role.add_property_override(
            "AssumeRolePolicyDocument.Statement.0.Principal.AWS",
            cdk.Fn.condition_if(
                has_investigator_principal.logical_id,
                investigator_principal_arn.value_as_string,
                f"arn:aws:iam::{self.account}:root",
            ),
        )
        thread_objects = conversation_bucket.arn_for_objects(f"{THREADS_PREFIX}*")
        investigator_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject", "s3:GetObjectVersion"], resources=[thread_objects]
            )
        )
        investigator_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:ListBucket", "s3:ListBucketVersions"],
                resources=[conversation_bucket.bucket_arn],
            )
        )
        conversation_key.grant(investigator_role, "kms:Decrypt")
        investigator_role.add_to_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"],
                resources=[conversation_secret.secret_arn],
            )
        )
        investigator_role.add_to_policy(
            iam.PolicyStatement(
                actions=["cognito-idp:ListUsers"], resources=[user_pool.user_pool_arn]
            )
        )

        # The runtime reads and writes single objects by key, and no delete. ListBucket is
        # granted only under the threads/ prefix: without it S3 answers a GET on a missing
        # key with 403 instead of 404 (observed on the first run, 5 Sep 2026), so the
        # first write of every thread failed.
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject", "s3:PutObject"], resources=[thread_objects]
            )
        )
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:ListBucket"],
                resources=[conversation_bucket.bucket_arn],
                conditions={"StringLike": {"s3:prefix": [f"{THREADS_PREFIX}*"]}},
            )
        )
        conversation_key.grant(runtime_role, "kms:Decrypt", "kms:GenerateDataKey")
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"],
                resources=[conversation_secret.secret_arn],
            )
        )

        conversation_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="RuntimeThreadRecords",
                principals=[iam.ArnPrincipal(runtime_role.role_arn)],
                actions=["s3:GetObject", "s3:PutObject"],
                resources=[thread_objects],
            )
        )
        conversation_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="RuntimeThreadListing",
                principals=[iam.ArnPrincipal(runtime_role.role_arn)],
                actions=["s3:ListBucket"],
                resources=[conversation_bucket.bucket_arn],
                conditions={"StringLike": {"s3:prefix": [f"{THREADS_PREFIX}*"]}},
            )
        )
        conversation_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="InvestigatorReads",
                principals=[iam.ArnPrincipal(investigator_role.role_arn)],
                actions=[
                    "s3:GetObject",
                    "s3:GetObjectVersion",
                    "s3:ListBucket",
                    "s3:ListBucketVersions",
                ],
                resources=[conversation_bucket.bucket_arn, thread_objects],
            )
        )
        # The deny covers reads only, so an administrator keeps the ability to repair the
        # policy and to delete objects for a data subject deletion.
        conversation_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="DenyOtherReaders",
                effect=iam.Effect.DENY,
                principals=[iam.AnyPrincipal()],
                actions=["s3:GetObject", "s3:GetObjectVersion"],
                resources=[thread_objects],
                conditions={
                    "StringNotEquals": {
                        "aws:PrincipalArn": [
                            runtime_role.role_arn,
                            investigator_role.role_arn,
                        ]
                    }
                },
            )
        )

        # ---- Tools gateway -------------------------------------------------------------
        tools_gateway_role = iam.Role(
            self,
            "ToolsGatewayRole",
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account},
                    "ArnLike": {
                        "aws:SourceArn": (
                            f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:gateway/*"
                        )
                    },
                },
            ),
            description="Lets the tools gateway retrieve from the GuppiGPT knowledge base",
        )
        tools_gateway_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:GetKnowledgeBase", "bedrock:Retrieve"],
                resources=[knowledge_base.attr_knowledge_base_arn],
            )
        )
        # AgenticRetrieveStream is not resource-scoped; the gateway target validation asks for it.
        tools_gateway_role.add_to_policy(
            iam.PolicyStatement(actions=["bedrock:AgenticRetrieveStream"], resources=["*"])
        )
        tools_gateway = agentcore.CfnGateway(
            self,
            "ToolsGateway",
            name=TOOLS_GATEWAY_NAME,
            description="GuppiGPT tools: the knowledge base as MCP tools, user JWT inbound",
            role_arn=tools_gateway_role.role_arn,
            protocol_type="MCP",
            authorizer_type="CUSTOM_JWT",
            authorizer_configuration=agentcore.CfnGateway.AuthorizerConfigurationProperty(
                custom_jwt_authorizer=agentcore.CfnGateway.CustomJWTAuthorizerConfigurationProperty(
                    discovery_url=discovery_url,
                    allowed_clients=jwt_allowed_clients,
                )
            ),
            exception_level="DEBUG",
        )
        kb_target = agentcore.CfnGatewayTarget(
            self,
            "KnowledgeBaseTarget",
            gateway_identifier=tools_gateway.attr_gateway_identifier,
            name=KB_TARGET_NAME,
            description="GuppiGPT documentation knowledge base",
            target_configuration=agentcore.CfnGatewayTarget.TargetConfigurationProperty(
                mcp=agentcore.CfnGatewayTarget.McpTargetConfigurationProperty(
                    connector=agentcore.CfnGatewayTarget.ConnectorTargetConfigurationProperty(
                        source=agentcore.CfnGatewayTarget.ConnectorSourceProperty(
                            connector_id="bedrock-knowledge-bases"
                        ),
                        configurations=[
                            agentcore.CfnGatewayTarget.ConnectorConfigurationProperty(
                                name="Retrieve",
                                description=(
                                    "Search the MCP, Strands Agents, and AG-UI documentation "
                                    "and return the most relevant passages."
                                ),
                                # No retrievalConfiguration default: CloudFormation stores the
                                # JSON numbers in ParameterValues as strings, and the knowledge
                                # base rejects a string numberOfResults. Service defaults apply.
                                parameter_values={
                                    "knowledgeBaseId": knowledge_base.attr_knowledge_base_id,
                                },
                            ),
                            agentcore.CfnGatewayTarget.ConnectorConfigurationProperty(
                                name="AgenticRetrieveStream",
                                parameter_values={
                                    "retrievers": [
                                        {
                                            "description": "MCP, Strands Agents, and AG-UI docs",
                                            "configuration": {
                                                "knowledgeBase": {
                                                    "knowledgeBaseId": (
                                                        knowledge_base.attr_knowledge_base_id
                                                    )
                                                }
                                            },
                                        }
                                    ],
                                    "agenticRetrieveConfiguration": {
                                        "foundationModelType": "MANAGED",
                                        "rerankingModelType": "MANAGED",
                                    },
                                },
                            ),
                        ],
                    )
                )
            ),
            credential_provider_configurations=[
                agentcore.CfnGatewayTarget.CredentialProviderConfigurationProperty(
                    credential_provider_type="GATEWAY_IAM_ROLE"
                )
            ],
        )
        kb_target.node.add_dependency(tools_gateway_role)
        kb_target.node.add_dependency(data_source)

        # Dynatrace trace export, shipped dark. The runtime's own OTEL_EXPORTER_OTLP_*
        # values are injected by the AgentCore platform when AGENT_OBSERVABILITY_ENABLED
        # is set (see RUNTIME_BASE_ENVIRONMENT above); OTEL's env var scheme carries only
        # one endpoint per signal, so this does not add a second export destination
        # beside CloudWatch, it redirects trace export to Dynatrace once both parameters
        # are set (docs/proposals/dynatrace.md covers the tradeoff and what was not
        # possible to confirm without a real deploy). Fn::If's AWS::NoValue branch omits
        # the two keys entirely while DynatraceOtlpEndpoint or DynatraceApiToken is
        # blank, which is the default, so the environment the container sees today does
        # not change until Sam supplies both.
        dynatrace_traces_endpoint = cdk.Token.as_string(
            cdk.Fn.condition_if(
                has_dynatrace_otlp.logical_id,
                f"{dynatrace_otlp_endpoint.value_as_string}/v1/traces",
                cdk.Aws.NO_VALUE,
            )
        )
        # The API token reaches the runtime through Secrets Manager, never its environment,
        # where GetAgentRuntime would show it to anyone allowed to read the runtime
        # (docs/proposals/dynatrace.md, "The token in Secrets Manager"). The secret always
        # exists so the role's grant has something to name; its value is the token once
        # both parameters are set. The image's launcher (agent/otel_headers.py) reads it and
        # sets OTEL_EXPORTER_OTLP_TRACES_HEADERS inside the process before
        # opentelemetry-instrument starts.
        dynatrace_token_secret = secretsmanager.Secret(
            self,
            "DynatraceTokenSecret",
            description="Dynatrace API token for the runtime's trace export",
            secret_string_value=SecretValue.unsafe_plain_text(
                cdk.Token.as_string(
                    cdk.Fn.condition_if(
                        has_dynatrace_otlp.logical_id,
                        dynatrace_api_token.value_as_string,
                        "unset",
                    )
                )
            ),
        )
        dynatrace_token_secret.grant_read(runtime_role)
        dynatrace_token_secret_arn = cdk.Token.as_string(
            cdk.Fn.condition_if(
                has_dynatrace_otlp.logical_id,
                dynatrace_token_secret.secret_arn,
                cdk.Aws.NO_VALUE,
            )
        )

        # The tools gateway now exists, so the runtime's environment can point at it.
        runtime.environment_variables = {
            **RUNTIME_BASE_ENVIRONMENT,
            "TOOLS_GATEWAY_URL": tools_gateway.attr_gateway_url,
            "MODEL_ID": MODEL_ID,
            "RETRIEVE_TOOL": RETRIEVE_TOOL,
            "CONVERSATION_LOG_ENABLED": "true" if CONVERSATION_LOG_ENABLED else "false",
            "CONVERSATION_LOG_BUCKET": conversation_bucket.bucket_name,
            "CONVERSATION_LOG_KEY_SECRET_ARN": conversation_secret.secret_arn,
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": dynatrace_traces_endpoint,
            "DYNATRACE_TOKEN_SECRET_ARN": dynatrace_token_secret_arn,
        }

        # ---- Operational alarms ----------------------------------------------------------
        # docs/proposals/operations.md records the thresholds and why. Every metric here is
        # in the AWS/Bedrock-AgentCore namespace with a "Resource" dimension carrying the
        # resource's own ARN; the gateway devguide page states that dimension for gateway
        # invocation metrics (observability-gateway-metrics.html), and the runtime page
        # (observability-runtime-metrics.html) lists the same metric names without a
        # dimensions table, so using "Resource" there too is an assumption, not something
        # the docs state outright (the same caveat the WafBlocks alarms above already carry
        # for GatewayId).
        def _resource_error_alarm(construct_id: str, description: str, resource_arn: str) -> None:
            alarm = cloudwatch.Alarm(
                self,
                construct_id,
                alarm_description=description,
                metric=cloudwatch.Metric(
                    namespace="AWS/Bedrock-AgentCore",
                    metric_name="SystemErrors",
                    dimensions_map={"Resource": resource_arn},
                    statistic="Sum",
                    period=Duration.minutes(5),
                ),
                threshold=1,
                evaluation_periods=1,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )
            alarm.add_alarm_action(cloudwatch_actions.SnsAction(alarm_topic))

        _resource_error_alarm(
            "EdgeGateway5xxAlarm",
            "5xx (SystemErrors) on the GuppiGPT edge gateway crossed zero",
            gateway.attr_gateway_arn,
        )
        _resource_error_alarm(
            "ToolsGateway5xxAlarm",
            "5xx (SystemErrors) on the GuppiGPT tools gateway crossed zero",
            tools_gateway.attr_gateway_arn,
        )
        _resource_error_alarm(
            "Runtime5xxAlarm",
            "5xx (SystemErrors) on the GuppiGPT runtime crossed zero",
            runtime.attr_agent_runtime_arn,
        )

        # UserErrors as a share of Invocations: a math expression rather than a raw count,
        # since occasional 4xx (an expired token, a malformed request) is expected traffic
        # and only a rate says whether it is worth looking at.
        edge_gateway_4xx_rate = cloudwatch.MathExpression(
            expression="(userErrors / invocations) * 100",
            using_metrics={
                "userErrors": cloudwatch.Metric(
                    namespace="AWS/Bedrock-AgentCore",
                    metric_name="UserErrors",
                    dimensions_map={"Resource": gateway.attr_gateway_arn},
                    statistic="Sum",
                ),
                "invocations": cloudwatch.Metric(
                    namespace="AWS/Bedrock-AgentCore",
                    metric_name="Invocations",
                    dimensions_map={"Resource": gateway.attr_gateway_arn},
                    statistic="Sum",
                ),
            },
            period=Duration.minutes(5),
            label="EdgeGateway4xxRate",
        )
        edge_gateway_4xx_rate_alarm = cloudwatch.Alarm(
            self,
            "EdgeGateway4xxRateAlarm",
            alarm_description=(
                "4xx (UserErrors) rate on the GuppiGPT edge gateway crossed "
                f"{EDGE_GATEWAY_4XX_RATE_THRESHOLD_PERCENT}%"
            ),
            metric=edge_gateway_4xx_rate,
            threshold=EDGE_GATEWAY_4XX_RATE_THRESHOLD_PERCENT,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        edge_gateway_4xx_rate_alarm.add_alarm_action(cloudwatch_actions.SnsAction(alarm_topic))

        # Runtime "Latency" is end-to-end (receipt to final token), the same quantity the
        # gateway table calls "Duration". Threshold: RUNTIME_LATENCY_P90_THRESHOLD_MS above.
        runtime_latency_p90_alarm = cloudwatch.Alarm(
            self,
            "RuntimeLatencyP90Alarm",
            alarm_description=(
                "GuppiGPT runtime invocation latency p90 crossed "
                f"{RUNTIME_LATENCY_P90_THRESHOLD_MS} ms"
            ),
            metric=cloudwatch.Metric(
                namespace="AWS/Bedrock-AgentCore",
                metric_name="Latency",
                dimensions_map={"Resource": runtime.attr_agent_runtime_arn},
                statistic="p90",
                period=Duration.minutes(5),
            ),
            threshold=RUNTIME_LATENCY_P90_THRESHOLD_MS,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        runtime_latency_p90_alarm.add_alarm_action(cloudwatch_actions.SnsAction(alarm_topic))

        # Bedrock throttling for the one inference profile the runtime calls.
        bedrock_throttling_alarm = cloudwatch.Alarm(
            self,
            "BedrockThrottlingAlarm",
            alarm_description="Bedrock InvocationThrottles for the GuppiGPT model crossed zero",
            metric=cloudwatch.Metric(
                namespace="AWS/Bedrock",
                metric_name="InvocationThrottles",
                dimensions_map={"ModelId": MODEL_ID},
                statistic="Sum",
                period=Duration.minutes(5),
            ),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        bedrock_throttling_alarm.add_alarm_action(cloudwatch_actions.SnsAction(alarm_topic))

        # ---- Vended log delivery ----------------------------------------------------------
        # AWS::Logs::DeliverySource, AWS::Logs::DeliveryDestination, and AWS::Logs::Delivery
        # (the CDK L1s below) wire each resource's APPLICATION_LOGS into a CloudWatch Logs
        # group under VENDED_LOG_PREFIX. The gateways' TRACES log type is not delivered here:
        # CloudFormation rejected a CloudWatch Logs destination for it on 4 Sep 2026 with
        # "Invalid destination type provided for this resource and log type", so gateway
        # traces would need an X-Ray destination, and the runtime's spans already reach
        # Transaction Search. The resource policy below
        # grants delivery.logs.amazonaws.com permission to write to the log groups; without
        # it, only a principal with logs:PutResourcePolicy on the log group gets one created
        # automatically the first time delivery starts (AWS-logs-infrastructure-V2-
        # CloudWatchLogs.html), which the deploying principal is not guaranteed to have.
        def _vended_log_delivery(
            resource_label: str, resource_name: str, resource_arn: str, log_types: list[str]
        ) -> logs.LogGroup:
            log_group = logs.LogGroup(
                self,
                f"{resource_label}LogGroup",
                log_group_name=f"{VENDED_LOG_PREFIX}/{resource_name}",
                retention=VENDED_LOG_RETENTION,
                removal_policy=RemovalPolicy.DESTROY,
            )
            destination = logs.CfnDeliveryDestination(
                self,
                f"{resource_label}LogDeliveryDestination",
                name=f"{resource_name}-logs".replace("_", "-"),
                delivery_destination_type="CWL",
                destination_resource_arn=log_group.log_group_arn,
            )
            for log_type in log_types:
                source = logs.CfnDeliverySource(
                    self,
                    f"{resource_label}{log_type.title().replace('_', '')}Source",
                    name=f"{resource_name}-{log_type}".replace("_", "-").lower(),
                    log_type=log_type,
                    resource_arn=resource_arn,
                )
                delivery = logs.CfnDelivery(
                    self,
                    f"{resource_label}{log_type.title().replace('_', '')}Delivery",
                    delivery_source_name=source.name,
                    delivery_destination_arn=destination.attr_arn,
                )
                delivery.node.add_dependency(source)
                delivery.node.add_dependency(destination)

            return log_group

        vended_log_groups = {
            "EdgeGateway": _vended_log_delivery(
                "EdgeGateway", GATEWAY_NAME, gateway.attr_gateway_arn, ["APPLICATION_LOGS"]
            ),
            "ToolsGateway": _vended_log_delivery(
                "ToolsGateway",
                TOOLS_GATEWAY_NAME,
                tools_gateway.attr_gateway_arn,
                ["APPLICATION_LOGS"],
            ),
            "Runtime": _vended_log_delivery(
                "Runtime", RUNTIME_NAME, runtime.attr_agent_runtime_arn, ["APPLICATION_LOGS"]
            ),
        }

        # Recommended prefix policy (AWS-logs-infrastructure-V2-CloudWatchLogs.html) rather
        # than one statement per log group, so a fourth vended-log destination needs no
        # policy change.
        logs.CfnResourcePolicy(
            self,
            "VendedLogDeliveryPolicy",
            policy_name="GuppiGptVendedLogDelivery",
            policy_document=json.dumps(
                iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            sid="AWSLogDeliveryWrite20150319",
                            effect=iam.Effect.ALLOW,
                            principals=[iam.ServicePrincipal("delivery.logs.amazonaws.com")],
                            actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                            resources=[
                                f"arn:aws:logs:{self.region}:{self.account}:log-group:"
                                f"{VENDED_LOG_PREFIX}/*"
                            ],
                            conditions={
                                "StringEquals": {"aws:SourceAccount": self.account},
                                "ArnLike": {
                                    "aws:SourceArn": (
                                        f"arn:aws:logs:{self.region}:{self.account}:*"
                                    )
                                },
                            },
                        )
                    ]
                ).to_json()
            ),
        )

        # ---- Dynatrace AWS integration ----------------------------------------------------
        # The log forwarding stream is shipped dark: every parameter above defaults empty,
        # so the condition below renders to nothing until Sam has a Dynatrace tenant
        # (docs/proposals/dynatrace.md). Dynatrace's own push-based AWS activation stack,
        # deployed outside this repo on 7 Sep 2026, polls CloudWatch on its own; the
        # role-based monitoring role this stack once created for that purpose was removed
        # the same day.
        # Log forwarding: a Firehose delivery stream with the "Dynatrace" HTTP endpoint
        # destination (docs.dynatrace.com/docs/ingest-from/amazon-web-services/
        # integrate-with-aws/aws-logs-ingest/lma-stream-logs-with-firehose), subscribed to
        # the three vended log groups above. The runtime's own log group
        # (/aws/bedrock-agentcore/runtimes/guppi_gpt-*) is created lazily by the service,
        # not by this stack (only _runtime_role's policy names its pattern); a
        # CloudFormation subscription filter needs an exact, stack-owned log group, so that
        # group is a follow-up, not something this change subscribes.
        #
        # The log ingest URL is derived from DynatraceOtlpEndpoint (Fn::Split on its fixed
        # "/api/v2/otlp" suffix recovers the tenant's base URL, then Fn::Join appends the
        # logs ingest path) rather than a separate DynatraceLogsEndpoint parameter: the
        # OTLP endpoint already names the tenant, and asking Sam to enter the same tenant a
        # second time would be redundant and one more way for the two to drift apart.
        dynatrace_logs_endpoint = cdk.Fn.join("", [dynatrace_base_url, DYNATRACE_LOGS_INGEST_PATH])

        # Failed deliveries only, in a small bucket of its own: the site and content
        # buckets are not reused for this.
        dynatrace_log_backup_bucket = s3.Bucket(
            self,
            "DynatraceLogBackupBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            lifecycle_rules=[
                s3.LifecycleRule(id="ExpireFailedDeliveries", expiration=Duration.days(7))
            ],
        )
        dynatrace_firehose_role = iam.Role(
            self,
            "DynatraceFirehoseRole",
            assumed_by=iam.ServicePrincipal("firehose.amazonaws.com"),
            description=(
                "Lets Firehose write failed Dynatrace log deliveries to the backup bucket"
            ),
            inline_policies={
                "BackupBucketWrite": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            actions=["s3:PutObject", "s3:GetBucketLocation", "s3:ListBucket"],
                            resources=[
                                dynatrace_log_backup_bucket.bucket_arn,
                                dynatrace_log_backup_bucket.arn_for_objects("*"),
                            ],
                        )
                    ]
                )
            },
        )

        # Buffer hints, content encoding, and the backup mode default (failed data only)
        # follow docs.dynatrace.com/docs/ingest-from/amazon-web-services/integrate-with-aws/
        # aws-logs-ingest/lma-stream-logs-with-firehose: 1 MiB or 60 seconds, GZIP, the
        # API token as the destination's access key.
        dynatrace_http_destination = firehose.HttpEndpoint(
            endpoint_config=firehose.HttpEndpointConfig(
                url=dynatrace_logs_endpoint,
                name="Dynatrace",
                access_key=SecretValue.cfn_parameter(dynatrace_api_token),
            ),
            buffering_hints=firehose.HttpBufferingHints(
                interval=Duration.seconds(60), size=Size.mebibytes(1)
            ),
            request_compression=firehose.HttpCompression.GZIP,
            # The default error log group has no expiry and this stack retains everything
            # else at 30 days (VENDED_LOG_RETENTION); disabled on both the HTTP endpoint
            # and its S3 backup rather than adding a fifth retention policy for a log
            # group failed deliveries already land in, in S3.
            s3_backup=firehose.DestinationS3BackupProps(
                bucket=dynatrace_log_backup_bucket, logging_config=firehose.DisableLogging()
            ),
            role=dynatrace_firehose_role,
            logging_config=firehose.DisableLogging(),
        )
        dynatrace_delivery_stream = firehose.DeliveryStream(
            self, "DynatraceLogDeliveryStream", destination=dynatrace_http_destination
        )

        dynatrace_logs_to_firehose_role = iam.Role(
            self,
            "DynatraceLogsToFirehoseRole",
            assumed_by=iam.ServicePrincipal("logs.amazonaws.com"),
            description=(
                "Lets CloudWatch Logs subscription filters write to the Dynatrace Firehose stream"
            ),
            inline_policies={
                "PutToFirehose": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            actions=["firehose:PutRecord", "firehose:PutRecordBatch"],
                            resources=[dynatrace_delivery_stream.delivery_stream_arn],
                        )
                    ]
                )
            },
        )
        dynatrace_firehose_destination = logs_destinations.FirehoseDestination(
            dynatrace_delivery_stream, role=dynatrace_logs_to_firehose_role
        )
        dynatrace_subscription_filters = [
            logs.SubscriptionFilter(
                self,
                f"Dynatrace{label}SubscriptionFilter",
                log_group=log_group,
                destination=dynatrace_firehose_destination,
                filter_pattern=logs.FilterPattern.all_events(),
            )
            for label, log_group in vended_log_groups.items()
        ]

        # Every resource above (the backup bucket, its own SSL-enforcement policy, both
        # roles and the default policies grant_write and the destination's internal grants
        # add to them, the delivery stream, and the three subscription filters) is
        # conditional on the same switch, applied last so every nested resource each L2
        # construct created along the way is caught.
        for construct in (
            dynatrace_log_backup_bucket,
            dynatrace_firehose_role,
            dynatrace_delivery_stream,
            dynatrace_logs_to_firehose_role,
            *dynatrace_subscription_filters,
        ):
            _apply_condition(construct, has_dynatrace_logs)

        # ---- Outputs -------------------------------------------------------------------
        cdk.CfnOutput(self, "SiteUrl", value=SITE_URL)
        cdk.CfnOutput(self, "SiteBucketName", value=site_bucket.bucket_name)
        cdk.CfnOutput(self, "DistributionId", value=distribution.distribution_id)
        cdk.CfnOutput(self, "UserPoolId", value=user_pool.user_pool_id)
        cdk.CfnOutput(self, "UserPoolClientId", value=client.user_pool_client_id)
        cdk.CfnOutput(self, "AuthDomain", value=AUTH_HOST)
        cdk.CfnOutput(self, "GatewayUrl", value=gateway.attr_gateway_url)
        cdk.CfnOutput(self, "GatewayArn", value=gateway.attr_gateway_arn)
        cdk.CfnOutput(self, "RuntimeArn", value=runtime.attr_agent_runtime_arn)
        cdk.CfnOutput(self, "RuntimeProtocol", value=protocol)
        cdk.CfnOutput(self, "ContentBucketName", value=content_bucket.bucket_name)
        cdk.CfnOutput(self, "KnowledgeBaseId", value=knowledge_base.attr_knowledge_base_id)
        cdk.CfnOutput(self, "DataSourceId", value=data_source.attr_data_source_id)
        cdk.CfnOutput(self, "ToolsGatewayUrl", value=tools_gateway.attr_gateway_url)
        cdk.CfnOutput(self, "IngestionScheduleName", value=ingestion.schedule_name)
        cdk.CfnOutput(self, "AlarmTopicArn", value=alarm_topic.topic_arn)
        cdk.CfnOutput(self, "ConversationLogBucketName", value=conversation_bucket.bucket_name)
        cdk.CfnOutput(self, "ConversationLogKeySecretArn", value=conversation_secret.secret_arn)
        cdk.CfnOutput(self, "InvestigatorRoleArn", value=investigator_role.role_arn)
        cdk.CfnOutput(
            self, "FeedbackApiUrl", value=feedback_api.url_for_path(f"/api/{FEEDBACK_PATH}")
        )
        cdk.CfnOutput(self, "FeedbackBusName", value=feedback_bus.event_bus_name)
        cdk.CfnOutput(self, "RumScriptPath", value=RUM_SCRIPT_PATH)
        cdk.CfnOutput(self, "RumBeaconOrigin", value=dynatrace_beacon_origin.value_as_string)

        # ---- Platform parameters -----------------------------------------------------
        for construct_id, parameter_name, value in (
            ("SiteBucketName", PARAM_SITE_BUCKET_NAME, site_bucket.bucket_name),
            ("DistributionId", PARAM_DISTRIBUTION_ID, distribution.distribution_id),
            ("SiteUrl", PARAM_SITE_URL, SITE_URL),
            ("EdgeGatewayId", PARAM_EDGE_GATEWAY_ID, gateway.attr_gateway_identifier),
            ("EdgeGatewayArn", PARAM_EDGE_GATEWAY_ARN, gateway.attr_gateway_arn),
            ("EdgeGatewayRoleArn", PARAM_EDGE_GATEWAY_ROLE_ARN, gateway_role.role_arn),
            ("ToolsGatewayId", PARAM_TOOLS_GATEWAY_ID, tools_gateway.attr_gateway_identifier),
            ("ToolsGatewayUrl", PARAM_TOOLS_GATEWAY_URL, tools_gateway.attr_gateway_url),
            ("ToolsGatewayRoleArn", PARAM_TOOLS_GATEWAY_ROLE_ARN, tools_gateway_role.role_arn),
            ("UserPoolClientId", PARAM_USER_POOL_CLIENT_ID, client.user_pool_client_id),
            ("JwtDiscoveryUrl", PARAM_JWT_DISCOVERY_URL, discovery_url),
            (
                "ConversationLogBucketName",
                PARAM_CONVERSATION_LOG_BUCKET_NAME,
                conversation_bucket.bucket_name,
            ),
            (
                "ConversationLogKeySecretArn",
                PARAM_CONVERSATION_LOG_KEY_SECRET_ARN,
                conversation_secret.secret_arn,
            ),
            ("AlarmTopicArn", PARAM_ALARM_TOPIC_ARN, alarm_topic.topic_arn),
            (
                "DynatraceTracesEndpoint",
                PARAM_DYNATRACE_TRACES_ENDPOINT,
                cdk.Token.as_string(
                    cdk.Fn.condition_if(
                        has_dynatrace_otlp.logical_id,
                        f"{dynatrace_otlp_endpoint.value_as_string}/v1/traces",
                        "none",
                    )
                ),
            ),
            (
                "DynatraceTokenSecretArn",
                PARAM_DYNATRACE_TOKEN_SECRET_ARN,
                dynatrace_token_secret.secret_arn,
            ),
        ):
            ssm.StringParameter(
                self,
                f"Platform{construct_id}Parameter",
                parameter_name=parameter_name,
                string_value=value,
                description="GuppiGPT platform contract, read by project stacks at deploy time",
            )

    def _runtime_role(self) -> iam.Role:
        """Execution role for the runtime, following the AgentCore documented policy."""
        role = iam.Role(
            self,
            "RuntimeRole",
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account},
                    "ArnLike": {
                        "aws:SourceArn": f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:*"
                    },
                },
            ),
            description="Execution role for the GuppiGPT agent runtime",
        )
        region, account = self.region, self.account
        role.add_to_policy(
            iam.PolicyStatement(actions=["ecr:GetAuthorizationToken"], resources=["*"])
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:DescribeLogGroups"],
                resources=[f"arn:aws:logs:{region}:{account}:log-group:*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:DescribeLogStreams",
                    "logs:PutLogEvents",
                ],
                resources=[
                    f"arn:aws:logs:{region}:{account}:log-group:/aws/bedrock-agentcore/runtimes/*"
                ],
            )
        )
        # The documented execution role adds this so the runtime can let X-Ray deliver
        # spans into the agent's own log group (the unified span destination) instead of
        # the shared aws/spans group; scoped to this runtime's log groups as the docs show.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:PutResourcePolicy"],
                resources=[
                    f"arn:aws:logs:{region}:{account}:log-group:"
                    f"/aws/bedrock-agentcore/runtimes/{RUNTIME_NAME}-*"
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "xray:PutTraceSegments",
                    "xray:PutTelemetryRecords",
                    "xray:GetSamplingRules",
                    "xray:GetSamplingTargets",
                ],
                resources=["*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
                conditions={"StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}},
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock-agentcore:GetWorkloadAccessToken",
                    "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
                    "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
                ],
                resources=[
                    f"arn:aws:bedrock-agentcore:{region}:{account}:workload-identity-directory/default",
                    f"arn:aws:bedrock-agentcore:{region}:{account}:workload-identity-directory/default/workload-identity/{RUNTIME_NAME}-*",
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=[
                    # A cross-region inference profile fans out to models in several
                    # regions, so the foundation-model wildcard stays broad; the profile
                    # itself is narrowed to the one MODEL_ID the agent calls.
                    "arn:aws:bedrock:*::foundation-model/*",
                    f"arn:aws:bedrock:{region}:{account}:inference-profile/{MODEL_ID}",
                ],
            )
        )
        return role
