"""A minimal Ray Serve application used by the RayService samples.

Deploy with a serveConfigV2 whose application import_path is ``serve_app:app``.
"""

from __future__ import annotations

from typing import Dict


def build_app():
    from ray import serve
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    @serve.deployment(num_replicas=2)
    class Echo:
        def __init__(self, prefix: str = "hello"):
            self.prefix = prefix

        async def __call__(self, request: Request) -> Dict[str, str]:
            name = request.path_params.get("name", "ray")
            return {"message": f"{self.prefix}, {name}"}

    return Echo.bind("hello")


app = build_app()
