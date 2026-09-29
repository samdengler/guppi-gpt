import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template
from guppi_gpt_infra.stack import FUNCTIONS_DIR, GuppiGptStack

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


def test_web_client_rotates_refresh_tokens(template):
    template.has_resource_properties(
        "AWS::Cognito::UserPoolClient",
        {
            "ClientName": "guppi-gpt-web",
            "RefreshTokenRotation": {"Feature": "ENABLED", "RetryGracePeriodSeconds": 30},
            # Unchanged: 30 days, expressed in minutes by CloudFormation.
            "RefreshTokenValidity": 43200,
        },
    )


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
                "CacheBehaviors": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "PathPattern": "/api/*",
                                "Compress": False,
                                "CachePolicyId": "4135ea2d-6df8-44a3-9df3-4b5a84be39ad",
                                "OriginRequestPolicyId": "b689b0a8-53d0-40ab-baf2-68738e2966ac",
                            }
                        )
                    ]
                ),
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


def test_web_acl_has_three_blocking_rules_associated_with_the_edge_gateway(template):
    acls = template.find_resources("AWS::WAFv2::WebACL")
    (acl,) = acls.values()
    assert acl["Properties"]["Scope"] == "REGIONAL"
    rules = acl["Properties"]["Rules"]
    assert len(rules) == 3
    names = {rule["Name"] for rule in rules}
    assert names == {"CloudFrontOnly", "AWSManagedRulesCommonRuleSet", "RateLimit"}
    for rule in rules:
        if rule["Name"] == "AWSManagedRulesCommonRuleSet":
            assert rule["OverrideAction"] == {"None": {}}
        else:
            assert rule["Action"] == {"Block": {}}
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
    # Two custom origins now: the edge gateway and the feedback API. Only the gateway
    # origin carries the header, since the feedback API checks the caller's token itself.
    custom_origins = [
        origin
        for origin in distribution["Properties"]["DistributionConfig"]["Origins"]
        if "OriginCustomHeaders" in origin
    ]
    (gateway_origin,) = custom_origins
    (header,) = gateway_origin["OriginCustomHeaders"]
    assert header["HeaderName"] == "X-Origin-Verify"
    assert "resolve:secretsmanager" in json.dumps(header["HeaderValue"])


def headers_policy(template, comment_prefix: str) -> tuple[str, dict]:
    """The response headers policy whose comment starts with `comment_prefix`."""
    policies = template.find_resources("AWS::CloudFront::ResponseHeadersPolicy")
    ((policy_id, policy),) = [
        (policy_id, policy)
        for policy_id, policy in policies.items()
        if policy["Properties"]["ResponseHeadersPolicyConfig"]["Comment"].startswith(comment_prefix)
    ]
    return policy_id, policy


def test_response_headers_policy_has_the_csp_and_is_on_the_default_behavior(template):
    (policy_id, policy) = headers_policy(template, "CSP and security headers for the static page")
    csp = policy["Properties"]["ResponseHeadersPolicyConfig"]["SecurityHeadersConfig"][
        "ContentSecurityPolicy"
    ]["ContentSecurityPolicy"]
    # DynatraceBeaconOrigin defaults blank, so the CSP the stack renders without that
    # parameter set is the false branch of the Fn::If (test_csp_adds_the_beacon_origin_
    # only_when_the_parameter_is_set below checks both branches).
    assert csp["Fn::If"][2] == (
        "default-src 'self'; connect-src 'self' https://auth.dengler.io; "
        "img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "frame-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
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
    (alarm,) = [
        a for a in alarms.values() if a["Properties"].get("MetricName") == "EstimatedCharges"
    ]
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
            "EnvironmentVariables": Match.object_like(
                {
                    "LOG_LEVEL": "INFO",
                    "MODEL_ID": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
                    "OTEL_PYTHON_EXCLUDED_URLS": "/ping$",
                    "RETRIEVE_TOOL": "docs___Retrieve",
                    "TOOLS_GATEWAY_URL": Match.any_value(),
                }
            )
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


def test_runtime_forwards_the_bearer_and_the_trace_context_to_the_container(template):
    template.has_resource_properties(
        "AWS::BedrockAgentCore::Runtime",
        {
            "RequestHeaderConfiguration": {
                "RequestHeaderAllowlist": ["Authorization", "traceparent"]
            }
        },
    )


def test_edge_target_forwards_the_session_id_and_the_trace_context(template):
    template.has_resource_properties(
        "AWS::BedrockAgentCore::GatewayTarget",
        {
            "Name": "api",
            "MetadataConfiguration": {
                "AllowedRequestHeaders": [
                    "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id",
                    "traceparent",
                ]
            },
        },
    )


def test_runtime_role_can_let_xray_write_spans_to_its_own_log_group(template):
    template.has_resource_properties(
        "AWS::IAM::Policy",
        Match.object_like(
            {
                "PolicyDocument": {
                    "Statement": Match.array_with(
                        [
                            Match.object_like(
                                {
                                    "Action": "logs:PutResourcePolicy",
                                    "Resource": Match.string_like_regexp(
                                        r"arn:aws:logs:.*:log-group:"
                                        r"/aws/bedrock-agentcore/runtimes/guppi_gpt-\*"
                                    ),
                                }
                            )
                        ]
                    )
                }
            }
        ),
    )


def test_rum_script_path_and_beacon_origin_outputs(template):
    outputs = template.to_json()["Outputs"]
    assert outputs["RumScriptPath"]["Value"] == "/dt/ruxitagentjs.js"
    assert outputs["RumBeaconOrigin"]["Value"] == {"Ref": "DynatraceBeaconOrigin"}
    template.has_parameter("DynatraceBeaconOrigin", {"Default": ""})


def test_csp_adds_the_beacon_origin_only_when_the_parameter_is_set(template):
    _, policy = headers_policy(template, "CSP and security headers for the static page")
    csp = policy["Properties"]["ResponseHeadersPolicyConfig"]["SecurityHeadersConfig"][
        "ContentSecurityPolicy"
    ]["ContentSecurityPolicy"]
    if_branches = csp["Fn::If"]
    assert if_branches[0] == "HasDynatraceBeaconOrigin"
    # Rendering with the parameter unset (the default): the same CSP the stack had
    # before Dynatrace, with no beacon origin appended.
    without_beacon = if_branches[2]
    assert without_beacon == (
        "default-src 'self'; connect-src 'self' https://auth.dengler.io; "
        "img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "frame-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    # Rendering with the parameter set: the beacon origin joined into connect-src.
    with_beacon = if_branches[1]
    joined = with_beacon["Fn::Join"][1]
    rendered = "".join(
        part if isinstance(part, str) else "<DynatraceBeaconOrigin>" for part in joined
    )
    assert rendered == (
        "default-src 'self'; connect-src 'self' https://auth.dengler.io "
        "<DynatraceBeaconOrigin>; img-src 'self' data:; style-src 'self'; "
        "script-src 'self'; frame-src 'self'; frame-ancestors 'none'; base-uri 'self'; "
        "form-action 'self'"
    )
    conditions = template.to_json().get("Conditions", {})
    assert "HasDynatraceBeaconOrigin" in conditions


def test_dynatrace_otlp_parameters_and_condition(template):
    template.has_parameter("DynatraceOtlpEndpoint", {"Default": ""})
    template.has_parameter("DynatraceApiToken", {"NoEcho": True, "Default": ""})
    conditions = template.to_json().get("Conditions", {})
    assert "HasDynatraceOtlp" in conditions
    # Both parameters must be non-empty; an Fn::And of two Fn::Not/Fn::Equals checks.
    expression = conditions["HasDynatraceOtlp"]
    assert "Fn::And" in expression
    assert len(expression["Fn::And"]) == 2


def test_runtime_env_omits_dynatrace_otlp_vars_until_both_parameters_are_set(template):
    template.has_resource_properties(
        "AWS::BedrockAgentCore::Runtime",
        {
            "EnvironmentVariables": Match.object_like(
                {
                    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": {
                        "Fn::If": [
                            "HasDynatraceOtlp",
                            Match.object_like(
                                {
                                    "Fn::Join": [
                                        "",
                                        [{"Ref": "DynatraceOtlpEndpoint"}, "/v1/traces"],
                                    ]
                                }
                            ),
                            {"Ref": "AWS::NoValue"},
                        ]
                    },
                    "OTEL_EXPORTER_OTLP_TRACES_HEADERS": {
                        "Fn::If": [
                            "HasDynatraceOtlp",
                            Match.object_like(
                                {
                                    "Fn::Join": [
                                        "",
                                        [
                                            "Authorization=Api-Token ",
                                            {"Ref": "DynatraceApiToken"},
                                        ],
                                    ]
                                }
                            ),
                            {"Ref": "AWS::NoValue"},
                        ]
                    },
                }
            )
        },
    )


def test_transaction_search_is_enabled_with_the_span_log_policy(template):
    template.has_resource_properties(
        "AWS::XRay::TransactionSearchConfig", {"IndexingPercentage": 1}
    )
    policies = [
        p["Properties"]["PolicyDocument"]
        for p in template.find_resources("AWS::Logs::ResourcePolicy").values()
        if "xray.amazonaws.com" in p["Properties"]["PolicyDocument"]
    ]
    (document,) = policies
    assert "log-group:aws/spans:*" in document
    searches = template.find_resources("AWS::XRay::TransactionSearchConfig")
    (search,) = searches.values()
    assert any(dep.startswith("TransactionSearchLogsPolicy") for dep in search.get("DependsOn", []))


def test_operational_alarms_report_metrics_and_notify_the_alarm_topic(template):
    (topic_id,) = template.find_resources("AWS::SNS::Topic").keys()
    alarms = template.find_resources("AWS::CloudWatch::Alarm")

    # docs/proposals/operations.md explains each threshold. Every alarm here is either a
    # plain metric (MetricName/Namespace at the top level) or a math expression (a Metrics
    # list of MetricStat entries), so both shapes are read the same way below.
    expected_metrics = {
        "EdgeGateway5xxAlarm": {("AWS/Bedrock-AgentCore", "SystemErrors")},
        "ToolsGateway5xxAlarm": {("AWS/Bedrock-AgentCore", "SystemErrors")},
        "Runtime5xxAlarm": {("AWS/Bedrock-AgentCore", "SystemErrors")},
        "RuntimeLatencyP90Alarm": {("AWS/Bedrock-AgentCore", "Latency")},
        "BedrockThrottlingAlarm": {("AWS/Bedrock", "InvocationThrottles")},
        "EdgeGateway4xxRateAlarm": {
            ("AWS/Bedrock-AgentCore", "UserErrors"),
            ("AWS/Bedrock-AgentCore", "Invocations"),
        },
    }

    matched = {}
    for logical_id, resource in alarms.items():
        for prefix in expected_metrics:
            if logical_id.startswith(prefix):
                matched[prefix] = resource["Properties"]
                break
    assert set(matched) == set(expected_metrics)

    for prefix, props in matched.items():
        assert {"Ref": topic_id} in props["AlarmActions"]
        if "MetricName" in props:
            found = {(props["Namespace"], props["MetricName"])}
        else:
            found = {
                (m["MetricStat"]["Metric"]["Namespace"], m["MetricStat"]["Metric"]["MetricName"])
                for m in props["Metrics"]
                if "MetricStat" in m
            }
        assert found == expected_metrics[prefix]
        assert props["TreatMissingData"] == "notBreaching"


def test_edge_gateway_has_per_user_rate_limit_on_the_jwt_sub_claim(template):
    template.has_resource_properties(
        "AWS::BedrockAgentCore::GatewayRateLimit",
        {
            "DimensionKeys": ["$.context.jwt.sub"],
            "Entries": [
                Match.object_like(
                    {
                        "Dimensions": {"$.context.jwt.sub": "*"},
                        "Requests": [{"Rate": 30, "Period": "minute"}],
                        "Connections": [{"Rate": 2, "Period": "second"}],
                    }
                )
            ],
            "GatewayIdentifier": {
                "Fn::GetAtt": [Match.string_like_regexp("EdgeGateway.*"), "GatewayIdentifier"]
            },
        },
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


def test_conversation_bucket_is_versioned_kms_encrypted_and_expires_at_30_days(template):
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
    assert rule["ExpirationInDays"] == 30
    assert rule["NoncurrentVersionExpiration"] == {"NoncurrentDays": 30}


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


def test_runtime_role_reads_writes_and_lists_thread_objects_only(template):
    document = statements(template, "RuntimeRole")
    (thread_statement,) = [
        s for s in document if s.get("Action") == ["s3:GetObject", "s3:PutObject"]
    ]
    assert "threads/*" in json.dumps(thread_statement["Resource"])
    # ListBucket is scoped to the threads/ prefix: without it S3 answers a GET on a
    # missing key with 403 rather than 404, and the first write of a thread never happens.
    (listing,) = [s for s in document if s.get("Action") == "s3:ListBucket"]
    assert listing["Condition"] == {"StringLike": {"s3:prefix": ["threads/*"]}}
    actions = json.dumps([s.get("Action") for s in document])
    assert "s3:DeleteObject" not in actions
    assert any(s.get("Action") == "secretsmanager:GetSecretValue" for s in document)
    assert any(s.get("Action") == ["kms:Decrypt", "kms:GenerateDataKey"] for s in document)


def test_investigator_role_reads_the_bucket_the_key_and_the_pool_and_nothing_else(template):
    document = statements(template, "ConversationInvestigatorRole")
    actions = sorted(
        action
        for statement in document
        for action in (
            statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
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
        r for r in roles.values() if "resolves a subject" in r["Properties"].get("Description", "")
    ]
    principal = role["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]["Principal"]["AWS"]
    condition_name, when_set, when_blank = principal["Fn::If"]
    assert condition_name == "HasInvestigatorPrincipal"
    assert when_set == {"Ref": "InvestigatorPrincipalArn"}
    assert when_blank == f"arn:aws:iam::{ACCOUNT}:root"
    assert role["Properties"]["MaxSessionDuration"] == 3600


def test_runtime_carries_the_conversation_log_variables_with_the_switch_on(template):
    template.has_resource_properties(
        "AWS::BedrockAgentCore::Runtime",
        {
            "EnvironmentVariables": Match.object_like(
                {
                    "CONVERSATION_LOG_ENABLED": "true",
                    "CONVERSATION_LOG_BUCKET": Match.any_value(),
                    "CONVERSATION_LOG_KEY_SECRET_ARN": Match.any_value(),
                }
            )
        },
    )


def test_vended_log_groups_have_30_day_retention_under_the_shared_prefix(template):
    groups = template.find_resources("AWS::Logs::LogGroup")
    names = {g["Properties"]["LogGroupName"] for g in groups.values()}
    assert names == {
        "/aws/vendedlogs/bedrock-agentcore/guppi-gpt-edge",
        "/aws/vendedlogs/bedrock-agentcore/guppi-gpt-tools",
        "/aws/vendedlogs/bedrock-agentcore/guppi_gpt",
    }
    for group in groups.values():
        assert group["Properties"]["RetentionInDays"] == 30


def test_vended_log_delivery_sources_cover_application_logs_and_traces(template):
    sources = template.find_resources("AWS::Logs::DeliverySource")
    log_types_by_resource = {}
    for source in sources.values():
        props = source["Properties"]
        resource_key = json.dumps(props["ResourceArn"])
        log_types_by_resource.setdefault(resource_key, set()).add(props["LogType"])

    # Every resource gets APPLICATION_LOGS only: a CloudWatch Logs destination for the
    # gateways' TRACES log type was rejected by CloudFormation on 4 Sep 2026.
    assert len(log_types_by_resource) == 3
    assert all(v == {"APPLICATION_LOGS"} for v in log_types_by_resource.values())

    # Each delivery depends explicitly on its source and its destination, since the
    # delivery source name that links them is a plain string, not a CloudFormation
    # reference CDK would otherwise infer a dependency from.
    deliveries = template.find_resources("AWS::Logs::Delivery")
    assert len(deliveries) == len(sources)
    for delivery in deliveries.values():
        assert len(delivery.get("DependsOn", [])) == 2


def test_vended_log_delivery_resource_policy_grants_the_delivery_service(template):
    policies = [
        p["Properties"]["PolicyDocument"]
        for p in template.find_resources("AWS::Logs::ResourcePolicy").values()
        if "delivery.logs.amazonaws.com" in p["Properties"]["PolicyDocument"]
    ]
    (raw,) = policies
    document = json.loads(raw)
    (statement,) = document["Statement"]
    assert statement["Principal"] == {"Service": "delivery.logs.amazonaws.com"}
    assert set(statement["Action"]) == {"logs:CreateLogStream", "logs:PutLogEvents"}
    assert "/aws/vendedlogs/bedrock-agentcore/*" in statement["Resource"]


def test_conversation_key_secret_has_no_template(template):
    secrets = template.find_resources("AWS::SecretsManager::Secret")
    (secret,) = [
        s for s in secrets.values() if "HMAC key" in s["Properties"].get("Description", "")
    ]
    generator = secret["Properties"]["GenerateSecretString"]
    assert generator["PasswordLength"] == 32
    assert "SecretStringTemplate" not in generator and "GenerateStringKey" not in generator


def test_dynatrace_monitoring_role_is_gone(template):
    # The role-based Dynatrace AWS integration was removed on 7 Sep 2026: Dynatrace's own
    # push-based activation stack, deployed outside this repo, polls CloudWatch on its own.
    roles = template.find_resources("AWS::IAM::Role")
    names = {r["Properties"].get("RoleName") for r in roles.values()}
    assert "GuppiGptDynatraceMonitoring" not in names
    rendered = template.to_json()
    assert "DynatraceAwsAccountId" not in rendered.get("Parameters", {})
    assert "DynatraceExternalId" not in rendered.get("Parameters", {})
    assert "HasDynatraceAws" not in rendered.get("Conditions", {})
    assert "DynatraceMonitoringRoleArn" not in rendered.get("Outputs", {})


def test_dynatrace_logs_condition_reuses_the_otlp_parameters(template):
    conditions = template.to_json().get("Conditions", {})
    assert "HasDynatraceLogs" in conditions
    expression = conditions["HasDynatraceLogs"]
    assert "Fn::And" in expression
    assert len(expression["Fn::And"]) == 2
    rendered = json.dumps(expression)
    assert "DynatraceOtlpEndpoint" in rendered
    assert "DynatraceApiToken" in rendered


def test_dynatrace_log_backup_bucket_is_small_and_conditional(template):
    buckets = template.find_resources("AWS::S3::Bucket")
    (backup,) = [
        b
        for b in buckets.values()
        if b["Properties"].get("LifecycleConfiguration", {}).get("Rules", [{}])[0].get("Id")
        == "ExpireFailedDeliveries"
    ]
    assert backup["Condition"] == "HasDynatraceLogs"
    (rule,) = backup["Properties"]["LifecycleConfiguration"]["Rules"]
    assert rule["ExpirationInDays"] == 7
    # Not the site, content, or conversation log buckets.
    assert len(buckets) == 4


def test_dynatrace_firehose_stream_targets_the_dynatrace_http_endpoint(template):
    streams = template.find_resources("AWS::KinesisFirehose::DeliveryStream")
    (stream,) = streams.values()
    assert stream["Condition"] == "HasDynatraceLogs"
    config = stream["Properties"]["HttpEndpointDestinationConfiguration"]
    assert config["EndpointConfiguration"]["Name"] == "Dynatrace"
    assert config["EndpointConfiguration"]["AccessKey"] == {"Ref": "DynatraceApiToken"}
    # The ingest URL is derived from DynatraceOtlpEndpoint: split off its fixed
    # "/api/v2/otlp" suffix to recover the tenant's base URL, then append the logs
    # ingest path, rather than a separate parameter naming the same tenant again.
    assert config["EndpointConfiguration"]["Url"] == {
        "Fn::Join": [
            "",
            [
                {
                    "Fn::Select": [
                        0,
                        {"Fn::Split": ["/api/v2/otlp", {"Ref": "DynatraceOtlpEndpoint"}]},
                    ]
                },
                "/api/v2/logs/ingest/aws_firehose",
            ],
        ]
    }
    assert config["BufferingHints"] == {"IntervalInSeconds": 60, "SizeInMBs": 1}
    assert config["RequestConfiguration"]["ContentEncoding"] == "GZIP"
    assert config["S3BackupMode"] == "FailedDataOnly"


def test_dynatrace_subscription_filters_target_the_three_vended_log_groups(template):
    filters = template.find_resources("AWS::Logs::SubscriptionFilter")
    dynatrace_filters = {
        k: v for k, v in filters.items() if v.get("Condition") == "HasDynatraceLogs"
    }
    assert len(dynatrace_filters) == 3
    (stream_logical_id,) = template.find_resources("AWS::KinesisFirehose::DeliveryStream").keys()
    log_group_refs = set()
    for f in dynatrace_filters.values():
        props = f["Properties"]
        assert props["DestinationArn"] == {"Fn::GetAtt": [stream_logical_id, "Arn"]}
        log_group_refs.add(props["LogGroupName"]["Ref"])
    vended_log_group_ids = set(
        template.find_resources(
            "AWS::Logs::LogGroup",
            Match.object_like({"Properties": {"LogGroupName": Match.any_value()}}),
        ).keys()
    )
    assert log_group_refs == vended_log_group_ids


def test_feedback_method_needs_a_cognito_token_and_a_validated_body(template):
    methods = template.find_resources("AWS::ApiGateway::Method")
    (method,) = [m for m in methods.values() if m["Properties"]["HttpMethod"] == "POST"]
    props = method["Properties"]
    assert props["AuthorizationType"] == "COGNITO_USER_POOLS"
    assert "AuthorizerId" in props
    # A user pool authorizer with no scope reads the bearer as an id token; the page holds
    # the access token, so the method names a scope every issued token claims.
    assert props["AuthorizationScopes"] == ["openid"]
    assert "RequestValidatorId" in props
    assert props["MethodResponses"] == [{"StatusCode": "202"}, {"StatusCode": "400"}]
    (validator,) = template.find_resources("AWS::ApiGateway::RequestValidator").values()
    assert validator["Properties"]["ValidateRequestBody"] is True
    (model,) = template.find_resources("AWS::ApiGateway::Model").values()
    schema = model["Properties"]["Schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"vote", "runId", "threadId"}
    assert schema["properties"]["vote"]["enum"] == ["up", "down", "none"]


def test_feedback_integration_puts_one_event_on_the_bus_with_no_compute(template):
    methods = template.find_resources("AWS::ApiGateway::Method")
    (method,) = [m for m in methods.values() if m["Properties"]["HttpMethod"] == "POST"]
    integration = method["Properties"]["Integration"]
    assert integration["Type"] == "AWS"
    assert integration["IntegrationHttpMethod"] == "POST"
    assert "apigateway:us-east-1:events:action/PutEvents" in json.dumps(integration["Uri"])
    assert integration["RequestParameters"] == {
        "integration.request.header.X-Amz-Target": "'AWSEvents.PutEvents'",
        "integration.request.header.Content-Type": "'application/x-amz-json-1.1'",
    }
    body = integration["RequestTemplates"]["application/json"]
    assert '"EventBusName":"guppi-gpt-feedback"' in body
    assert '"Source":"guppigpt.feedback"' in body
    # The one field the request body cannot supply is the time the request arrived. The
    # caller's Cognito sub claim stays out of the event: the template cannot hash it, and
    # a vote joins its conversation through threadId rather than through the subject.
    assert "$context.requestTimeEpoch" in body
    assert "claims.sub" not in body
    assert "subject" not in body
    assert integration["IntegrationResponses"][0]["StatusCode"] == "202"
    assert integration["IntegrationResponses"][1]["StatusCode"] == "400"


def test_feedback_behavior_is_matched_before_the_api_wildcard(template):
    (distribution,) = template.find_resources("AWS::CloudFront::Distribution").values()
    config = distribution["Properties"]["DistributionConfig"]
    # CloudFront compares the path against these patterns in the order they are listed, so
    # a vote reaches the feedback API rather than the edge gateway only while this holds.
    assert [b["PathPattern"] for b in config["CacheBehaviors"]] == [
        "/api/feedback",
        "/api/*",
        "/sandbox/*",
    ]
    feedback_behavior = config["CacheBehaviors"][0]
    assert feedback_behavior["CachePolicyId"] == "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
    assert feedback_behavior["OriginRequestPolicyId"] == "b689b0a8-53d0-40ab-baf2-68738e2966ac"
    (origin,) = [o for o in config["Origins"] if o["Id"] == feedback_behavior["TargetOriginId"]]
    assert origin["OriginPath"] == "/prod"
    assert "execute-api" in json.dumps(origin["DomainName"])
    # The API authorizes every request itself, so this origin carries no secret header.
    assert "OriginCustomHeaders" not in origin


def test_feedback_bus_and_archive_keep_every_vote_without_dynatrace(template):
    (bus,) = template.find_resources("AWS::Events::EventBus").values()
    assert bus["Properties"]["Name"] == "guppi-gpt-feedback"
    assert "Condition" not in bus
    (archive,) = template.find_resources("AWS::Events::Archive").values()
    assert "Condition" not in archive
    assert archive["Properties"]["RetentionDays"] == 30
    assert archive["Properties"]["EventPattern"] == {"source": ["guppigpt.feedback"]}


def test_feedback_reaches_dynatrace_only_when_the_parameters_are_set(template):
    # All three resources sit under HasDynatraceLogs, the condition that requires both
    # DynatraceOtlpEndpoint and DynatraceApiToken, so CloudFormation creates none of them
    # while either parameter is empty, which is the default.
    (connection,) = template.find_resources("AWS::Events::Connection").values()
    assert connection["Condition"] == "HasDynatraceLogs"
    auth = connection["Properties"]["AuthParameters"]["ApiKeyAuthParameters"]
    assert auth["ApiKeyName"] == "Authorization"
    assert auth["ApiKeyValue"]["Fn::Join"][1] == [
        "Api-Token ",
        {"Ref": "DynatraceApiToken"},
    ]

    (destination,) = template.find_resources("AWS::Events::ApiDestination").values()
    assert destination["Condition"] == "HasDynatraceLogs"
    endpoint = destination["Properties"]["InvocationEndpoint"]
    assert endpoint["Fn::Join"][1][-1] == "/api/v2/bizevents/ingest"

    (rule,) = template.find_resources("AWS::Events::Rule").values()
    assert rule["Condition"] == "HasDynatraceLogs"
    assert rule["Properties"]["EventPattern"] == {"source": ["guppigpt.feedback"]}
    (target,) = rule["Properties"]["Targets"]
    assert target["RetryPolicy"] == {"MaximumRetryAttempts": 2}
    (queue_id,) = template.find_resources("AWS::SQS::Queue").keys()
    assert target["DeadLetterConfig"]["Arn"] == {"Fn::GetAtt": [queue_id, "Arn"]}
    body = target["InputTransformer"]["InputTemplate"]
    assert '"event.type":"guppigpt.reply-feedback"' in body
    assert '"event.provider":"guppigpt"' in body
    for field in ("vote", "run.id", "trace.id", "thread.id", "message.id", "request.id"):
        assert f'"{field}":<' in body
    assert "subject" not in body


def test_feedback_dead_letter_queue_has_an_alarm_under_the_same_condition(template):
    (queue_id,) = template.find_resources("AWS::SQS::Queue").keys()
    alarms = template.find_resources("AWS::CloudWatch::Alarm")
    (alarm,) = [
        a
        for a in alarms.values()
        if a["Properties"].get("Namespace") == "AWS/SQS"
        and a["Properties"].get("MetricName") == "ApproximateNumberOfMessagesVisible"
    ]
    assert alarm["Condition"] == "HasDynatraceLogs"
    assert alarm["Properties"]["Dimensions"] == [
        {"Name": "QueueName", "Value": {"Fn::GetAtt": [queue_id, "QueueName"]}}
    ]
    assert alarm["Properties"]["Threshold"] == 1
    assert alarm["Properties"]["TreatMissingData"] == "notBreaching"
    (action,) = alarm["Properties"]["AlarmActions"]
    assert action == {"Ref": next(iter(template.find_resources("AWS::SNS::Topic")))}


def test_feedback_outputs_name_the_api_and_the_bus(template):
    outputs = template.to_json()["Outputs"]
    assert json.dumps(outputs["FeedbackApiUrl"]["Value"]).endswith('"/api/feedback"]]}')
    (bus_id,) = template.find_resources("AWS::Events::EventBus").keys()
    assert outputs["FeedbackBusName"]["Value"] == {"Ref": bus_id}


PLATFORM_PARAMETERS_FROM_OUTPUTS = {
    "/guppi/platform/site-bucket-name": "SiteBucketName",
    "/guppi/platform/distribution-id": "DistributionId",
    "/guppi/platform/site-url": "SiteUrl",
    "/guppi/platform/edge-gateway-arn": "GatewayArn",
    "/guppi/platform/tools-gateway-url": "ToolsGatewayUrl",
    "/guppi/platform/user-pool-client-id": "UserPoolClientId",
    "/guppi/platform/conversation-log-bucket-name": "ConversationLogBucketName",
    "/guppi/platform/conversation-log-key-secret-arn": "ConversationLogKeySecretArn",
    "/guppi/platform/alarm-topic-arn": "AlarmTopicArn",
}


def platform_parameters(template) -> dict[str, object]:
    return {
        p["Properties"]["Name"]: p["Properties"]["Value"]
        for p in template.find_resources("AWS::SSM::Parameter").values()
        if p["Properties"]["Name"].startswith("/guppi/platform/")
    }


def gateway_by_name(template, name: str) -> tuple[str, dict]:
    gateways = template.find_resources("AWS::BedrockAgentCore::Gateway")
    ((logical_id, gateway),) = [
        (k, g) for k, g in gateways.items() if g["Properties"]["Name"] == name
    ]
    return logical_id, gateway


def test_platform_parameters_are_the_contract_names(template):
    assert set(platform_parameters(template)) == {
        *PLATFORM_PARAMETERS_FROM_OUTPUTS,
        "/guppi/platform/edge-gateway-id",
        "/guppi/platform/edge-gateway-role-arn",
        "/guppi/platform/tools-gateway-id",
        "/guppi/platform/tools-gateway-role-arn",
        "/guppi/platform/jwt-discovery-url",
    }
    for parameter in template.find_resources("AWS::SSM::Parameter").values():
        assert parameter["Properties"]["Type"] == "String"


def test_platform_parameters_carry_the_same_values_as_the_outputs(template):
    parameters = platform_parameters(template)
    outputs = template.to_json()["Outputs"]
    for name, output in PLATFORM_PARAMETERS_FROM_OUTPUTS.items():
        assert parameters[name] == outputs[output]["Value"], name
    assert parameters["/guppi/platform/site-url"] == "https://chat.dengler.io/"


def test_platform_gateway_parameters_point_at_each_gateway_and_its_role(template):
    parameters = platform_parameters(template)
    for prefix, gateway_name in (("edge", "guppi-gpt-edge"), ("tools", "guppi-gpt-tools")):
        logical_id, gateway = gateway_by_name(template, gateway_name)
        assert parameters[f"/guppi/platform/{prefix}-gateway-id"] == {
            "Fn::GetAtt": [logical_id, "GatewayIdentifier"]
        }
        # The role parameter is the very role the gateway runs as.
        role_arn = gateway["Properties"]["RoleArn"]
        assert parameters[f"/guppi/platform/{prefix}-gateway-role-arn"] == role_arn
    assert parameters["/guppi/platform/tools-gateway-url"] == {
        "Fn::GetAtt": ["ToolsGateway", "GatewayUrl"]
    }


def test_platform_jwt_parameters_match_the_edge_gateway_authorizer(template):
    parameters = platform_parameters(template)
    _, gateway = gateway_by_name(template, "guppi-gpt-edge")
    authorizer = gateway["Properties"]["AuthorizerConfiguration"]["CustomJWTAuthorizer"]
    assert parameters["/guppi/platform/jwt-discovery-url"] == authorizer["DiscoveryUrl"]
    assert [parameters["/guppi/platform/user-pool-client-id"]] == authorizer["AllowedClients"]


def function_code(template, comment_prefix: str) -> tuple[str, dict]:
    functions = template.find_resources("AWS::CloudFront::Function")
    ((logical_id, function),) = [
        (k, f)
        for k, f in functions.items()
        if f["Properties"]["FunctionConfig"]["Comment"].startswith(comment_prefix)
    ]
    return logical_id, function


def viewer_request_function(behavior: dict) -> str:
    (association,) = behavior["FunctionAssociations"]
    assert association["EventType"] == "viewer-request"
    return association["FunctionARN"]["Fn::GetAtt"][0]


def test_two_cloudfront_functions_carry_the_code_from_the_functions_directory(template):
    functions = template.find_resources("AWS::CloudFront::Function")
    assert len(functions) == 2
    for comment, file_name in (
        ("Serves /p/<name>/", "page-path.js"),
        ("Maps /api/<name>/invocations", "agent-path.js"),
    ):
        _, function = function_code(template, comment)
        assert function["Properties"]["FunctionConfig"]["Runtime"] == "cloudfront-js-2.0"
        source = (FUNCTIONS_DIR / file_name).read_text()
        assert function["Properties"]["FunctionCode"] == source


def test_page_path_function_is_on_the_default_behavior(template):
    (distribution,) = template.find_resources("AWS::CloudFront::Distribution").values()
    config = distribution["Properties"]["DistributionConfig"]
    page_id, _ = function_code(template, "Serves /p/<name>/")
    assert viewer_request_function(config["DefaultCacheBehavior"]) == page_id


def test_agent_path_function_is_on_the_api_wildcard_only(template):
    (distribution,) = template.find_resources("AWS::CloudFront::Distribution").values()
    config = distribution["Properties"]["DistributionConfig"]
    agent_id, _ = function_code(template, "Maps /api/<name>/invocations")
    behaviors = {b["PathPattern"]: b for b in config["CacheBehaviors"]}
    assert viewer_request_function(behaviors["/api/*"]) == agent_id
    # The feedback behavior keeps its path and is still matched first.
    assert "FunctionAssociations" not in behaviors["/api/feedback"]
    assert list(behaviors) == ["/api/feedback", "/api/*", "/sandbox/*"]


def test_sandbox_behavior_serves_the_site_bucket_with_the_sandbox_headers(template):
    """/sandbox/frame.html holds the MCP Apps bridge; its CSP lets the app's inline script
    and style run, fetches nothing, and lets only this site frame it."""
    (distribution,) = template.find_resources("AWS::CloudFront::Distribution").values()
    config = distribution["Properties"]["DistributionConfig"]
    behaviors = {b["PathPattern"]: b for b in config["CacheBehaviors"]}
    sandbox = behaviors["/sandbox/*"]
    default = config["DefaultCacheBehavior"]
    # The same S3 origin as the page, not the gateway, and no path rewrite.
    assert sandbox["TargetOriginId"] == default["TargetOriginId"]
    assert "FunctionAssociations" not in sandbox
    assert sandbox["CachePolicyId"] == default["CachePolicyId"]
    assert sandbox["ViewerProtocolPolicy"] == "redirect-to-https"

    policy_id, policy = headers_policy(template, "CSP and security headers for the MCP Apps")
    assert sandbox["ResponseHeadersPolicyId"] == {"Ref": policy_id}
    headers = policy["Properties"]["ResponseHeadersPolicyConfig"]["SecurityHeadersConfig"]
    assert headers["ContentSecurityPolicy"] == {
        "ContentSecurityPolicy": (
            "default-src 'none'; script-src 'self' 'unsafe-inline'; "
            "style-src 'unsafe-inline'; img-src data:; frame-ancestors 'self'"
        ),
        "Override": True,
    }
    csp = headers["ContentSecurityPolicy"]["ContentSecurityPolicy"]
    assert "connect-src" not in csp
    # frame-ancestors decides who may frame it; X-Frame-Options DENY would refuse the page.
    assert "FrameOptions" not in headers
    assert headers["ContentTypeOptions"] == {"Override": True}
    assert headers["StrictTransportSecurity"]["AccessControlMaxAgeSec"] == 31536000


def test_the_page_may_frame_only_its_own_origin(template):
    _, policy = headers_policy(template, "CSP and security headers for the static page")
    csp = policy["Properties"]["ResponseHeadersPolicyConfig"]["SecurityHeadersConfig"][
        "ContentSecurityPolicy"
    ]["ContentSecurityPolicy"]
    for branch in csp["Fn::If"][1:]:
        rendered = json.dumps(branch)
        assert "frame-src 'self';" in rendered
        assert "frame-ancestors 'none';" in rendered
