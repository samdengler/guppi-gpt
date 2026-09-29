"""CDK app entry. One stack, one region."""

import os

import aws_cdk as cdk
from guppi_mcp_app_infra.stack import GuppiMcpAppStack

REGION = "us-east-1"

app = cdk.App()
GuppiMcpAppStack(
    app,
    "GuppiMcpApp",
    env=cdk.Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"), region=REGION),
    description="guppi-mcp-app: an MCP server behind the GuppiGPT tools gateway",
)
app.synth()
