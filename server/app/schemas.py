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
    exam_mode: Literal["standard", "own_project", "free_creation"] = "standard"
    overwrite: bool = False
    # 图形验证码（M16）：CAPTCHA_ENABLED 开启时必验
    captcha_id: str = Field(default="", max_length=64)
    captcha_text: str = Field(default="", max_length=8)

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


class RegistrationCreate(BaseModel):
    """流程式注册第一步：仅采集学号/姓名/性别，其余字段留空待问卷补全。"""

    student_id: str
    name: str = Field(min_length=1, max_length=32)
    gender: Literal["male", "female"]
    # 图形验证码（M16）：CAPTCHA_ENABLED 开启时必验
    captcha_id: str = Field(default="", max_length=64)
    captcha_text: str = Field(default="", max_length=8)

    @field_validator("student_id")
    @classmethod
    def check_student_id(cls, v: str) -> str:
        if not re.fullmatch(config.STUDENT_ID_REGEX, v):
            raise ValueError(f"学号格式不正确（需匹配 {config.STUDENT_ID_REGEX}）")
        return v

    model_config = {"str_strip_whitespace": True}


class RegistrationOut(BaseModel):
    student_id: str
    campaign_id: str
    name: str
    gender: str
    registration_complete: int
    message: str


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
    exam_mode: str
    registration_complete: int
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
    repo_url: str | None = None
    cleaned_file_path: str | None = None
    attachment_file_path: str | None = None


class ApplicantMeOut(BaseModel):
    student_id: str
    name: str
    status: str
    exam_mode: str
    registration_complete: int
    has_session: bool
    track: str | None = None


class PublicQuestionOut(BaseModel):
    id: str
    domain: str
    track: str
    title: str
    body: str


class ProjectSubmitBody(BaseModel):
    repo_url: str

    model_config = {"str_strip_whitespace": True}


class AdminVerify(BaseModel):
    token: str


class ScoreIn(BaseModel):
    increment_score: float
    human_rate: float
    comment: str = ""
    scorer: str = ""

    model_config = {"str_strip_whitespace": True}


class ScoreOut(BaseModel):
    session_id: str
    increment_score: float
    human_rate: float
    comment: str
    scorer: str
    created_at: str
    updated_at: str


class ReviewSessionOut(BaseModel):
    session_id: str
    student_id: str
    student_name: str
    track: str
    uploaded_at: str | None
    repo_url: str | None
    stored_lan: bool = False
    increment_score: float | None = None
    human_rate: float | None = None
    scorer: str | None = None
    comment: str | None = None
    score_updated_at: str | None = None
    question_meta: list[dict] = Field(default_factory=list)
    slots: dict = Field(default_factory=dict)


class SlotWindowIn(BaseModel):
    start: str
    end: str


class SlotGenerateIn(BaseModel):
    date: str
    windows: list[SlotWindowIn] = Field(default_factory=list)
    slot_minutes: int
    note: str = ""

    model_config = {"str_strip_whitespace": True}


class AdminSlotOut(BaseModel):
    id: str
    start_at: str
    end_at: str
    note: str
    booked_by: str | None = None
    booked_at: str | None = None
    method: str | None = None
    student_name: str | None = None
    created_at: str


class CandidateSlotOut(BaseModel):
    id: str
    start_at: str
    end_at: str
    note: str
    booked_at: str | None = None
    method: str | None = None


class SlotBookIn(BaseModel):
    # 契约要求缺失/非法走 400 中文文案而非 pydantic 422，故此处宽松、路由内校验
    method: str | None = None


class CommentIn(BaseModel):
    comment: str = ""

    model_config = {"str_strip_whitespace": True}


class InterviewUpsert(BaseModel):
    m1: float = Field(ge=1, le=5, multiple_of=0.5)
    m2: float = Field(ge=1, le=5, multiple_of=0.5)
    m3: float = Field(ge=1, le=5, multiple_of=0.5)
    m4: float = Field(ge=1, le=5, multiple_of=0.5)
    m5: float = Field(ge=1, le=5, multiple_of=0.5)
    evidence: dict | None = None
    track_source: Literal["A", "B"] | None = None
    s_flag: bool | None = None
    inconsistency: str | None = None
    direct_channel: bool | None = None
    free_note: str | None = None
    level: str | None = None
    interviewer: str | None = None

    model_config = {"str_strip_whitespace": True}


class InterviewOut(BaseModel):
    campaign_id: str
    student_id: str
    m1: float
    m2: float
    m3: float
    m4: float
    m5: float
    evidence: dict = Field(default_factory=dict)
    track_source: str = "A"
    s_flag: int = 0
    inconsistency: str = ""
    direct_channel: int = 0
    free_note: str = ""
    level: str = ""
    weighted_total: float | None = None
    interviewer: str = ""
    created_at: str
    updated_at: str


def gender_label(gender: str) -> str:
    return GENDERS.get(gender, gender)


def focus_label(key: str) -> str:
    return FOCUS_NEXT_YEAR.get(key, key)


def domain_labels(keys: list[str]) -> list[str]:
    return [DOMAINS.get(k, k) for k in keys]
