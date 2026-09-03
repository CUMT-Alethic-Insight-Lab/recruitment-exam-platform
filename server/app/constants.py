DOMAINS = {
    "llm_arch": "语言/LLM（偏架构）",
    "cv_arch": "CV（偏架构）",
    "vlm_align": "VLM/MLLM（偏对齐）",
    "rl": "RL（大模型与传统决策）",
    "agent": "Agent 编排与框架",
    "memory": "记忆系统",
    "infra": "AI Infra",
    "aix": "AI+X 交付",
    "ai_safety": "AI 安全",
}
DOMAIN_KEYS = list(DOMAINS.keys())
MAX_DOMAINS = 3

EXAM_TRACKS = [
    {"key": "track1", "name": "研发型（理论/概念导向）"},
    {"key": "track2", "name": "实操型（落地实践）"},
    {"key": "track3", "name": "自由创作型（自选领域现场创作/已有项目增量）"},
]

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
