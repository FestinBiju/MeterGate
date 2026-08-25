"""Safe domain failures translated at the HTTP boundary."""


class DomainError(Exception):
    """Base class for expected domain failures."""


class ResourceNotFoundError(DomainError):
    def __init__(self, resource: str, resource_id: str) -> None:
        self.resource = resource
        self.resource_id = resource_id
        super().__init__(f"{resource} '{resource_id}' was not found")


class SlugConflictError(DomainError):
    def __init__(self, resource: str, slug: str) -> None:
        self.resource = resource
        self.slug = slug
        super().__init__(f"{resource} slug '{slug}' already exists")


class InvalidStateTransitionError(DomainError):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
