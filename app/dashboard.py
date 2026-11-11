from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

STATIC = Path(__file__).parent / "static"


def configure_dashboard(app: FastAPI) -> None:
    app.mount("/assets", StaticFiles(directory=STATIC), name="assets")

    @app.get("/", include_in_schema=False)
    async def dashboard():
        return FileResponse(
            STATIC / "index.html",
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": (
                    "default-src 'self'; script-src 'self'; style-src 'self'; "
                    "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
                ),
            },
        )
