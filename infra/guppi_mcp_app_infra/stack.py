"""The GuppiMcpApp stack. It reads the platform's /guppi/platform/* parameters and adds
this project's MCP server Runtime and its target on the platform's tools gateway."""

import aws_cdk as cdk
from constructs import Construct


class GuppiMcpAppStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
