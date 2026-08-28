"""ASGI entry point for the MeterGate API."""

from app.application import create_app

app = create_app()
