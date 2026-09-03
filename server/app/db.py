import sqlite3
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaign (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    year        INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',
    expires_at  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    exam_config TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS application (
    student_id      TEXT NOT NULL,
    campaign_id     TEXT NOT NULL,
    name            TEXT NOT NULL,
    gender          TEXT NOT NULL,
    class_name      TEXT NOT NULL,
    qq              TEXT NOT NULL,
    lab_intent      INTEGER NOT NULL,
    domains         TEXT NOT NULL,
    focus_next_year TEXT NOT NULL,
    expectation     TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'applied',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (campaign_id, student_id)
);

CREATE INDEX IF NOT EXISTS idx_application_campaign ON application (campaign_id);

CREATE TABLE IF NOT EXISTS question (
    id          TEXT PRIMARY KEY,
    domain      TEXT NOT NULL,
    track       TEXT NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    tags        TEXT NOT NULL DEFAULT '[]',
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_question_domain_track
    ON question (domain, track, active);

CREATE TABLE IF NOT EXISTS exam_session (
    id               TEXT PRIMARY KEY,
    campaign_id      TEXT NOT NULL,
    student_id       TEXT NOT NULL,
    session_token    TEXT NOT NULL UNIQUE,
    machine_id       TEXT NOT NULL,
    track            TEXT NOT NULL,
    domain_tags      TEXT NOT NULL DEFAULT '[]',
    questions_served TEXT NOT NULL DEFAULT '[]',
    rerolls          INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL DEFAULT 'draft',
    opened_at        TEXT NOT NULL,
    uploaded_at      TEXT,
    cleaned_file_path TEXT,
    FOREIGN KEY (campaign_id, student_id)
        REFERENCES application (campaign_id, student_id)
);

CREATE INDEX IF NOT EXISTS idx_exam_session_app
    ON exam_session (campaign_id, student_id);
"""

DEFAULT_EXAM_CONFIG = {
    "track1_per_domain": 2,
    "track2_per_domain": 2,
    "reroll_limit": 2,
    "track3_guide_text": (
        "自由创作型：请选择一个领域（或结合你的已有项目），"
        "现场创作或为已有项目增加实质功能/内容，产出过程将通过 opencode 会话轨迹评估。"
    ),
}


@contextmanager
def connection():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connection() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(campaign)")}
    if "exam_config" not in cols:
        conn.execute("ALTER TABLE campaign ADD COLUMN exam_config TEXT NOT NULL DEFAULT '{}'")
