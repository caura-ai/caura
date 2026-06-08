"""Pure-ASGI middlewares for core-storage-api."""

from core_storage_api.middleware.role_filter import RejectWritesOnReaderMiddleware

__all__ = ["RejectWritesOnReaderMiddleware"]
