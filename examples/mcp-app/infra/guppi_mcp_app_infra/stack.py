"""The GuppiMcpApp stack.

It reads the platform's `/guppi/platform/*` parameters (guppi-gpt's
`docs/proposals/platform.md`) and adds three things: the MCP server as an AgentCore
Runtime with protocol MCP, a target on the platform's tools gateway that addresses the
Runtime's MCP endpoint, and a grant that lets the tools gateway's role invoke the Runtime.
The gateway checks the user's JWT and signs its call to the Runtime with that role.
Nothing in the platform stack is edited; the grant is a policy owned by this stack.
"""

from pathlib import Path

import aws_cdk as cdk
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_ssm as ssm
from constructs import Construct

PLATFORM_PARAMETER_PREFIX = "/guppi/platform"
PARAM_TOOLS_GATEWAY_ID = f"{PLATFORM_PARAMETER_PREFIX}/tools-gateway-id"
PARAM_TOOLS_GATEWAY_ROLE_ARN = f"{PLATFORM_PARAMETER_PREFIX}/tools-gateway-role-arn"
PARAM_USER_POOL_CLIENT_ID = f"{PLATFORM_PARAMETER_PREFIX}/user-pool-client-id"
PARAM_JWT_DISCOVERY_URL = f"{PLATFORM_PARAMETER_PREFIX}/jwt-discovery-url"

RUNTIME_NAME = "guppi_mcp_app"  # runtime names are letters, digits and underscores
# The tools gateway names a target's tools <target>___<tool>. The target is named after
# the project so the platform agent's filter for a mcp-app page matches mcp-app___*
# (docs/decision-log.md, step 4).
TARGET_NAME = "mcp-app"
RUNTIME_QUALIFIER = "DEFAULT"


class GuppiMcpAppStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        tools_gateway_id = ssm.StringParameter.value_for_string_parameter(
            self, PARAM_TOOLS_GATEWAY_ID
        )
        tools_gateway_role_arn = ssm.StringParameter.value_for_string_parameter(
            self, PARAM_TOOLS_GATEWAY_ROLE_ARN
        )
        # The tools gateway refuses JWT_PASSTHROUGH on an MCP server target ("MCP server
        # target does not support JWT_PASSTHROUGH credential provider type", first deploy,
        # 29 Sep 2026), so by default the gateway signs each request with its own role and
        # the Runtime takes SigV4 instead of a JWT. `-c target_credentials=JWT_PASSTHROUGH`
        # restores the passthrough design (JWT authorizer on the Runtime) for the day the
        # service accepts it (docs/decision-log.md, step 6).
        target_credentials = self.node.try_get_context("target_credentials") or "GATEWAY_IAM_ROLE"
        jwt_authorizer = None
        if target_credentials == "JWT_PASSTHROUGH":
            jwt_authorizer = agentcore.CfnRuntime.AuthorizerConfigurationProperty(
                custom_jwt_authorizer=agentcore.CfnRuntime.CustomJWTAuthorizerConfigurationProperty(
                    discovery_url=ssm.StringParameter.value_for_string_parameter(
                        self, PARAM_JWT_DISCOVERY_URL
                    ),
                    allowed_clients=[
                        ssm.StringParameter.value_for_string_parameter(
                            self, PARAM_USER_POOL_CLIENT_ID
                        )
                    ],
                )
            )

        # ---- Server image ----------------------------------------------------------------
        runtime_role = self._runtime_role()
        image_uri = self.node.try_get_context("image_uri")
        if image_uri is None:
            # The context is the repository root so server/Dockerfile can read uv.lock;
            # .dockerignore at the root keeps the context and the asset hash to the server
            # files, the lockfile, and the workspace pyprojects.
            repo_root = Path(__file__).resolve().parents[2]
            asset = ecr_assets.DockerImageAsset(
                self,
                "ServerImage",
                directory=str(repo_root),
                file="server/Dockerfile",
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

        # ---- Runtime ---------------------------------------------------------------------
        # With no authorizer configuration the Runtime accepts only SigV4 from principals
        # allowed InvokeAgentRuntime, which is the tools gateway role below. With the JWT
        # authorizer, the platform's tools gateway ARN is not bound in
        # allowed_workload_configuration: guppi-gpt's decision log records that the binding
        # does not work with token passthrough (docs/decision-log.md, step 4).
        runtime = agentcore.CfnRuntime(
            self,
            "Runtime",
            agent_runtime_name=RUNTIME_NAME,
            description="guppi-mcp-app MCP server (show_card)",
            role_arn=runtime_role.role_arn,
            agent_runtime_artifact=agentcore.CfnRuntime.AgentRuntimeArtifactProperty(
                container_configuration=agentcore.CfnRuntime.ContainerConfigurationProperty(
                    container_uri=image_uri
                )
            ),
            network_configuration=agentcore.CfnRuntime.NetworkConfigurationProperty(
                network_mode="PUBLIC"
            ),
            protocol_configuration="MCP",
            # The Runtime accepts an Authorization allowlist only with a JWT authorizer
            # ("Authorization header can be specified in requestHeaderAllowlist only when
            # runtime is set up with customJWTAuthorizer", 29 Sep 2026).
            request_header_configuration=(
                agentcore.CfnRuntime.RequestHeaderConfigurationProperty(
                    request_header_allowlist=["Authorization"]
                )
                if jwt_authorizer
                else None
            ),
            authorizer_configuration=jwt_authorizer,
            environment_variables={"LOG_LEVEL": "INFO"},
        )
        # The Runtime checks at creation that its role can pull the image; without this
        # the Runtime depends only on the role and races its default policy (observed on
        # the first deploy, 29 Sep 2026: "Access denied while validating ECR URI").
        runtime.node.add_dependency(runtime_role)

        # ---- Invoke grant for the tools gateway ------------------------------------------
        tools_gateway_role = iam.Role.from_role_arn(
            self, "ToolsGatewayRole", tools_gateway_role_arn, mutable=True
        )
        invoke_policy = iam.Policy(
            self,
            "ToolsGatewayInvokePolicy",
            roles=[tools_gateway_role],
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

        # ---- Target on the tools gateway -------------------------------------------------
        # McpTargetConfiguration has no Runtime ARN property (only an agent runtime HTTP
        # target does); mcp_server takes an HTTPS endpoint, and a Runtime's MCP endpoint is
        # its invocation URL with the ARN URL-encoded. DEFAULT listing syncs the tools at
        # the control plane, signed with the gateway role, and lists them with the other
        # cached targets; a DYNAMIC target's tools arrive on a later tools/list page, which
        # the platform agent does not read (docs/decision-log.md, step 7).
        target = agentcore.CfnGatewayTarget(
            self,
            "McpTarget",
            gateway_identifier=tools_gateway_id,
            name=TARGET_NAME,
            description="guppi-mcp-app MCP server on AgentCore Runtime",
            target_configuration=agentcore.CfnGatewayTarget.TargetConfigurationProperty(
                mcp=agentcore.CfnGatewayTarget.McpTargetConfigurationProperty(
                    mcp_server=agentcore.CfnGatewayTarget.McpServerTargetConfigurationProperty(
                        endpoint=self._runtime_mcp_endpoint(runtime),
                        listing_mode="DEFAULT",
                    )
                )
            ),
            credential_provider_configurations=[
                agentcore.CfnGatewayTarget.CredentialProviderConfigurationProperty(
                    credential_provider_type=target_credentials,
                    credential_provider=(
                        agentcore.CfnGatewayTarget.CredentialProviderProperty(
                            iam_credential_provider=agentcore.CfnGatewayTarget.IamCredentialProviderProperty(
                                service="bedrock-agentcore", region=self.region
                            )
                        )
                        if target_credentials == "GATEWAY_IAM_ROLE"
                        else None
                    ),
                )
            ],
        )
        target.node.add_dependency(invoke_policy)

        cdk.CfnOutput(self, "RuntimeArn", value=runtime.attr_agent_runtime_arn)
        cdk.CfnOutput(self, "TargetName", value=TARGET_NAME)

    def _runtime_mcp_endpoint(self, runtime: agentcore.CfnRuntime) -> str:
        """https://bedrock-agentcore.<region>.amazonaws.com/runtimes/<encoded ARN>/invocations,
        the Runtime's MCP endpoint. The ARN is rebuilt from the runtime id so its `:` and
        `/` can be written URL-encoded."""
        encoded_arn = (
            f"arn%3A{self.partition}%3Abedrock-agentcore%3A{self.region}%3A{self.account}"
            f"%3Aruntime%2F{runtime.attr_agent_runtime_id}"
        )
        return (
            f"https://bedrock-agentcore.{self.region}.{self.url_suffix}/runtimes/"
            f"{encoded_arn}/invocations?qualifier={RUNTIME_QUALIFIER}"
        )

    def _runtime_role(self) -> iam.Role:
        """Execution role for the runtime, following the AgentCore documented policy."""
        region, account = self.region, self.account
        role = iam.Role(
            self,
            "RuntimeRole",
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": account},
                    "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{region}:{account}:*"},
                },
            ),
            description="Execution role for the guppi-mcp-app MCP server runtime",
        )
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
        return role
