# Copyright 2025 Google LLC
# SPDX-License-Identifier: Apache-2.0

"""Development API for inspecting and interacting with Genkit.

This module provides a reflection API server for inspection and interaction
during development. It exposes endpoints for health checks, action discovery,
and action execution.
"""

from __future__ import annotations

import asyncio
import json
import urllib.parse
from collections.abc import AsyncGenerator
from http.server import BaseHTTPRequestHandler
from typing import Any

import structlog
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from genkit.codec import dump_dict, dump_json
from genkit.core.action import Action
from genkit.core.constants import DEFAULT_GENKIT_VERSION
from genkit.core.error import get_callable_json
from genkit.core.registry import Registry
from genkit.web.requests import (
    is_streaming_requested,
)
from genkit.web.servers.signals import terminate_all_servers
from genkit.web.typing import (
    Application,
    LifespanHandler,
)

logger = structlog.get_logger(__name__)


def make_reflection_server(registry: Registry, encoding='utf-8'):
    """Create and return a ReflectionServer class with the given registry.

    Args:
        registry: The registry to use for the reflection server.
        encoding: The text encoding to use; default 'utf-8'.

    Returns:
        A ReflectionServer class configured with the given registry.
    """

    class ReflectionServer(BaseHTTPRequestHandler):
        """HTTP request handler for the Genkit reflection API.

        This handler provides endpoints for inspecting and interacting with
        registered Genkit actions during development.
        """

        def do_GET(self) -> None:  # noqa: N802
            """Handle GET requests to the reflection API.

            Endpoints:
                - /api/__health: Returns 200 OK if the server is healthy
                - /api/actions: Returns JSON describing all registered actions

            For the /api/actions endpoint, returns a JSON object mapping action
            keys to their metadata, including input/output schemas.
            """
            if self.path == '/api/__health':
                self.send_response(200, 'OK')
                self.end_headers()

            elif self.path == '/api/actions':
                self.send_response(200)
                self.send_header('content-type', 'application/json')
                self.end_headers()
                actions = registry.list_serializable_actions()
                self.wfile.write(bytes(json.dumps(actions), encoding))
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self) -> None:  # noqa: N802
            """Handle POST requests to the reflection API.

            Flow:
                1. Reads and validates the request payload
                2. Looks up the requested action
                3. Executes the action with the provided input
                4. Returns the action result as JSON with trace ID

            The response format varies based on whether the action returns a
            Pydantic model or a plain value.
            """
            if self.path == '/api/notify':
                self.send_response(200)
                self.end_headers()

            elif self.path.startswith('/api/runAction'):
                content_len = int(self.headers.get('content-length') or 0)
                post_body = self.rfile.read(content_len)
                payload = json.loads(post_body.decode(encoding=encoding))
                action = registry.lookup_action_by_key(payload['key'])
                context = payload['context'] if 'context' in payload else {}

                query = urllib.parse.urlparse(self.path).query
                query_params = urllib.parse.parse_qs(query)
                stream = query_params.get('stream', ['false'])[0] == 'true'
                if stream:

                    def send_chunk(chunk):
                        self.wfile.write(
                            bytes(
                                dump_json(chunk),
                                encoding,
                            )
                        )
                        self.wfile.write(bytes('\n', encoding))

                    self.send_response(200)
                    self.send_header('x-genkit-version', DEFAULT_GENKIT_VERSION)
                    # TODO: Since each event being sent down the wire is a JSON
                    # chunk, shouldn't this be set to text/event-stream?
                    self.send_header('content-type', 'application/json')
                    self.end_headers()

                    try:
                        output = asyncio.run(
                            action.arun_raw(
                                raw_input=payload['input'],
                                on_chunk=send_chunk,
                                context=context,
                            )
                        )
                        self.wfile.write(
                            bytes(
                                json.dumps({
                                    'result': dump_dict(output.response),
                                    'telemetry': {'traceId': output.trace_id},
                                }),
                                encoding,
                            )
                        )
                    except Exception as e:
                        # Since we're streaming, the headers have already been
                        # sent as a 200 OK, but we must indicate an error
                        # regardless.
                        error_response = get_callable_json(e).model_dump(
                            by_alias=True
                        )
                        logger.error(
                            'Error streaming action', error=error_response
                        )
                        self.wfile.write(
                            bytes(
                                json.dumps({'error': error_response}), encoding
                            )
                        )
                else:
                    try:
                        output = asyncio.run(
                            action.arun_raw(
                                raw_input=payload['input'], context=context
                            )
                        )

                        self.send_response(200)
                        self.send_header(
                            'x-genkit-version', DEFAULT_GENKIT_VERSION
                        )
                        self.send_header('content-type', 'application/json')
                        self.end_headers()

                        self.wfile.write(
                            bytes(
                                json.dumps({
                                    'result': dump_dict(output.response),
                                    'telemetry': {'traceId': output.trace_id},
                                }),
                                encoding,
                            )
                        )
                    except Exception as e:
                        # We aren't streaming here so send a JSON-encoded 500
                        # internal server error response.
                        error_response = get_callable_json(e).model_dump(
                            by_alias=True
                        )
                        logger.error(
                            'Error running action', error=error_response
                        )

                        self.send_response(500)
                        self.send_header(
                            'x-genkit-version', DEFAULT_GENKIT_VERSION
                        )
                        self.send_header('content-type', 'application/json')
                        self.end_headers()
                        self.wfile.write(
                            bytes(json.dumps(error_response), encoding)
                        )

    return ReflectionServer


def create_reflection_asgi_app(
    registry: Registry,
    on_app_startup: LifespanHandler | None = None,
    on_app_shutdown: LifespanHandler | None = None,
    version: str = DEFAULT_GENKIT_VERSION,
    encoding: str = 'utf-8',
) -> Application:
    """Create and return a ASGI application for the Genkit reflection API.

    Key endpoints:

        | Method | Path           | Handler               |
        |--------|----------------|-----------------------|
        | GET    | /api/__health  | Health check          |
        | GET    | /api/actions   | List actions          |
        | POST   | /api/runAction | Run action (streaming)|
        | POST   | /api/notify    | Handle notification   |

    Args:
        registry: The registry to use for the reflection server.
        on_app_startup: Optional callback to execute when the app's
            lifespan starts. Must be an async function.
        on_app_shutdown: Optional callback to execute when the app's
            lifespan ends. Must be an async function.
        version: The version string to use when setting the value of
            the X-GENKIT-VERSION HTTP header.
        encoding: The text encoding to use; default 'utf-8'.

    Returns:
        An ASGI application configured with the given registry.
    """

    async def health_check(request: Request) -> JSONResponse:
        """Handle health check requests.

        Args:
            request: The Starlette request object.

        Returns:
            A JSON response with status code 200.
        """
        return JSONResponse(content={'status': 'OK'})

    async def terminate(request: Request) -> JSONResponse:
        """Handle the quit endpoint.

        Args:
            request: The Starlette request object.

        Returns:
            An empty JSON response with status code 200.
        """
        await logger.ainfo('Shutting down servers...')
        terminate_all_servers()
        return JSONResponse(content={'status': 'OK'})

    async def list_actions(request: Request) -> JSONResponse:
        """Handle the request for listing available actions.

        Args:
            request: The Starlette request object.

        Returns:
            A JSON response containing all serializable actions.
        """
        return JSONResponse(
            content=registry.list_serializable_actions(),
            status_code=200,
            headers={'x-genkit-version': version},
        )

    async def handle_notify(request: Request) -> JSONResponse:
        """Handle the notification endpoint.

        Args:
            request: The Starlette request object.

        Returns:
            An empty JSON response with status code 200.
        """
        return JSONResponse(
            content={},
            status_code=200,
            headers={'x-genkit-version': version},
        )

    async def run_action(
        request: Request,
    ) -> JSONResponse | StreamingResponse:
        """Handle the runAction endpoint for executing registered actions.

        Flow:
        1. Reads and validates the request payload
        2. Looks up the requested action
        3. Executes the action with the provided input
        4. Returns the action result as JSON with trace ID

        Args:
            request: The Starlette request object.

        Returns:
            A JSON or StreamingResponse with the action result, or an error
            response.
        """
        # Get the action.
        payload = await request.json()
        action = registry.lookup_action_by_key(payload['key'])
        if action is None:
            return JSONResponse(
                content={'error': f'Action not found: {payload["key"]}'},
                status_code=404,
            )

        # Run the action.
        context = payload.get('context', {})
        stream = is_streaming_requested(request)
        handler = run_streaming_action if stream else run_standard_action
        return await handler(action, payload, context, version)

    async def run_streaming_action(
        action: Action,
        payload: dict[str, Any],
        context: dict[str, Any],
        version: str,
    ) -> StreamingResponse | JSONResponse:
        """Handle streaming action execution for Starlette.

        Args:
            action: The action to execute.
            payload: Request payload with input data.
            context: Execution context.
            version: The Genkit version header value.

        Returns:
            A StreamingResponse with JSON chunks containing result or error
            events.
        """

        async def sse_generator() -> AsyncGenerator[str, None]:
            """Server-Sent Events Generator for streaming JSON chunks.

            Since we generate a stream of event objects, and the headers will
            have been sent already if an error occurs at a later stage, we
            indicate an error status by streaming an error object.
            """
            try:

                async def send_chunk(chunk):
                    out = json.dumps(chunk)
                    yield f'{out}\n'

                output = await action.arun_raw(
                    raw_input=payload['input'],
                    on_chunk=send_chunk,
                    context=context,
                )

                final_response = {
                    'result': dump_dict(output.response),
                    'telemetry': {'traceId': output.trace_id},
                }
                yield f'{json.dumps(final_response)}\n'

            except Exception as e:
                error_response = get_callable_json(e).model_dump(by_alias=True)
                await logger.aerror(
                    'Error streaming action',
                    error=error_response,
                )
                yield f'{json.dumps(error_response)}\n'

        return StreamingResponse(
            sse_generator(),
            media_type='text/event-stream',
            headers={'x-genkit-version': version},
        )

    async def run_standard_action(
        action: Action,
        payload: dict[str, Any],
        context: dict[str, Any],
        version: str,
    ) -> JSONResponse:
        """Handle standard (non-streaming) action execution for Starlette.

        Args:
            action: The action to execute.
            payload: Request payload with input data.
            context: Execution context.
            version: The Genkit version header value.

        Returns:
            A JSONResponse with the action result or error.
        """
        try:
            output = await action.arun_raw(
                raw_input=payload['input'], context=context
            )
            response = {
                'result': dump_dict(output.response),
                'telemetry': {'traceId': output.trace_id},
            }
            return JSONResponse(
                content=response,
                status_code=200,
                headers={'x-genkit-version': version},
            )
        except Exception as e:
            error_response = get_callable_json(e).model_dump(by_alias=True)
            await logger.aerror('Error executing action', error=error_response)
            return JSONResponse(
                content=error_response,
                status_code=500,
            )

    return Starlette(
        routes=[
            Route('/api/__health', health_check, methods=['GET']),
            Route('/api/__quitquitquit', terminate, methods=['POST']),
            Route('/api/actions', list_actions, methods=['GET']),
            Route('/api/notify', handle_notify, methods=['POST']),
            Route('/api/runAction', run_action, methods=['POST']),
        ],
        middleware=[
            Middleware(
                CORSMiddleware,
                allow_origins=['*'],
                allow_methods=['*'],
                allow_headers=['*'],
            )
        ],
        on_startup=[on_app_startup] if on_app_startup else [],
        on_shutdown=[on_app_shutdown] if on_app_shutdown else [],
    )
