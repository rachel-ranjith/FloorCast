"""AWS Lambda entrypoint — wraps the FastAPI app with Mangum.

Deploy behind API Gateway (HTTP API). The handler is `floorcast.lambda_handler.handler`.
"""

from __future__ import annotations

from mangum import Mangum

from floorcast.api.main import app

handler = Mangum(app, lifespan="off")
