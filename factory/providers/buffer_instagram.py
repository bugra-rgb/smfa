"""Buffer Instagram is not enabled in the community distribution.
Use the direct Instagram OAuth adapter. No tenant binding is shipped.
"""
from .base import ProviderError

def ready(connection):
    return False

class BufferInstagramPublisher:
    def __init__(self, *args, **kwargs):
        raise ProviderError("Use the direct Instagram adapter; Buffer Instagram is not configured in this distribution.", "unsupported_provider", False)
