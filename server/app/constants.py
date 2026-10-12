DOMAINS = {
    "llm_arch": "语言/LLM（偏架构）",
    "cv_arch": "CV（偏架构）",
    "vlm_align": "VLM/MLLM（偏对齐）",
    "rl": "RL（大模型与传统决策）",
    "agent": "Agent 编排与框架",
    "memory": "记忆系统",
    "infra": "AI Infra",
    "ai_safety": "AI 安全",
}
DOMAIN_KEYS = list(DOMAINS.keys())
MAX_DOMAINS = 3

EXAM_TRACKS = [
    {"key": "track1", "name": "研发型（理论/概念导向）"},
    {"key": "track2", "name": "实操型（落地实践）"},
    {"key": "track3", "name": "自由创作型（自选领域现场创作/已有项目增量）"},
]

EXAM_MODES = {
    "standard": "标准机试",
    "own_project": "个人项目（GitHub 仓库直提）",
    "free_creation": "领域内自由发挥（会话记录 + GitHub 链接）",
}

# exam_session.track='project' 的展示名（个人项目轨直提，不经 enroll 开考）
PROJECT_TRACK_NAME = "个人项目（GitHub 仓库）"

# 交卷/自有项目附件（.zip）大小上限
MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024

GENDERS = {"male": "男", "female": "女"}

FOCUS_NEXT_YEAR = {
    "grades": "绩点学业",
    "student_work": "学生工作",
    "research": "科研",
    "project": "项目经验",
}

EXAM_SESSION_STATUS = ["draft", "uploaded", "voided"]

APPLICATION_STATUS = [
    "applied",
    "tested",
    "shortlisted",
    "interviewed",
    "admitted",
    "rejected",
]

CAMPAIGN_STATUS = ["open", "closed"]
