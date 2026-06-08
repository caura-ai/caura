"""Repository layer — single point of DB access for the OSS backend.

All SQLAlchemy queries live in repository classes. Services and routes
must use these repositories instead of executing SQL directly.
"""


def scope_sql(
    tenant_id: str,
    fleet_id: str | None,
    table: str = "m",
) -> tuple[str, dict]:
    """Build a WHERE clause fragment for tenant + optional fleet scoping."""
    clause = f"{table}.tenant_id = :tenant_id"
    params: dict = {"tenant_id": tenant_id}
    if fleet_id is not None:
        clause += f" AND {table}.fleet_id = :fleet_id"
        params["fleet_id"] = fleet_id
    return clause, params


from core_api.repositories.agent_repository import AgentRepository
from core_api.repositories.audit_repository import AuditRepository
from core_api.repositories.document_repository import DocumentRepository
from core_api.repositories.entity_repository import EntityRepository
from core_api.repositories.fleet_repository import FleetRepository
from core_api.repositories.memory_repository import MemoryRepository
from core_api.repositories.report_repository import ReportRepository
from core_api.repositories.task_repository import TaskRepository

# Module-level singletons (stateless — safe to share)
agent_repo = AgentRepository()
audit_repo = AuditRepository()
document_repo = DocumentRepository()
entity_repo = EntityRepository()
fleet_repo = FleetRepository()
memory_repo = MemoryRepository()
report_repo = ReportRepository()
task_repo = TaskRepository()

__all__ = [
    "AgentRepository",
    "AuditRepository",
    "DocumentRepository",
    "EntityRepository",
    "FleetRepository",
    "MemoryRepository",
    "ReportRepository",
    "TaskRepository",
    "agent_repo",
    "audit_repo",
    "document_repo",
    "entity_repo",
    "fleet_repo",
    "memory_repo",
    "report_repo",
    "task_repo",
]
