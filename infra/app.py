"""CDK app entry. One stack, one region."""

import os

import aws_cdk as cdk
from guppi_gpt_infra.stack import GuppiGptStack

REGION = "us-east-1"

app = cdk.App()
GuppiGptStack(
    app,
    "GuppiGpt",
    env=cdk.Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"), region=REGION),
    description="GuppiGPT: stateless chat page on AgentCore (spike stage)",
)
app.synth()
