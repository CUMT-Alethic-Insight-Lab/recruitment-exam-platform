from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from .. import org_content
from ..org_auth import require_org_identity

router = APIRouter(prefix="/api/v1", tags=["organization content"])

Lifecycle = Literal["idea", "proposed", "approved", "recruiting", "active", "paused", "completed", "archived"]


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    title: str = Field(min_length=1, max_length=200, pattern=r"\S")
    description: str = Field(default="", max_length=10000)
    lifecycle: Lifecycle = "proposed"
    recruiting: bool = False


class ProjectUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str | None = Field(default=None, min_length=1, max_length=80, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    title: str | None = Field(default=None, min_length=1, max_length=200, pattern=r"\S")
    description: str | None = Field(default=None, max_length=10000)
    lifecycle: Lifecycle | None = None
    recruiting: bool | None = None


def web_admin(request):
    identity = require_org_identity(request, web_only=True)
    if identity.role != "admin":
        raise HTTPException(403, "web administrator required")
    return identity


@router.get("/projects")
def list_projects(
    request: Request,
    limit: int = Query(50, ge=1, le=100),
    cursor: str | None = Query(None, max_length=512),
    recruiting: bool | None = None,
):
    identity = require_org_identity(request, scope="projects:read")
    return org_content.projects(identity, limit, cursor, recruiting)


@router.get("/projects/{project_id}")
def get_project(project_id: str, request: Request):
    return org_content.project(require_org_identity(request, scope="projects:read"), project_id)


@router.post("/projects", status_code=201)
def create_project(body: ProjectCreate, request: Request):
    return org_content.save_project(web_admin(request), body.model_dump())


@router.patch("/projects/{project_id}")
def update_project(project_id: str, body: ProjectUpdate, request: Request):
    identity = web_admin(request)
    values = body.model_dump(exclude_unset=True)
    if any(value is None for value in values.values()):
        raise HTTPException(422, "project fields cannot be null")
    return org_content.save_project(identity, values, project_id)


@router.get("/knowledge/search")
def search_knowledge(
    request: Request,
    q: str = Query(min_length=1, max_length=200),
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = Query(None, max_length=512),
):
    return org_content.search(require_org_identity(request, scope="knowledge:read"), q, limit, cursor)


@router.get("/knowledge/documents/{document_id}")
def get_document(document_id: str, request: Request):
    return org_content.document(require_org_identity(request, scope="knowledge:read"), document_id)
