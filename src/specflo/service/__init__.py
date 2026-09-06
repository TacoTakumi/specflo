"""The service facade the CLI performs every project artifact operation through.

``ProjectService`` names the operations; ``LocalProjectService`` performs them
as in-process calls on the files under a checkout root.
"""

from .local import LocalProjectService
from .protocol import ProjectService

__all__ = ["LocalProjectService", "ProjectService"]
