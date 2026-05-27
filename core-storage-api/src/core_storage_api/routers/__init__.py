from core_storage_api.routers.agents import router as agents_router
from core_storage_api.routers.audit import router as audit_router
from core_storage_api.routers.debug import router as debug_router
from core_storage_api.routers.documents import router as documents_router
from core_storage_api.routers.entities import router as entities_router
from core_storage_api.routers.fleet import router as fleet_router
from core_storage_api.routers.health import router as health_router
from core_storage_api.routers.idempotency import router as idempotency_router
from core_storage_api.routers.keystones import router as keystones_router
from core_storage_api.routers.lifecycle_audit import router as lifecycle_audit_router
from core_storage_api.routers.memories import router as memories_router
from core_storage_api.routers.purge import router as purge_router
from core_storage_api.routers.reports import router as reports_router
from core_storage_api.routers.tasks import router as tasks_router

__all__ = [
    "agents_router",
    "audit_router",
    "debug_router",
    "documents_router",
    "entities_router",
    "fleet_router",
    "health_router",
    "idempotency_router",
    "keystones_router",
    "lifecycle_audit_router",
    "memories_router",
    "purge_router",
    "reports_router",
    "tasks_router",
]
