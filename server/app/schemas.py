import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from . import config
from .constants import DOMAIN_KEYS, DOMAINS, FOCUS_NEXT_YEAR, GENDERS, MAX_DOMAINS


class CampaignCreate(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,32}$")
    name: str = Field(min_length=1, max_length=64)
    year: int = Field(ge=2000, le=2100)


class CampaignOut(BaseModel):
    id: str
    name: str
    year: int
    status: str
    expires_at: str
    created_at: str


class CampaignPatch(BaseModel):
    name: str | None = None
    status: Literal["open", "closed"] | None = None
    exam_config: dict | None = None


class ApplicationCreate(BaseModel):
    student_id: str
    name: str = Field(min_length=1, max_length=32)
    gender: Literal["male", "female"]
    class_name: str = Field(min_length=1, max_length=64)
    qq: str = Field(min_length=5, max_length=12)
    lab_intent: bool
    domains: list[str] = Field(min_length=1, max_length=MAX_DOMAINS)
    focus_next_year: Literal["grades", "student_work", "research", "project"]
    expectation: str = Field(default="", max_length=1000)
    overwrite: bool = False

    @field_validator("student_id")
    @classmethod
    def check_student_id(cls, v: str) -> str:
        if not re.fullmatch(config.STUDENT_ID_REGEX, v):
            raise ValueError(f"学号格式不正确（需匹配 {config.STUDENT_ID_REGEX}）")
        return v

    @field_validator("qq")
    @classmethod
    def check_qq(cls, v: str) -> str:
        if not re.fullmatch(r"\d{5,11}", v):
            raise ValueError("QQ 号需为 5-11 位数字")
        return v

    @field_validator("domains")
    @classmethod
    def check_domains(cls, v: list[str]) -> list[str]:
        invalid = [d for d in v if d not in DOMAIN_KEYS]
        if invalid:
            raise ValueError(f"无效领域: {invalid}")
        if len(set(v)) != len(v):
            raise ValueError("领域不可重复")
        return v

    model_config = {"str_strip_whitespace": True}


class ApplicationUpdate(ApplicationCreate):
    pass


class ApplicationOut(BaseModel):
    student_id: str
    campaign_id: str
    name: str
    gender: str
    class_name: str
    qq: str
    lab_intent: bool
    domains: list[str]
    focus_next_year: str
    expectation: str
    status: str
    created_at: str
    updated_at: str


class QuestionCreate(BaseModel):
    domain: str
    track: str
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=8000)
    tags: list[str] = Field(default_factory=list)
    active: bool = True

    model_config = {"str_strip_whitespace": True}


class QuestionPatch(BaseModel):
    domain: str | None = None
    track: str | None = None
    title: str | None = None
    body: str | None = None
    tags: list[str] | None = None
    active: bool | None = None


class QuestionOut(BaseModel):
    id: str
    domain: str
    track: str
    title: str
    body: str
    tags: list[str]
    active: bool
    created_at: str
    updated_at: str


class ExamEnroll(BaseModel):
    student_id: str
    track: str


class ExamReroll(BaseModel):
    session_id: str


class ExamVoid(BaseModel):
    student_id: str


class ExamSessionOut(BaseModel):
    id: str
    campaign_id: str
    student_id: str
    machine_id: str
    track: str
    domain_tags: list[str]
    questions_served: list[dict]
    rerolls: int
    status: str
    opened_at: str
    uploaded_at: str | None
    student_name: str | None = None


class AdminVerify(BaseModel):
    token: str


def gender_label(gender: str) -> str:
    return GENDERS.get(gender, gender)


def focus_label(key: str) -> str:
    return FOCUS_NEXT_YEAR.get(key, key)


def domain_labels(keys: list[str]) -> list[str]:
    return [DOMAINS.get(k, k) for k in keys]
