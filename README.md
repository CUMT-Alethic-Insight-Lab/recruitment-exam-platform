# Alethic Insight 招新机试平台

中国矿业大学 **Alethic Insight** 实验室招新使用的在线机试平台：考生在**自己的电脑**上用
AI 工具（Kimi Code 或 opencode）作答，本地考试端在交卷时把 AI 会话**清洗后**上传，
评审在网页端按清洗后的轨迹打分。

> **对考生的话**：会话清洗代码（`exam_kit/cleaner.py` 及其测试）就在这个公开仓库里，
> 欢迎审查——我们承诺只上传清洗后的会话记录（去除思考过程与工具输出正文），
> 不读取、不上传你电脑里的其他文件。清洗规则有测试覆盖，发现问题欢迎提 Issue。

## 架构

```
考生电脑（exam_kit，纯 Python 标准库，无三方依赖）
  ├─ exam_server.py    本地机试服务（发题/换题/交卷，127.0.0.1:8899）
  ├─ cleaner.py        会话清洗：导出 AI 会话 → 去除思考/工具输出 → 生成评审用 md
  └─ session_receiver.py  实验室局域网接收机（upload_mode=lan 时收清洗件）
        │  交卷：HTTPS 上传（默认）/ Tailscale P2P 直连 / 局域网接收机
        ▼
云端 VPS（server，FastAPI + SQLite，ROLE=edge）
  ├─ 招新主站：报名 / 账号 / 问卷 / 面试评审 / 题库抽题
  └─ 交卷暂存转发：multipart 暂存 → 定时转发 lab 收口（云端不留数据）
        ▼
实验室小主机（lab，ROLE=authoritative）
  └─ 权威库 + ingest-staged 收口 + exam-archive 即时归档
```

## 目录导览

- `server/` — 招新平台后端与页面（FastAPI + SQLite + 原生 HTML/JS）
  - `app/` 应用代码；`tests/` pytest 套件；`scripts/import_question_bank.py` 题库导入器
  - `deploy/` 部署脚本（参数化，无硬编码主机/密钥）、systemd 单元、nginx 片段
- `exam_kit/` — 考生端考试系统（纯标准库）与安装器
  - `cleaner.py` 会话清洗（**公开供考生审查**）；`installer/` 安装器打包
- `docs/platform_flow.md` — 平台流程说明

## 快速运行

```bash
# 服务端
cd server
pip install -r requirements.txt
python -m pytest tests -q            # 220 项测试
uvicorn app.main:app --port 8901     # 本地启动（开发默认 dev-secret-key）

# 考生端
cd exam_kit
python -m pytest -q                  # 271 项测试
python exam_server.py                # 本地机试服务（127.0.0.1:8899）
```

## 私有仓库（org 内部）

以下内容不随本仓公开，维护在 org 私有仓：

- **question-bank** — 真实机试题库与解析校验（本仓 `server/tests/fixtures/` 仅有合成夹具）
- **infra-handoff** — 真实主机地址、账号、密钥路径与运维排障记录
- **exam-archive** — 考生活档（清洗件 md / 仓库链接 / 附件）

## 许可与归属

CUMT Alethic Insight Lab。页面中的社群二维码与招新素材版权归实验室所有。
