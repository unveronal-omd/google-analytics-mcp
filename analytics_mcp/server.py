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
from starlette.routing import Mount, Route
from starlette.responses import RedirectResponse, Response
from starlette.requests import Request
import httpx
from urllib.parse import urlencode
from starlette.types import Receive, Scope, Send
from pydantic import AnyHttpUrl
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
import jwt
from jwt import PyJWKClient
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from mcp.server.auth.middleware.bearer_auth import (
    BearerAuthBackend,
    RequireAuthMiddleware,
)
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.routes import (
    build_resource_metadata_url,
    create_protected_resource_routes,
)


AUTH0_ISSUER = "https://dev-qcfguwv7uyogdyng.us.auth0.com/"
MCP_AUDIENCE = "https://ga4-mcp-server-180590969315.europe-west3.run.app/mcp/"
REQUIRED_SCOPE = "use:mcp"


auth_settings = AuthSettings(
    issuer_url=AnyHttpUrl(AUTH0_ISSUER),
    resource_server_url=AnyHttpUrl(MCP_AUDIENCE),
    required_scopes=[REQUIRED_SCOPE],
    validate_token_resource=False,
)


class Auth0TokenVerifier(TokenVerifier):
    """Verify Auth0-issued JWT access tokens."""

    def __init__(self):
        self.jwks_client = PyJWKClient(
            f"{AUTH0_ISSUER}.well-known/jwks.json"
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            signing_key = self.jwks_client.get_signing_key_from_jwt(token)

            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=MCP_AUDIENCE,
                issuer=AUTH0_ISSUER,
            )

            scopes = payload.get("scope", "").split()

            if REQUIRED_SCOPE not in scopes:
                return None

            return AccessToken(
                token=token,
                client_id=payload.get("sub", ""),
                scopes=scopes,
                expires_at=payload.get("exp"),
            )

        except Exception as exc:
            print(f"Auth0 token validation failed: {exc}", file=sys.stderr)
            return None


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


async def authorize_proxy(request):
    """Redirect OAuth authorization requests to Auth0."""
    params = dict(request.query_params)
    auth0_authorize_url = f"{AUTH0_ISSUER}authorize?{urlencode(params)}"
    return RedirectResponse(auth0_authorize_url)


async def token_proxy(request: Request):
    """Forward OAuth token requests to Auth0."""
    body = await request.body()

    headers = {
        "Content-Type": request.headers.get(
            "content-type",
            "application/x-www-form-urlencoded",
        )
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{AUTH0_ISSUER}oauth/token",
            content=body,
            headers=headers,
        )

    return Response(
        content=response.content,
        status_code=response.status_code,
        media_type=response.headers.get("content-type"),
    )


resource_metadata_url = build_resource_metadata_url(
    auth_settings.resource_server_url
)

protected_mcp = RequireAuthMiddleware(
    handle_mcp,
    required_scopes=auth_settings.required_scopes or [],
    resource_metadata_url=resource_metadata_url,
)

http_app = Starlette(
    routes=[
        Route("/authorize", endpoint=authorize_proxy, methods=["GET"]),
        Route("/token", endpoint=token_proxy, methods=["POST"]),
        *create_protected_resource_routes(
            resource_url=auth_settings.resource_server_url,
            authorization_servers=[auth_settings.issuer_url],
            scopes_supported=auth_settings.required_scopes,
        ),
        Mount("/mcp", app=protected_mcp),
    ],
    middleware=[
        Middleware(
            AuthenticationMiddleware,
            backend=BearerAuthBackend(Auth0TokenVerifier()),
        ),
        Middleware(AuthContextMiddleware),
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
