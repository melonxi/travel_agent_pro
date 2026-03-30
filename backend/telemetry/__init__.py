# backend/telemetry/__init__.py
from telemetry.setup import setup_telemetry
from telemetry.decorators import traced

__all__ = ["setup_telemetry", "traced"]
