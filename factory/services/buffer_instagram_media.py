"""The tenant-specific Buffer Instagram experiment is excluded from this release."""
from factory.providers.base import ProviderError


def prepare(ctx, account, video, pub, metadata, checkpoint):
    raise ProviderError('Use the direct Instagram adapter for cover publishing.', 'unsupported_provider', False)
