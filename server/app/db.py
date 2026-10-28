import sqlite3
from contextlib import contextmanager

from . import config

# 独立、只由受信名单维护；允许名单先于账号存在，不随报名批次回收。
MEMBERSHIP_SCHEMA = """
CREATE TABLE IF NOT EXISTS membership (
    student_id TEXT PRIMARY KEY NOT NULL,
    name       TEXT NOT NULL,
    source     TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

SCHEMA = MEMBERSHIP_SCHEMA + """
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
    pw_salt         TEXT,
    pw_hash         TEXT,
    pw_changed      INTEGER NOT NULL DEFAULT 0,
    exam_mode       TEXT NOT NULL DEFAULT 'standard',
    registration_complete INTEGER NOT NULL DEFAULT 1,
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
    bank_id     TEXT,
    role        TEXT NOT NULL DEFAULT '',
    meta        TEXT NOT NULL DEFAULT '{}',
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
    repo_url         TEXT,
    attachment_file_path TEXT,
    slots            TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (campaign_id, student_id)
        REFERENCES application (campaign_id, student_id)
);

CREATE INDEX IF NOT EXISTS idx_exam_session_app
    ON exam_session (campaign_id, student_id);

CREATE TABLE IF NOT EXISTS exam_score (
    session_id      TEXT PRIMARY KEY REFERENCES exam_session(id),
    increment_score REAL,
    human_rate      REAL,
    comment         TEXT NOT NULL DEFAULT '',
    scorer          TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS interview_score (
    campaign_id   TEXT NOT NULL,
    student_id    TEXT NOT NULL,
    m1            REAL,
    m2            REAL,
    m3            REAL,
    m4            REAL,
    m5            REAL,
    evidence      TEXT NOT NULL DEFAULT '{}',
    track_source  TEXT NOT NULL DEFAULT 'A',
    s_flag        INTEGER NOT NULL DEFAULT 0,
    inconsistency TEXT NOT NULL DEFAULT '',
    direct_channel INTEGER NOT NULL DEFAULT 0,
    free_note     TEXT NOT NULL DEFAULT '',
    level         TEXT NOT NULL DEFAULT '',
    weighted_total REAL,
    interviewer   TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (campaign_id, student_id),
    FOREIGN KEY (campaign_id, student_id)
        REFERENCES application (campaign_id, student_id)
);

CREATE TABLE IF NOT EXISTS admin_user (
    username    TEXT PRIMARY KEY,
    pw_salt     TEXT,
    pw_hash     TEXT,
    pw_changed  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS account (
    student_id  TEXT PRIMARY KEY,
    role        TEXT NOT NULL DEFAULT 'applicant',
    nickname    TEXT NOT NULL DEFAULT '',
    profile     TEXT NOT NULL DEFAULT '{}',
    avatar_ext  TEXT,
    pw_salt     TEXT,
    pw_hash     TEXT,
    pw_changed  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS login_session (
    token_hash  TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    campaign_id TEXT,
    student_id  TEXT,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS login_attempt (
    key          TEXT PRIMARY KEY,
    fails        INTEGER NOT NULL DEFAULT 0,
    window_start TEXT,
    locked_until TEXT
);

CREATE TABLE IF NOT EXISTS captcha (
    id          TEXT PRIMARY KEY,
    code        TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    used        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS interview_slot (
    id          TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL,
    start_at    TEXT NOT NULL,
    end_at      TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    booked_by   TEXT,
    booked_at   TEXT,
    method      TEXT,
    created_at  TEXT NOT NULL,
    UNIQUE (campaign_id, booked_by)
);

CREATE INDEX IF NOT EXISTS idx_interview_slot_campaign_start
    ON interview_slot (campaign_id, start_at);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    target      TEXT NOT NULL DEFAULT '',
    detail      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);
"""

DEFAULT_EXAM_CONFIG = {
    "track1_per_domain": 1,
    "track2_per_domain": 1,
    "reroll_limit": 2,
    "track3_guide_text": (
        "自由创作型：请选择一个领域（或结合你的已有项目），"
        "现场创作或为已有项目增加实质功能/内容，产出过程将通过 Kimi Code 会话轨迹评估。"
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
    session_cols = {r["name"] for r in conn.execute("PRAGMA table_info(exam_session)")}
    if "repo_url" not in session_cols:
        conn.execute("ALTER TABLE exam_session ADD COLUMN repo_url TEXT")
    if "attachment_file_path" not in session_cols:
        conn.execute("ALTER TABLE exam_session ADD COLUMN attachment_file_path TEXT")
    if "slots" not in session_cols:
        conn.execute(
            "ALTER TABLE exam_session ADD COLUMN slots TEXT NOT NULL DEFAULT '{}'")
    app_cols = {r["name"] for r in conn.execute("PRAGMA table_info(application)")}
    if "pw_salt" not in app_cols:
        conn.execute("ALTER TABLE application ADD COLUMN pw_salt TEXT")
    if "pw_hash" not in app_cols:
        conn.execute("ALTER TABLE application ADD COLUMN pw_hash TEXT")
    if "pw_changed" not in app_cols:
        conn.execute(
            "ALTER TABLE application ADD COLUMN pw_changed INTEGER NOT NULL DEFAULT 0"
        )
    q_cols = {r["name"] for r in conn.execute("PRAGMA table_info(question)")}
    if "bank_id" not in q_cols:
        conn.execute("ALTER TABLE question ADD COLUMN bank_id TEXT")
    if "role" not in q_cols:
        conn.execute("ALTER TABLE question ADD COLUMN role TEXT NOT NULL DEFAULT ''")
    if "meta" not in q_cols:
        conn.execute("ALTER TABLE question ADD COLUMN meta TEXT NOT NULL DEFAULT '{}'")
    if "exam_mode" not in app_cols:
        conn.execute(
            "ALTER TABLE application ADD COLUMN exam_mode TEXT NOT NULL DEFAULT 'standard'"
        )
    if "registration_complete" not in app_cols:
        conn.execute(
            "ALTER TABLE application ADD COLUMN registration_complete INTEGER NOT NULL DEFAULT 1"
        )
    slot_cols = {r["name"] for r in conn.execute("PRAGMA table_info(interview_slot)")}
    # 面试方式 method（'online' 线上 / 'offline' 线下，未预约为 NULL）：
    # 旧库该字段原名列 booked_mode，重命名收编；未约槽位无存量需回填
    if "method" not in slot_cols:
        if "booked_mode" in slot_cols:
            conn.execute("ALTER TABLE interview_slot RENAME COLUMN booked_mode TO method")
        else:
            conn.execute("ALTER TABLE interview_slot ADD COLUMN method TEXT")
    # 同批次同起点仅允许一个槽：先清历史重复行（保留最早一行），再补唯一索引，
    # 让 generate 并发重放 / 重复插入在数据库层被兜底拦截
    conn.execute(
        "DELETE FROM interview_slot WHERE rowid NOT IN"
        " (SELECT MIN(rowid) FROM interview_slot GROUP BY campaign_id, start_at)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_interview_slot_campaign_start"
        " ON interview_slot (campaign_id, start_at)"
    )
    acct_cols = {r["name"] for r in conn.execute("PRAGMA table_info(account)")}
    if "profile" not in acct_cols:
        conn.execute("ALTER TABLE account ADD COLUMN profile TEXT NOT NULL DEFAULT '{}'")
    if "real_name" not in acct_cols:
        conn.execute("ALTER TABLE account ADD COLUMN real_name TEXT NOT NULL DEFAULT ''")
    if "profile_completed" not in acct_cols:
        conn.execute("ALTER TABLE account ADD COLUMN profile_completed INTEGER NOT NULL DEFAULT 0")
    member_cols = {r["name"] for r in conn.execute("PRAGMA table_info(membership)")}
    if "club_position" not in member_cols:
        conn.execute("ALTER TABLE membership ADD COLUMN club_position TEXT NOT NULL DEFAULT ''")
    _migrate_account(conn)


def _migrate_account(conn: sqlite3.Connection) -> None:
    """account 表为账号唯一可信源：首次迁移时从 application/admin_user 灌入，
    之后仅做幂等考生补种（admin_user 已退役，不再覆盖，避免回滚用户改过的密码）。"""
    count = conn.execute("SELECT COUNT(*) c FROM account").fetchone()["c"]
    # 考生播种：nickname 默认 = 姓名，凭据沿用 application.pw_*
    conn.execute(
        "INSERT OR IGNORE INTO account"
        " (student_id, role, nickname, avatar_ext, pw_salt, pw_hash, pw_changed,"
        "  created_at, updated_at)"
        " SELECT student_id, 'applicant', name, NULL, pw_salt, pw_hash, pw_changed,"
        "        created_at, updated_at"
        " FROM application"
    )
    if count == 0:
        # 管理员播种：学号同号时管理员凭据优先（覆盖考生行）
        conn.execute(
            "INSERT OR REPLACE INTO account"
            " (student_id, role, nickname, pw_salt, pw_hash, pw_changed,"
            "  created_at, updated_at)"
            " SELECT username, 'admin', '', pw_salt, pw_hash, pw_changed,"
            "        created_at, created_at"
            " FROM admin_user"
        )
