#!/usr/bin/env python

# Copyright 2025 Google LLC All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Entry point for the Google Analytics MCP server."""

import asyncio
import os
import uvicorn
import sys
import analytics_mcp.coordinator as coordinator
from mcp.server.lowlevel import NotificationOptions
from mcp.server.models import InitializationOptions
import mcp.server.stdio
import mcp.server
import traceback
import contextlib
from collections.abc import AsyncIterator
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.types import Receive, Scope, Send
from pydantic import AnyHttpUrl
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings


AUTH0_ISSUER = "https://dev-qcfguwv7uyogdyng.us.auth0.com/"
MCP_AUDIENCE = "https://ga4-mcp-server-180590969315.europe-west3.run.app/mcp/"
REQUIRED_SCOPE = "use:mcp"


# Streamable HTTP session manager for remote MCP clients.
session_manager = StreamableHTTPSessionManager(
    app=coordinator.app,
    event_store=None,
    json_response=False,
    stateless=False,
)


@contextlib.asynccontextmanager
async def lifespan(app: Starlette) -> AsyncIterator[None]:
    """Manage the Streamable HTTP session manager lifecycle."""
    async with session_manager.run():
        yield


async def handle_mcp(scope: Scope, receive: Receive, send: Send) -> None:
    """Handle Streamable HTTP MCP requests."""
    await session_manager.handle_request(scope, receive, send)


http_app = Starlette(
    routes=[
        Mount("/mcp", app=handle_mcp),
    ],
    lifespan=lifespan,
)


async def run_server_async():
    """Runs the MCP server over standard I/O."""
    print("Starting MCP Stdio Server:", coordinator.app.name, file=sys.stderr)
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await coordinator.app.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name=coordinator.app.name,  # Use the server name defined above
                server_version="1.0.0",
                capabilities=coordinator.app.get_capabilities(
                    # Define server capabilities - consult MCP docs for options
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


def run_server():
    """Synchronous wrapper to run the async MCP server."""
    asyncio.run(run_server_async())


def run_http_server():
    """Runs the MCP server over Streamable HTTP."""
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(http_app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    try:
        transport = os.environ.get("MCP_TRANSPORT", "stdio")

        if transport == "http":
            run_http_server()
        else:
            run_server()
    except KeyboardInterrupt:
        print(f"\nMCP Server ({transport}) stopped by user.", file=sys.stderr)
    except Exception:
        print(f"MCP Server ({transport}) encountered an error:", file=sys.stderr)
        traceback.print_exc()
    finally:
        print(f"MCP Server ({transport}) process exiting.", file=sys.stderr)
