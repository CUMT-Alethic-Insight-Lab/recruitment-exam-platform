# 12 · 官方参考与兼容性注意

> 查阅日：2026-10-27。技术标准可能继续迭代，上线前再次按实现所选版本核对，**不要将本节当作已部署证据**。

## 组织公开资料

- 官网：https://www.alethicinsight.org/
- open-onboarding：https://github.com/CUMT-Alethic-Insight-Lab/open-onboarding
- awesome-ai：https://github.com/CUMT-Alethic-Insight-Lab/awesome-ai
- governance-public：请在组织 GitHub 核验实际仓库和最新正式制度，不从本设计稿生成新政策。

公开 `open-onboarding` 已强调不得保存真实招新问题、面试评估、凭据及内部笔记。此系统若需要内部资料，应采用私有业务存储和授权接口，而非把内部数据混进公开学习文档仓。

## OAuth / 本地 CLI

- OAuth 2.0 Security Best Current Practice（RFC 9700）：https://www.rfc-editor.org/rfc/rfc9700.html
- OAuth for Native Apps（RFC 8252）：https://www.rfc-editor.org/rfc/rfc8252.html
- Proof Key for Code Exchange（RFC 7636）：https://www.rfc-editor.org/rfc/rfc7636.html
- Device Authorization Grant（RFC 8628）：https://www.rfc-editor.org/rfc/rfc8628.html
- Browser-Based Applications BCP（RFC 10017）：https://www.rfc-editor.org/rfc/rfc10017.html

## MCP

- MCP 官方规范主页：https://modelcontextprotocol.io/specification
- 2025-11-25 授权规范（用于理解本地 stdio vs HTTP 授权区别）：https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization
- 2025-11-25 传输规范：https://modelcontextprotocol.io/specification/2025-11-25/basic/transports

2026-10-27 规范主页已指向 `2026-07-28`；该版本采用更现代的无状态请求/能力协商模式，SDK 与宿主应用兼容性可能不一致。因此推荐 CLI + 本地 stdio MCP 先行，远程 MCP 单独做客户端兼容矩阵与协议升级验证。

## Gitea

- 仓库与镜像文档：https://docs.gitea.com/1.26/usage/repository/repo-mirror/

镜像是代码 Git 历史的单向同步，不可假定完整同步 Issues、PR、仓库权限。推送镜像可能覆盖远端 refs，私有→公开发布必须另设人工审查。

## 参考来源的使用原则

- 规范和资料只对具体技术判断负责，不代表组织已经承诺采用某方案。
- 同一建议如果依赖既有官网实际配置，需以代码与现场检测为准；没有访问权限时在 PR 写 UNKNOWN。
- 决策待定的表单/字段/成员权利不要借用示例内容直接上线。
