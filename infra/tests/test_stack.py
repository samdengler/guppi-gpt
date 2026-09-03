import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template
from guppi_gpt_infra.stack import GuppiGptStack

ACCOUNT = "123456789012"
REGION = "us-east-1"
ZONE_CONTEXT_KEY = f"hosted-zone:account={ACCOUNT}:domainName=dengler.io:region={REGION}"


def synth(**extra_context) -> Template:
    app = cdk.App(
        context={
            ZONE_CONTEXT_KEY: {"Id": "/hostedzone/Z0000000000000", "Name": "dengler.io."},
            "image_uri": f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/guppi-gpt:test",
            **extra_context,
        }
    )
    stack = GuppiGptStack(app, "GuppiGpt", env=cdk.Environment(account=ACCOUNT, region=REGION))
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def template() -> Template:
    return synth()


def test_secret_parameter_is_no_echo(template):
    template.has_parameter("GoogleClientSecret", {"NoEcho": True})


def test_apex_placeholder_record(template):
    template.has_resource_properties(
        "AWS::Route53::RecordSet",
        {"Name": "dengler.io.", "Type": "A", "ResourceRecords": ["192.0.2.1"]},
    )


def test_user_pool_domain_waits_for_apex_record(template):
    domains = template.find_resources("AWS::Cognito::UserPoolDomain")
    assert len(domains) == 1
    (domain,) = domains.values()
    assert domain["Properties"]["Domain"] == "auth.dengler.io"
    assert any(dep.startswith("ApexPlaceholder") for dep in domain.get("DependsOn", []))


def test_gateway_has_no_protocol_type_and_uses_cognito_jwt(template):
    gateways = template.find_resources("AWS::BedrockAgentCore::Gateway")
    assert len(gateways) == 1
    (gateway,) = gateways.values()
    assert "ProtocolType" not in gateway["Properties"]
    assert gateway["Properties"]["AuthorizerType"] == "CUSTOM_JWT"


def test_runtime_is_agui_and_not_bound_to_gateway_by_default(template):
    template.has_resource_properties(
        "AWS::BedrockAgentCore::Runtime",
        {
            "ProtocolConfiguration": "AGUI",
            "NetworkConfiguration": {"NetworkMode": "PUBLIC"},
            "AuthorizerConfiguration": {
                "CustomJWTAuthorizer": Match.object_equals(
                    {
                        "DiscoveryUrl": Match.any_value(),
                        "AllowedClients": Match.any_value(),
                    }
                )
            },
        },
    )


def test_runtime_binds_to_gateway_when_asked():
    synth(bind_runtime_to_gateway=True).has_resource_properties(
        "AWS::BedrockAgentCore::Runtime",
        {
            "AuthorizerConfiguration": {
                "CustomJWTAuthorizer": {
                    "AllowedWorkloadConfiguration": {
                        "HostingEnvironments": [
                            {
                                "Arn": {
                                    "Fn::GetAtt": [
                                        Match.string_like_regexp("EdgeGateway.*"),
                                        "GatewayArn",
                                    ]
                                }
                            }
                        ]
                    }
                }
            },
        },
    )


def test_target_is_runtime_with_jwt_passthrough(template):
    template.has_resource_properties(
        "AWS::BedrockAgentCore::GatewayTarget",
        {
            "Name": "api",
            "CredentialProviderConfigurations": [{"CredentialProviderType": "JWT_PASSTHROUGH"}],
            "TargetConfiguration": {"Http": {"AgentcoreRuntime": {"Qualifier": "DEFAULT"}}},
        },
    )


def test_api_behavior_streams_through_cloudfront(template):
    template.has_resource_properties(
        "AWS::CloudFront::Distribution",
        {
            "DistributionConfig": {
                "Aliases": ["chat.dengler.io"],
                "CacheBehaviors": [
                    Match.object_like(
                        {
                            "PathPattern": "/api/*",
                            "Compress": False,
                            "CachePolicyId": "4135ea2d-6df8-44a3-9df3-4b5a84be39ad",
                            "OriginRequestPolicyId": "b689b0a8-53d0-40ab-baf2-68738e2966ac",
                        }
                    )
                ],
                "Origins": Match.array_with(
                    [
                        Match.object_like(
                            {"CustomOriginConfig": Match.object_like({"OriginReadTimeout": 60})}
                        )
                    ]
                ),
            }
        },
    )


def test_no_lambda_functions(template):
    assert template.find_resources("AWS::Lambda::Function") == {}
