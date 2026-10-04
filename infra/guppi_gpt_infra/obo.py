"""The on-behalf-of token issuer (guppi-hr D20, D47) and the AgentCore Identity credential
providers that call it.

Okta signs people in (D46) but its free plan cannot exchange tokens, so a small RFC 8693
issuer stands in for the production identity provider: an HTTP API in front of one Lambda
function (lambdas/obo_issuer), approved by Sam on 3 October 2026 as a Lambda in the
request path. It runs about five times per chat, never per turn on the callers' side; the
HR tools gateway exchanges once per tool call (guppi-hr aws-feedback A14).

Each client is an OAuth client of the issuer with a secret in Secrets Manager, generated
here and never seen by anyone: the issuer reads it at cold start, and an AgentCore Identity
credential provider in on-behalf-of mode references it (`EXTERNAL`), so a caller asks
Identity for a token and never handles the secret. RULES says which subject token each
client may exchange and which scopes it may receive; the issuer enforces them.

Published under /guppi/obo/*: the issuer and discovery URLs, and per client the provider's
name and ARN and the secret's ARN, which the caller's role must be allowed to read. Project stacks read them at deploy time, like /guppi/platform/*.
"""

from __future__ import annotations

import json
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import Duration, RemovalPolicy
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cloudwatch_actions
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_sns as sns
from aws_cdk import aws_ssm as ssm
from constructs import Construct

ISSUER_DIR = Path(__file__).resolve().parent / "lambdas" / "obo_issuer"
PARAMS = "/guppi/obo"
API_NAME = "guppi-obo-issuer"
FUNCTION_NAME = "guppi-gpt-obo-issuer"
PROVIDER_PREFIX = "guppi-obo-"
# About five exchanges per chat start, plus one per HR tool call; a burst covers a chat's
# warm start and a few turns at once.
RATE_PER_SECOND = 20
BURST = 40
RESERVED_CONCURRENCY = 10

AGENTS = "api://hr-agents"
TOOLS = "api://hr-tools"
TOOLS_RUNTIME = "api://hr-tools-runtime"
POLICY = "hr.tools.policy"
DOMAIN_SCOPES = {
    "profile": [POLICY, "hr.tools.profile.read", "hr.tools.profile.write"],
    "pay": [POLICY, "hr.tools.pay.read", "hr.tools.pay.write"],
    "travel": [POLICY],
}
CANVAS_SCOPES = [POLICY, "hr.tools.profile.read", "hr.tools.pay.read"]
AGENT_CLIENTS = [f"hr-agent-{domain}" for domain in DOMAIN_SCOPES]

# Which subject token each client may present, and what it may receive (guppi-hr
# docs/proposals/obo-token-exchange.md, "Clients and what each may exchange").
RULES: dict[str, dict] = {
    # The Connect bridge and the /p/hr-diy/ orchestrator: the employee's Okta token, for
    # the agents token or the canvas's tools token.
    "hr-bridge": {
        "subject": {"issuer": "okta", "act_depths": [0]},
        "grants": [{"audience": AGENTS, "scopes": ["hr.agents"]}, {"audience": TOOLS, "scopes": CANVAS_SCOPES}],
    },
    # Each sub-agent: the bridge's agents token, for its own domain's tools token.
    **{
        f"hr-agent-{domain}": {
            "subject": {"issuer": "self", "audiences": [AGENTS], "clients": ["hr-bridge"], "act_depths": [1]},
            "grants": [{"audience": TOOLS, "scopes": scopes}],
        }
        for domain, scopes in DOMAIN_SCOPES.items()
    },
    # The tools gateway's target: a caller's tools token, for a token only the tools runtime
    # accepts, carrying the caller's scopes (its target requests fixed scopes).
    "hr-tools-gateway": {
        "subject": {"issuer": "self", "audiences": [TOOLS], "clients": ["hr-bridge", *AGENT_CLIENTS],
                    "act_depths": [1, 2]},
        "grants": [{"audience": TOOLS_RUNTIME,
                    "scopes": sorted({s for scopes in DOMAIN_SCOPES.values() for s in scopes} | set(CANVAS_SCOPES))}],
        "inherit_scopes": True,
    },
}
CLIENTS = list(RULES)
RULES_FOR_TESTS = RULES


class OboIssuer(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        okta_issuer: str,
        okta_audience: str,
        okta_client_ids: list[str],
        alarm_topic: sns.ITopic,
    ) -> None:
        super().__init__(scope, construct_id)
        stack = cdk.Stack.of(self)
        account, region = stack.account, stack.region

        role = iam.Role(self, "IssuerRole", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
                        description="The on-behalf-of token issuer (guppi-hr D47)")
        role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole"))

        # Only the issuer may sign: the key policy gives the account administration but not
        # kms:Sign, so no IAM policy elsewhere in the account can mint a token.
        admin_actions = ["kms:Create*", "kms:Describe*", "kms:Enable*", "kms:List*", "kms:Put*", "kms:Update*",
                         "kms:Revoke*", "kms:Disable*", "kms:Get*", "kms:Delete*", "kms:TagResource",
                         "kms:UntagResource", "kms:ScheduleKeyDeletion", "kms:CancelKeyDeletion"]
        key_policy = iam.PolicyDocument(statements=[
            iam.PolicyStatement(sid="AccountAdministration", principals=[iam.AccountRootPrincipal()],
                                actions=admin_actions, resources=["*"]),
            iam.PolicyStatement(sid="IssuerSigns", principals=[role], actions=["kms:Sign", "kms:GetPublicKey"],
                                resources=["*"]),
        ])
        self.key = kms.Key(self, "SigningKey", key_spec=kms.KeySpec.RSA_2048, key_usage=kms.KeyUsage.SIGN_VERIFY,
                           policy=key_policy, removal_policy=RemovalPolicy.RETAIN,
                           description="Signs on-behalf-of tokens (guppi-hr D47)")

        # One secret per client, a generated string the issuer and AgentCore Identity read.
        self.secrets: dict[str, secretsmanager.Secret] = {}
        for client in CLIENTS:
            secret = secretsmanager.Secret(
                self, f"Client{_pascal(client)}Secret",
                description=f"Client secret of {client} at the on-behalf-of issuer (guppi-hr D47)",
                generate_secret_string=secretsmanager.SecretStringGenerator(
                    secret_string_template=json.dumps({"client_id": client}),
                    generate_string_key="client_secret", exclude_punctuation=True, password_length=48),
                removal_policy=RemovalPolicy.DESTROY,
            )
            secret.add_to_resource_policy(iam.PolicyStatement(
                principals=[iam.ServicePrincipal("bedrock-agentcore.amazonaws.com")],
                actions=["secretsmanager:GetSecretValue"], resources=["*"],
                conditions={"StringEquals": {"aws:SourceAccount": account}},
            ))
            secret.grant_read(role)
            self.secrets[client] = secret

        issuer_parameter_name = f"{PARAMS}/issuer"
        function = lambda_.Function(
            self, "Issuer",
            runtime=lambda_.Runtime.PYTHON_3_12, architecture=lambda_.Architecture.ARM_64,
            handler="index.handler", code=lambda_.Code.from_asset(str(ISSUER_DIR)),
            role=role, memory_size=1024, timeout=Duration.seconds(10),
            reserved_concurrent_executions=RESERVED_CONCURRENCY,
            function_name=FUNCTION_NAME,
            log_group=logs.LogGroup(self, "IssuerLogs", log_group_name=f"/aws/lambda/{FUNCTION_NAME}",
                                    retention=logs.RetentionDays.ONE_MONTH, removal_policy=RemovalPolicy.DESTROY),
            description="On-behalf-of token issuer, RFC 8693 (guppi-hr D47)",
            environment={
                "KEY_ID": self.key.key_id,
                "ISSUER_PARAMETER": issuer_parameter_name,
                "OKTA_ISSUER": okta_issuer,
                "OKTA_AUDIENCE": okta_audience,
                "OKTA_CLIENTS": cdk.Fn.join(",", okta_client_ids),
                "RULES": json.dumps(RULES, separators=(",", ":")),
                "CLIENT_SECRET_ARNS": stack.to_json_string({c: s.secret_arn for c, s in self.secrets.items()}),
            },
        )
        role.add_to_policy(iam.PolicyStatement(
            actions=["ssm:GetParameter"],
            resources=[f"arn:aws:ssm:{region}:{account}:parameter{issuer_parameter_name}"]))

        self.api = apigw.HttpApi(self, "Api", api_name=API_NAME, description="On-behalf-of token issuer (guppi-hr D47)")
        integration = integrations.HttpLambdaIntegration("IssuerIntegration", function)
        for path, method in (("/.well-known/openid-configuration", apigw.HttpMethod.GET),
                             ("/jwks.json", apigw.HttpMethod.GET), ("/token", apigw.HttpMethod.POST)):
            self.api.add_routes(path=path, methods=[method], integration=integration)
        stage = self.api.default_stage.node.default_child
        stage.default_route_settings = apigw.CfnStage.RouteSettingsProperty(
            throttling_burst_limit=BURST, throttling_rate_limit=RATE_PER_SECOND)
        self.issuer_url = self.api.api_endpoint
        self.discovery_url = f"{self.issuer_url}/.well-known/openid-configuration"

        issuer_parameter = ssm.StringParameter(self, "IssuerParameter", parameter_name=issuer_parameter_name,
                                               string_value=self.issuer_url,
                                               description="On-behalf-of issuer URL (guppi-hr D47)")
        discovery_parameter = ssm.StringParameter(self, "DiscoveryParameter", parameter_name=f"{PARAMS}/discovery-url",
                                                  string_value=self.discovery_url,
                                                  description="On-behalf-of issuer discovery URL (guppi-hr D47)")

        # Identity reads the discovery document when a provider is created, and the issuer
        # reads its own URL from SSM at cold start, so the providers wait for both.
        self.providers: dict[str, agentcore.CfnOAuth2CredentialProvider] = {}
        for client in CLIENTS:
            provider = agentcore.CfnOAuth2CredentialProvider(
                self, f"Provider{_pascal(client)}",
                name=f"{PROVIDER_PREFIX}{client}",
                credential_provider_vendor="CustomOauth2",
                oauth2_provider_config_input=agentcore.CfnOAuth2CredentialProvider.Oauth2ProviderConfigInputProperty(
                    custom_oauth2_provider_config=agentcore.CfnOAuth2CredentialProvider.CustomOauth2ProviderConfigInputProperty(
                        oauth_discovery=agentcore.CfnOAuth2CredentialProvider.Oauth2DiscoveryProperty(
                            discovery_url=self.discovery_url),
                        client_id=client,
                        client_secret_source="EXTERNAL",
                        client_secret_config=agentcore.CfnOAuth2CredentialProvider.SecretReferenceProperty(
                            secret_id=self.secrets[client].secret_arn, json_key="client_secret"),
                        client_authentication_method="CLIENT_SECRET_BASIC",
                        on_behalf_of_token_exchange_config=agentcore.CfnOAuth2CredentialProvider.OnBehalfOfTokenExchangeConfigProperty(
                            grant_type="TOKEN_EXCHANGE",
                            token_exchange_grant_type_config=agentcore.CfnOAuth2CredentialProvider.TokenExchangeGrantTypeConfigProperty(
                                actor_token_content="NONE")),
                    )),
            )
            provider.node.add_dependency(self.api, issuer_parameter, function)
            self.providers[client] = provider
            # AgentCore Identity reads an EXTERNAL secret as the caller of GetResourceOauth2Token
            # (guppi-hr aws-feedback A16), so each caller's role is granted its client's secret.
            for field, value in (("provider-name", provider.name), ("provider-arn", provider.attr_credential_provider_arn),
                                 ("secret-arn", self.secrets[client].secret_arn)):
                ssm.StringParameter(self, f"{_pascal(client)}{_pascal(field)}Parameter",
                                    parameter_name=f"{PARAMS}/{client}/{field}", string_value=value,
                                    description=f"On-behalf-of credential provider for {client} (guppi-hr D47)")

        # Alarms: the function failing, being throttled, or refusing much more than usual.
        for name, metric, threshold in (
            ("Errors", function.metric_errors(period=Duration.minutes(5)), 1),
            ("Throttles", function.metric_throttles(period=Duration.minutes(5)), 1),
            ("Api5xx", self.api.metric_server_error(period=Duration.minutes(5)), 1),
            ("Api4xx", self.api.metric_client_error(period=Duration.minutes(5)), 50),
        ):
            alarm = cloudwatch.Alarm(self, f"Issuer{name}Alarm", metric=metric, threshold=threshold,
                                     evaluation_periods=1,
                                     comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                                     treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
                                     alarm_description=f"On-behalf-of issuer {name} (guppi-hr D47)")
            alarm.add_alarm_action(cloudwatch_actions.SnsAction(alarm_topic))

        cdk.CfnOutput(stack, "OboIssuerUrl", value=self.issuer_url)
        self.discovery_parameter = discovery_parameter


def _pascal(text: str) -> str:
    return "".join(part.capitalize() for part in text.replace("/", "-").split("-"))
