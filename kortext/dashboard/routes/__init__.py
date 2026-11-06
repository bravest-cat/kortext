"""
Kortext Dashboard modular route package.
Decomposes dashboard functionality into discrete APIRouters:
- status: Health checks, substrate telemetry, TCP/HTTP checkers, collection point stats.
- journal: Journal parsing and file reading.
- query: Retrieval sandbox and graph/symbol queries.
- actions: Action triggers (dream consolidation, collection creation, cache clearing, task management, SCIP/symbol indexing).
"""

from kortext.dashboard.routes.actions import router as actions_router
from kortext.dashboard.routes.journal import router as journal_router
from kortext.dashboard.routes.memory import router as memory_router
from kortext.dashboard.routes.query import router as query_router
from kortext.dashboard.routes.status import router as status_router

all_routers = [status_router, journal_router, query_router, actions_router, memory_router]

__all__ = [
    "actions_router",
    "journal_router",
    "memory_router",
    "query_router",
    "status_router",
    "all_routers",
]
