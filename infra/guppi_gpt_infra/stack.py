"""The GuppiGpt stack.

DNS and certificates, Cognito with Google federation, the agent runtime, the edge
gateway with a runtime target, the tools gateway in front of the knowledge base,
CloudFront serving the page and proxying /api/* to the gateway, a regional web ACL on
the edge gateway, and the billing and WAF alarms.

Resource ordering that matters:
  apex A record -> user pool custom domain (Cognito refuses the domain without an A record)
  gateway -> runtime (runtime authorizer names the gateway as its allowed workload)
  runtime -> gateway role policy -> gateway target
  tools gateway -> runtime environment variables (the runtime needs the tools gateway url)
"""

from __future__ import annotations

from pathlib import Path

import aws_cdk as cdk
import jsii
from aws_cdk import (
    Duration,
    Fn,
    RemovalPolicy,
    SecretValue,
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
    aws_iam as iam,
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
    aws_wafv2 as wafv2,
)
from constructs import Construct

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
TOOLS_GATEWAY_NAME = "guppi-gpt-tools"
KB_TARGET_NAME = "docs"  # tools are named docs___Retrieve and docs___AgenticRetrieveStream
KB_NAME = "guppi-gpt-docs"
CONTENT_PREFIX = "docs/"  # scripts/seed-content.sh writes docs/<source>/... to the content bucket
ORIGIN_RESPONSE_TIMEOUT = Duration.seconds(60)
CLOUDFRONT_HOSTED_ZONE_ID = "Z2FDTNDATAQYW2"  # the same for every CloudFront distribution
RETRIEVE_TOOL = f"{KB_TARGET_NAME}___Retrieve"

MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

ORIGIN_HEADER_NAME = "X-Origin-Verify"

# Both WAF rules below start in COUNT so real traffic can be watched before anything is
# blocked. Flip this once the common rule set has been checked against real prompts.
WAF_BLOCK = False

# Twice the expected monthly figure (design section 11).
BILLING_ALARM_USD = 50


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

        zone = route53.HostedZone.from_lookup(self, "Zone", domain_name=ZONE_NAME)

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
                                wafv2.CfnWebACL.TextTransformationProperty(
                                    priority=0, type="NONE"
                                )
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
                request_header_allowlist=["Authorization"]
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
            environment_variables={"LOG_LEVEL": "INFO"},
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
                allowed_request_headers=[SESSION_HEADER]
            ),
        )
        target.node.add_dependency(invoke_policy)

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
        security_headers_policy = cloudfront.ResponseHeadersPolicy(
            self,
            "SecurityHeadersPolicy",
            comment="CSP and security headers for the static page",
            security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                content_security_policy=cloudfront.ResponseHeadersContentSecurityPolicy(
                    content_security_policy=(
                        "default-src 'self'; "
                        f"connect-src 'self' https://{AUTH_HOST}; "
                        "img-src 'self' data:; "
                        "style-src 'self'; "
                        "script-src 'self'; "
                        "frame-ancestors 'none'; "
                        "base-uri 'self'; "
                        "form-action 'self'"
                    ),
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
                origin=origins.S3BucketOrigin.with_origin_access_control(site_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                response_headers_policy=security_headers_policy,
            ),
            additional_behaviors={
                "/api/*": cloudfront.BehaviorOptions(
                    origin=gateway_origin,
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                    cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                    # Forwarding Host breaks the gateway's TLS and routing.
                    origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
                    compress=False,
                )
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

        # The tools gateway now exists, so the runtime's environment can point at it.
        runtime.environment_variables = {
            "LOG_LEVEL": "INFO",
            "TOOLS_GATEWAY_URL": tools_gateway.attr_gateway_url,
            "MODEL_ID": MODEL_ID,
            "RETRIEVE_TOOL": RETRIEVE_TOOL,
        }

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
