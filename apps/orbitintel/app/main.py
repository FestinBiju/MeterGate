"""ASGI entrypoint for OrbitIntel."""

from app.application import create_app

app = create_app()
