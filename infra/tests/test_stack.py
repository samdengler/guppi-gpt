import json

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
    (gateway,) = [g for g in gateways.values() if g["Properties"]["Name"] == "guppi-gpt-edge"]
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


def test_managed_knowledge_base_reads_the_content_bucket(template):
    template.has_resource_properties(
        "AWS::Bedrock::KnowledgeBase",
        {
            "KnowledgeBaseConfiguration": {
                "Type": "MANAGED",
                "ManagedKnowledgeBaseConfiguration": {"EmbeddingModelType": "MANAGED"},
            }
        },
    )
    template.has_resource_properties(
        "AWS::Bedrock::DataSource",
        {
            "DataDeletionPolicy": "DELETE",
            "DataSourceConfiguration": {
                "Type": "MANAGED_KNOWLEDGE_BASE_CONNECTOR",
                "ManagedKnowledgeBaseConnectorConfiguration": {
                    "ConnectorParameters": Match.object_like(
                        {
                            "type": "S3",
                            "filterConfiguration": {"inclusionPrefixes": ["docs/"]},
                        }
                    ),
                    "DeletionProtectionConfiguration": {"DeletionProtectionStatus": "DISABLED"},
                },
            },
        },
    )
    template.has_resource_properties(
        "AWS::S3::Bucket",
        Match.object_like({"VersioningConfiguration": {"Status": "Enabled"}}),
    )


def test_nightly_ingestion_is_a_scheduler_universal_target(template):
    template.has_resource_properties(
        "AWS::Scheduler::Schedule",
        {
            "ScheduleExpression": "cron(0 9 * * ? *)",
            "Target": Match.object_like({"Arn": Match.any_value(), "Input": Match.any_value()}),
        },
    )
    rendered = json.dumps(template.to_json())
    assert ":scheduler:::aws-sdk:bedrockagent:startIngestionJob" in rendered
    assert '{\\"KnowledgeBaseId\\":\\"' in rendered and '\\"DataSourceId\\":\\"' in rendered
    template.has_resource_properties(
        "AWS::IAM::Policy",
        Match.object_like(
            {
                "PolicyDocument": {
                    "Statement": Match.array_with(
                        [Match.object_like({"Action": "bedrock:StartIngestionJob"})]
                    )
                }
            }
        ),
    )


def test_web_acl_has_three_count_rules_associated_with_the_edge_gateway(template):
    acls = template.find_resources("AWS::WAFv2::WebACL")
    (acl,) = acls.values()
    assert acl["Properties"]["Scope"] == "REGIONAL"
    rules = acl["Properties"]["Rules"]
    assert len(rules) == 3
    names = {rule["Name"] for rule in rules}
    assert names == {"CloudFrontOnly", "AWSManagedRulesCommonRuleSet", "RateLimit"}
    for rule in rules:
        if rule["Name"] == "AWSManagedRulesCommonRuleSet":
            assert rule["OverrideAction"] == {"Count": {}}
        else:
            assert rule["Action"] == {"Count": {}}
    template.has_resource_properties(
        "AWS::WAFv2::WebACLAssociation",
        {
            "ResourceArn": {
                "Fn::GetAtt": [Match.string_like_regexp("EdgeGateway.*"), "GatewayArn"]
            },
        },
    )


def test_cloudfront_gateway_origin_carries_the_origin_verify_header(template):
    rendered = json.dumps(template.to_json())
    assert "X-Origin-Verify" in rendered
    origins = template.find_resources("AWS::CloudFront::Distribution")
    (distribution,) = origins.values()
    custom_origins = [
        origin
        for origin in distribution["Properties"]["DistributionConfig"]["Origins"]
        if "CustomOriginConfig" in origin
    ]
    (gateway_origin,) = custom_origins
    (header,) = gateway_origin["OriginCustomHeaders"]
    assert header["HeaderName"] == "X-Origin-Verify"
    assert "resolve:secretsmanager" in json.dumps(header["HeaderValue"])


def test_response_headers_policy_has_the_csp_and_is_on_the_default_behavior(template):
    policies = template.find_resources("AWS::CloudFront::ResponseHeadersPolicy")
    (policy_id, policy) = next(iter(policies.items()))
    csp = policy["Properties"]["ResponseHeadersPolicyConfig"]["SecurityHeadersConfig"][
        "ContentSecurityPolicy"
    ]["ContentSecurityPolicy"]
    assert csp == (
        "default-src 'self'; connect-src 'self' https://auth.dengler.io; "
        "img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    template.has_resource_properties(
        "AWS::CloudFront::Distribution",
        {
            "DistributionConfig": Match.object_like(
                {
                    "DefaultCacheBehavior": Match.object_like(
                        {"ResponseHeadersPolicyId": {"Ref": policy_id}}
                    )
                }
            )
        },
    )


def test_billing_alarm_has_the_cost_limit_and_the_alarm_topic(template):
    alarms = template.find_resources("AWS::CloudWatch::Alarm")
    (alarm,) = [a for a in alarms.values() if a["Properties"]["MetricName"] == "EstimatedCharges"]
    props = alarm["Properties"]
    assert props["Namespace"] == "AWS/Billing"
    assert props["Dimensions"] == [{"Name": "Currency", "Value": "USD"}]
    assert props["Threshold"] == 50
    assert len(props["AlarmActions"]) == 1


def test_alarm_email_subscription_is_conditional(template):
    template.has_parameter("AlarmEmail", {"Default": ""})
    subscriptions = template.find_resources("AWS::SNS::Subscription")
    (subscription,) = subscriptions.values()
    assert subscription["Properties"]["Protocol"] == "email"
    assert "Condition" in subscription
    conditions = template.to_json().get("Conditions", {})
    assert subscription["Condition"] in conditions


def test_runtime_environment_variables_point_at_the_tools_gateway(template):
    template.has_resource_properties(
        "AWS::BedrockAgentCore::Runtime",
        {
            "EnvironmentVariables": {
                "LOG_LEVEL": "INFO",
                "MODEL_ID": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
                "RETRIEVE_TOOL": "docs___Retrieve",
                "TOOLS_GATEWAY_URL": Match.any_value(),
            }
        },
    )


def test_runtime_role_grants_only_the_one_inference_profile(template):
    template.has_resource_properties(
        "AWS::IAM::Policy",
        Match.object_like(
            {
                "PolicyDocument": {
                    "Statement": Match.array_with(
                        [
                            Match.object_like(
                                {
                                    "Action": [
                                        "bedrock:InvokeModel",
                                        "bedrock:InvokeModelWithResponseStream",
                                    ],
                                    "Resource": [
                                        "arn:aws:bedrock:*::foundation-model/*",
                                        Match.string_like_regexp(
                                            r"arn:aws:bedrock:.*:inference-profile/"
                                            r"us\.anthropic\.claude-haiku-4-5-20251001-v1:0"
                                        ),
                                    ],
                                }
                            )
                        ]
                    )
                }
            }
        ),
    )


def test_tools_gateway_is_mcp_with_cognito_jwt_and_kb_connector(template):
    template.has_resource_properties(
        "AWS::BedrockAgentCore::Gateway",
        {"Name": "guppi-gpt-tools", "ProtocolType": "MCP", "AuthorizerType": "CUSTOM_JWT"},
    )
    template.has_resource_properties(
        "AWS::BedrockAgentCore::GatewayTarget",
        {
            "Name": "docs",
            "CredentialProviderConfigurations": [{"CredentialProviderType": "GATEWAY_IAM_ROLE"}],
            "TargetConfiguration": {
                "Mcp": {
                    "Connector": {
                        "Source": {"ConnectorId": "bedrock-knowledge-bases"},
                        "Configurations": Match.array_with(
                            [Match.object_like({"Name": "Retrieve"})]
                        ),
                    }
                }
            },
        },
    )
    template.has_resource_properties(
        "AWS::IAM::Policy",
        Match.object_like(
            {
                "PolicyDocument": {
                    "Statement": Match.array_with(
                        [
                            Match.object_like(
                                {"Action": ["bedrock:GetKnowledgeBase", "bedrock:Retrieve"]}
                            )
                        ]
                    )
                }
            }
        ),
    )


def test_runtime_forwards_the_bearer_to_the_container(template):
    template.has_resource_properties(
        "AWS::BedrockAgentCore::Runtime",
        {"RequestHeaderConfiguration": {"RequestHeaderAllowlist": ["Authorization"]}},
    )


# ---- Conversation log ----------------------------------------------------------------


def conversation_bucket(template) -> dict:
    buckets = template.find_resources("AWS::S3::Bucket")
    (bucket,) = [
        b
        for b in buckets.values()
        if "LifecycleConfiguration" in b["Properties"]
        and b["Properties"]["LifecycleConfiguration"]["Rules"][0]["Id"] == "ExpireThreadRecords"
    ]
    return bucket


def statements(template, logical_id_prefix: str) -> list[dict]:
    policies = template.find_resources("AWS::IAM::Policy")
    found = []
    for logical_id, policy in policies.items():
        if logical_id.startswith(logical_id_prefix):
            found.extend(policy["Properties"]["PolicyDocument"]["Statement"])
    return found


def test_conversation_bucket_is_versioned_kms_encrypted_and_expires_at_730_days(template):
    props = conversation_bucket(template)["Properties"]
    assert props["VersioningConfiguration"] == {"Status": "Enabled"}
    encryption = props["BucketEncryption"]["ServerSideEncryptionConfiguration"][0]
    assert encryption["BucketKeyEnabled"] is True
    assert encryption["ServerSideEncryptionByDefault"]["SSEAlgorithm"] == "aws:kms"
    assert "KMSMasterKeyID" in encryption["ServerSideEncryptionByDefault"]
    assert props["PublicAccessBlockConfiguration"] == {
        "BlockPublicAcls": True,
        "BlockPublicPolicy": True,
        "IgnorePublicAcls": True,
        "RestrictPublicBuckets": True,
    }
    (rule,) = props["LifecycleConfiguration"]["Rules"]
    assert rule["Prefix"] == "threads/"
    assert rule["ExpirationInDays"] == 730
    assert rule["NoncurrentVersionExpiration"] == {"NoncurrentDays": 730}


def test_conversation_bucket_policy_denies_readers_other_than_the_two_roles(template):
    policies = template.find_resources("AWS::S3::BucketPolicy")
    documents = [p["Properties"]["PolicyDocument"]["Statement"] for p in policies.values()]
    (document,) = [d for d in documents if any(s.get("Sid") == "DenyOtherReaders" for s in d)]
    sids = {statement.get("Sid") for statement in document}
    assert {"RuntimeThreadRecords", "InvestigatorReads", "DenyOtherReaders"} <= sids
    (deny,) = [s for s in document if s.get("Sid") == "DenyOtherReaders"]
    assert deny["Effect"] == "Deny"
    assert deny["Action"] == ["s3:GetObject", "s3:GetObjectVersion"]
    assert deny["Principal"] == {"AWS": "*"}
    assert len(deny["Condition"]["StringNotEquals"]["aws:PrincipalArn"]) == 2
    # enforce_ssl adds its own deny to the same document.
    assert any(
        s.get("Condition", {}).get("Bool", {}).get("aws:SecureTransport") == "false"
        for s in document
    )


def test_runtime_role_reads_and_writes_thread_objects_and_cannot_list_them(template):
    document = statements(template, "RuntimeRole")
    (thread_statement,) = [
        s for s in document if s.get("Action") == ["s3:GetObject", "s3:PutObject"]
    ]
    assert "threads/*" in json.dumps(thread_statement["Resource"])
    actions = json.dumps([s.get("Action") for s in document])
    assert "s3:ListBucket" not in actions and "s3:DeleteObject" not in actions
    assert any(s.get("Action") == "secretsmanager:GetSecretValue" for s in document)
    assert any(s.get("Action") == ["kms:Decrypt", "kms:GenerateDataKey"] for s in document)


def test_investigator_role_reads_the_bucket_the_key_and_the_pool_and_nothing_else(template):
    document = statements(template, "ConversationInvestigatorRole")
    actions = sorted(
        action
        for statement in document
        for action in (
            statement["Action"]
            if isinstance(statement["Action"], list)
            else [statement["Action"]]
        )
    )
    assert actions == [
        "cognito-idp:ListUsers",
        "kms:Decrypt",
        "s3:GetObject",
        "s3:GetObjectVersion",
        "s3:ListBucket",
        "s3:ListBucketVersions",
        "secretsmanager:GetSecretValue",
    ]


def test_investigator_trust_is_the_account_root_until_the_parameter_is_set(template):
    template.has_parameter("InvestigatorPrincipalArn", {"Default": ""})
    roles = template.find_resources("AWS::IAM::Role")
    (role,) = [
        r
        for r in roles.values()
        if "resolves a subject" in r["Properties"].get("Description", "")
    ]
    principal = role["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]["Principal"]["AWS"]
    condition_name, when_set, when_blank = principal["Fn::If"]
    assert condition_name == "HasInvestigatorPrincipal"
    assert when_set == {"Ref": "InvestigatorPrincipalArn"}
    assert when_blank == f"arn:aws:iam::{ACCOUNT}:root"
    assert role["Properties"]["MaxSessionDuration"] == 3600


def test_runtime_carries_the_conversation_log_variables_with_the_switch_off(template):
    template.has_resource_properties(
        "AWS::BedrockAgentCore::Runtime",
        {
            "EnvironmentVariables": Match.object_like(
                {
                    "CONVERSATION_LOG_ENABLED": "false",
                    "CONVERSATION_LOG_BUCKET": Match.any_value(),
                    "CONVERSATION_LOG_KEY_SECRET_ARN": Match.any_value(),
                }
            )
        },
    )


def test_conversation_key_secret_has_no_template(template):
    secrets = template.find_resources("AWS::SecretsManager::Secret")
    (secret,) = [
        s for s in secrets.values() if "HMAC key" in s["Properties"].get("Description", "")
    ]
    generator = secret["Properties"]["GenerateSecretString"]
    assert generator["PasswordLength"] == 32
    assert "SecretStringTemplate" not in generator and "GenerateStringKey" not in generator
