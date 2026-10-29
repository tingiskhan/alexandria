"""Errors a GraphClient raises."""


class GraphError(Exception):
    """A Graph or sign-in request failed. The message carries Graph's error code and text."""


class ResyncRequiredError(GraphError):
    """The delta link has expired (HTTP 410), so the drive has to be listed again from the start."""
