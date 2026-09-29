import aws_cdk as cdk
from aws_cdk.assertions import Template
from guppi_mcp_app_infra.stack import GuppiMcpAppStack

ACCOUNT = "123456789012"
REGION = "us-east-1"


def synth() -> Template:
    app = cdk.App()
    stack = GuppiMcpAppStack(
        app, "GuppiMcpApp", env=cdk.Environment(account=ACCOUNT, region=REGION)
    )
    return Template.from_stack(stack)


def test_stack_synthesizes():
    synth()
