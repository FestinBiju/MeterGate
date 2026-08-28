"""Shared structural-integrity failures for deterministic commerce checks."""


class IntegrityStructureError(ValueError):
    """Persisted integrity material cannot be safely interpreted."""

    def __init__(self, resource: str) -> None:
        self.resource = resource
        super().__init__(f"Persisted {resource} integrity material is invalid")
