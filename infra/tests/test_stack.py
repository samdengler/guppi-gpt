import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template
from guppi_mcp_app_infra.stack import (
    PARAM_JWT_DISCOVERY_URL,
    PARAM_TOOLS_GATEWAY_ID,
    PARAM_TOOLS_GATEWAY_ROLE_ARN,
    PARAM_USER_POOL_CLIENT_ID,
    GuppiMcpAppStack,
)

ACCOUNT = "123456789012"
REGION = "us-east-1"


def synth(**extra_context) -> Template:
    app = cdk.App(
        context={
            "image_uri": f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/mcp-app:test",
            **extra_context,
        }
    )
    stack = GuppiMcpAppStack(
        app, "GuppiMcpApp", env=cdk.Environment(account=ACCOUNT, region=REGION)
    )
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def template() -> Template:
    return synth()


def ssm_parameter(template: Template, name: str) -> str:
    """The logical id of the CloudFormation parameter that resolves SSM parameter `name`."""
    found = [
        logical_id
        for logical_id, parameter in template.to_json()["Parameters"].items()
        if parameter.get("Type") == "AWS::SSM::Parameter::Value<String>"
        and parameter.get("Default") == name
    ]
    assert len(found) == 1, f"one parameter reads {name}"
    return found[0]


def only(template: Template, resource_type: str) -> dict:
    resources = template.find_resources(resource_type)
    assert len(resources) == 1
    return next(iter(resources.values()))["Properties"]


def test_target_is_on_the_platform_tools_gateway(template):
    target = only(template, "AWS::BedrockAgentCore::GatewayTarget")
    assert target["GatewayIdentifier"] == {"Ref": ssm_parameter(template, PARAM_TOOLS_GATEWAY_ID)}


def test_target_is_named_after_the_project_and_signs_with_the_gateway_role(template):
    target = only(template, "AWS::BedrockAgentCore::GatewayTarget")
    assert target["Name"] == "mcp-app"
    assert target["CredentialProviderConfigurations"] == [
        {
            "CredentialProviderType": "GATEWAY_IAM_ROLE",
            "CredentialProvider": {
                "IamCredentialProvider": {"Service": "bedrock-agentcore", "Region": REGION}
            },
        }
    ]
    mcp_server = target["TargetConfiguration"]["Mcp"]["McpServer"]
    assert mcp_server["ListingMode"] == "DEFAULT"


def test_target_endpoint_is_the_runtime_mcp_invocation_url(template):
    target = only(template, "AWS::BedrockAgentCore::GatewayTarget")
    endpoint = target["TargetConfiguration"]["Mcp"]["McpServer"]["Endpoint"]
    parts = endpoint["Fn::Join"][1]
    flat = "".join(p if isinstance(p, str) else "{}" for p in parts)
    assert flat.startswith("https://bedrock-agentcore.us-east-1.")
    assert "/runtimes/arn%3A{}%3Abedrock-agentcore%3Aus-east-1%3A123456789012%3Aruntime%2F{}" in (
        flat
    )
    assert flat.endswith("/invocations?qualifier=DEFAULT")
    (runtime_logical_id,) = template.find_resources("AWS::BedrockAgentCore::Runtime")
    assert {"Fn::GetAtt": [runtime_logical_id, "AgentRuntimeId"]} in parts


def test_runtime_speaks_mcp_and_takes_sigv4(template):
    runtime = only(template, "AWS::BedrockAgentCore::Runtime")
    assert runtime["ProtocolConfiguration"] == "MCP"
    assert "RequestHeaderConfiguration" not in runtime
    assert "AuthorizerConfiguration" not in runtime


def test_jwt_passthrough_variant_puts_the_platform_jwt_on_the_runtime():
    template = synth(target_credentials="JWT_PASSTHROUGH")
    runtime = only(template, "AWS::BedrockAgentCore::Runtime")
    assert runtime["ProtocolConfiguration"] == "MCP"
    assert runtime["RequestHeaderConfiguration"] == {"RequestHeaderAllowlist": ["Authorization"]}
    jwt = runtime["AuthorizerConfiguration"]["CustomJWTAuthorizer"]
    assert jwt["DiscoveryUrl"] == {"Ref": ssm_parameter(template, PARAM_JWT_DISCOVERY_URL)}
    assert jwt["AllowedClients"] == [{"Ref": ssm_parameter(template, PARAM_USER_POOL_CLIENT_ID)}]
    assert "AllowedWorkloadConfiguration" not in jwt
    target = only(template, "AWS::BedrockAgentCore::GatewayTarget")
    assert target["CredentialProviderConfigurations"] == [
        {"CredentialProviderType": "JWT_PASSTHROUGH"}
    ]


def test_tools_gateway_role_may_invoke_the_runtime(template):
    role_param = ssm_parameter(template, PARAM_TOOLS_GATEWAY_ROLE_ARN)
    policies = template.find_resources(
        "AWS::IAM::Policy",
        {
            "Properties": {
                "PolicyDocument": {
                    "Statement": Match.array_with(
                        [Match.object_like({"Action": "bedrock-agentcore:InvokeAgentRuntime"})]
                    )
                }
            }
        },
    )
    assert len(policies) == 1
    policy = next(iter(policies.values()))["Properties"]
    (role,) = policy["Roles"]
    assert role_param in str(role), "the policy attaches to the role named by the SSM ARN"


def test_outputs(template):
    outputs = template.to_json()["Outputs"]
    assert "RuntimeArn" in outputs
    assert outputs["TargetName"]["Value"] == "mcp-app"


def test_runtime_waits_for_its_role_policy(template):
    (runtime,) = template.find_resources("AWS::BedrockAgentCore::Runtime").values()
    (policy_id,) = template.find_resources(
        "AWS::IAM::Policy",
        {"Properties": {"Roles": [{"Ref": Match.string_like_regexp("RuntimeRole")}]}},
    )
    assert policy_id in runtime["DependsOn"]
