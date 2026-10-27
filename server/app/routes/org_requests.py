from typing import Literal

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .. import service_requests
from ..org_auth import require_org_identity

router = APIRouter(prefix="/api/v1", tags=["organization applications"])


class DraftBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["project-join"] = "project-join"
    form_revision: str = Field(min_length=1, max_length=100)
    fields: dict
    draft_id: str | None = Field(default=None, min_length=1, max_length=200)


class ValidateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["project-join"] = "project-join"
    form_revision: str = Field(min_length=1, max_length=100)
    fields: dict


class SubmitBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft_id: str = Field(min_length=1, max_length=200)
    confirmation: str = Field(min_length=1, max_length=500)


class ReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["in_review", "needs_information", "approved", "rejected"]
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("reason")
    @classmethod
    def nonblank_reason(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("A review reason is required")
        return value


@router.get("/forms/project-join/schema")
def project_join_schema(request: Request):
    require_org_identity(request, scope="applications:create")
    return service_requests.schema()


@router.post("/applications/drafts", status_code=201)
def draft(body: DraftBody, request: Request):
    identity = require_org_identity(request, scope="applications:create")
    return service_requests.save_draft(identity, body.kind, body.form_revision, body.fields, body.draft_id)


@router.post("/applications/validate")
def validate(body: ValidateBody, request: Request):
    require_org_identity(request, scope="applications:create")
    return service_requests.validate(body.kind, body.form_revision, body.fields)


@router.get("/applications")
def list_applications(request: Request,
                      limit: int = Query(default=50, ge=1, le=100),
                      cursor: str | None = Query(default=None, max_length=200),
                      assigned: bool = False,
                      project: str | None = Query(default=None, max_length=200)):
    identity = require_org_identity(request, scope="applications:read:self")
    if assigned:
        return service_requests.list_review_queue(identity, limit, cursor)
    return service_requests.list_mine(identity, limit, cursor, project)


@router.get("/applications/{request_id}")
def application_detail(request_id: str, request: Request):
    identity = require_org_identity(request, scope="applications:read:self")
    return service_requests.detail(identity, request_id)


@router.post("/applications/{request_id}/confirmations", status_code=201)
def confirmation(request_id: str, request: Request):
    identity = require_org_identity(request, scope="applications:create", web_only=True)
    return service_requests.issue_confirmation(identity, request_id)


@router.post("/applications", status_code=201)
def submit(body: SubmitBody, request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    identity = require_org_identity(request, scope="applications:create")
    return service_requests.submit(identity, body.draft_id, body.confirmation, idempotency_key)


@router.post("/applications/{request_id}/review")
def review(request_id: str, body: ReviewBody, request: Request):
    identity = require_org_identity(request, web_only=True)
    return service_requests.review(identity, request_id, body.decision, body.reason)


@router.post("/applications/{request_id}/withdraw")
def withdraw(request_id: str, request: Request):
    identity = require_org_identity(request, scope="applications:create", web_only=True)
    return service_requests.withdraw(identity, request_id)
