# 机试安装包下载分发鉴权方案（设计稿，未部署）

状态：草案 · 2026-10-16
范围：`AlethicInsight-ExamKit-2026-setup.exe`（约 331MB，Cloudflare R2，自定义域 `dl.alethicinsight.com`）的下载鉴权。

---

## 1. 现状摸底

### 1.1 kit-version.json 的实际结构

`server/static/kit-version.json`（由 FastAPI 静态挂载直接公开伺服，见 `server/tests/test_prefix.py:45`）：

```json
{
  "version": "2026.10.13-1",
  "url": "https://dl.alethicinsight.com/AlethicInsight-ExamKit-2026-setup.exe",
  "sha256": "8c2dea…bd8b",
  "notes": "…"
}
```

`url` 是 **R2 公开 bucket 自定义域的裸直链**，无任何鉴权参数。

### 1.2 URL 的两个消费方（都要改，缺一不可）

1. **官网入口页**：`server/static/index.html:766` —— 末页「机试入口」slide 里
   **硬编码**的 `<a class="entry-btn" href="https://dl.alethicinsight.com/AlethicInsight-ExamKit-2026-setup.exe">下载机试安装包</a>`。
   注意：它**不读** kit-version.json，URL 在两处各写一份。
2. **机试包 OTA**：`exam_kit/exam_server.py:1109` `check_kit_update()` 周期轮询
   `{cloud_base}/static/kit-version.json`（匿名 GET），发现新版本后由
   `download_kit_installer()`（`exam_kit/exam_server.py:1221`）用 curl 直接拉
   `info["url"]` 落盘并校验 sha256。此路径当前同样**不带任何凭证**。

### 1.3 「暂停下载」的现状实现

纯静态层面手工改 HTML：commit `b4e4ce6`（chore(static): 机试安装包暂停下载）
的做法是把入口页按钮置灰 + 文案改「机试包更新中 · 暂停下载」；恢复下载时改回
（`server/tests/test_m15_admin_path.py:49-50` 现断言暂停文案不存在、R2 直链存在）。
**R2 侧 URL 在暂停期间仍然裸奔**——知道链接的人随时可下，暂停只是藏了入口。

### 1.4 website-spa-proto

无机试包下载入口。全仓搜 `dl.alethicinsight / setup.exe / kit-version / 机试安装包`
均无命中；它只是主站原型，链到 `/recruitment` 主站。**无需改动**。

### 1.5 服务端可复用的既有件

- 考生鉴权：`server/app/auth.py:194` `require_applicant(request, cid)`（Bearer → 学号）；
  考生 token 前端存在 `localStorage`（`server/static/account.html:182`）。
- 学号维度内存限流范式：`server/app/routes/exam.py:425` `_check_p2p_key_rate`
  （10 次/小时/学号）。
- 审计：`server/app/auth.py:130` `write_audit(conn, actor, action, target, detail)`。
- env 配置范式：`server/app/config.py`（`os.environ.get(...)`，缺省安全值）。

---

## 2. 威胁模型与目标（先把话说清楚）

安装包内容本身**不是机密**——官网页脚明确写着「下载过慢可在 QQ 群文件中获取同一
安装包」，即官方本来就有第二个分发渠道。因此：

- **做不到的**：防止考生下载后自行转发 exe 文件本体。任何 URL 级方案都防不了这个，
  也不必防。
- **做得到的（本方案目标）**：
  1. R2 直链不再匿名可用——公开页面上不再存在可无限转发的裸链；
  2. 下载与「已报名学号」绑定，每次签发留审计（谁、何时、哪个批次）；
  3. 可按招新节奏开/关发放窗口（替代现在手工改 HTML 的「暂停」）；
  4. 泄漏的签名 URL 短时自然失效，失窃成本低。

约束（来自需求与实测）：

- 331MB 大文件**不能进 Worker 内存**——Worker 只做验签 + 流式转发/302，绝不 `arrayBuffer()`。
- 考生可能用 aria2 / IDM 等多线程下载器 → 签名方案必须对 **Range 请求友好**，
  且同一次下载的几十个并发分段共享同一条签名 URL。
- 校园网/宿舍网慢速与断点续传 → 有效期不能按「秒级一次性」设计。

---

## 3. 三条路评估

### (a) R2 转私有 + 平台登录换短时 HMAC 签名 URL（Worker 验签）——推荐

流程：考生登录平台（学号+密码，现有账号体系）→ 调 lab 签发端点拿
`https://dl.alethicinsight.com/<key>?exp=…&sig=HMAC(…)` → Worker 验签 →
从私有 bucket 流式回源（或 302 到 R2 预签名 URL，见 §5.3）。

- 安全性：bucket 私有后**唯一**公网入口就是 Worker，验签不过即 403；签名含过期时间，
  转发出去的链接短时作废。
- 大文件：Worker 端 `bucket.get(key, { range: request.headers })` 是**流式**的，
  不过 Worker 内存（R2→Worker→客户端在 CF 网内流穿，R2 出口零费用）；
  Range 透传天然支持 aria2/IDM 分段。
- 可追溯：签发端点按学号写审计 + 限流。
- 成本：一个 Worker（免费额度足够，请求数=下载次数）、一个共享 HMAC 密钥。

### (b) 保持 R2 公开 + URL 带限时 token 查询参数（Worker 校验）

**逻辑上不成立**：如果 bucket 保持公开，知道裸路径的人绕过 Worker 直接打 R2 公开域
（或 `r2.dev` 子域）即可，token 形同虚设；如果为了让它成立而把公开访问关掉、
所有流量走 Worker，那就**收敛成了方案 (a)**，只是 bucket 策略名存实亡。
唯一适用场景是「bucket 必须保持公开」的过渡态——而本场景没有此约束。不推荐。

### (c) 路径混淆 + Referer 校验——不够

- Referer 由客户端自报：curl/aria2/IDM **默认不发 Referer**（误伤合法多线程下载器），
  而转发者加 `-H "Referer: …"` 一行就绕过。攻防成本倒挂。
- 混淆路径即「不公开的 URL 即密码」：一旦被转发（QQ 群一句话的事）永久有效，
  无过期、无撤销、无审计。
- 它连 §2 的最低目标（可开关窗口、可追溯）都满足不了。仅可作为纵深防御的小佐料
  （对象 key 用不可枚举的随机段），不能当方案。

### 结论

**选 (a)**。它是唯一同时满足「真鉴权、可开关、可审计、大文件流式、Range 友好」的路，
且在 Cloudflare 生态内零新增基础设施费用。

---

## 4. 推荐方案详细设计

### 4.1 签名格式（lab 签发 / Worker 验签，共享一把密钥）

```
sig = hex( HMAC_SHA256(KIT_SIGNING_SECRET, "{key}\n{exp}") )
url = https://dl.alethicinsight.com/{key}?exp={exp}&sig={sig}
```

- `key` = R2 对象名，如 `AlethicInsight-ExamKit-2026-setup.exe`（签名绑定到具体对象，
  防止拿 A 文件的签名去下 B 文件）。
- `exp` = Unix 秒。Worker 侧：`now > exp` → 403；HMAC 比对用恒定时间比较。
- 密钥 `KIT_SIGNING_SECRET`：lab 环境变量 + Worker secret 各一份，256-bit 随机。

### 4.2 有效期与限流口径（招新场景）

- **签名 URL TTL：6 小时**（`KIT_URL_TTL_SECONDS=21600`，env 可调）。
  依据：331MB 在 200 KB/s 的差网络约 28 分钟；留一个数量级余量覆盖校园网晚高峰、
  下载器中断后隔几小时续传（aria2 `-c` 沿用原 URL）。6h 足够短，转发价值有限。
  **不建议**「一次性 token」：多线程下载器几十个 Range 连接共享一链，一次性语义
  必然误杀，且服务端要做状态存储，复杂度换不来安全增益。
- **签发限流：每学号 10 次/小时**（复用 `_check_p2p_key_rate` 范式）+ 每次签发写审计。
  过期后考生回平台重新换取即可，不构成使用障碍。
- **发放窗口开关：lab 侧 env `KIT_DOWNLOAD_OPEN=0/1`**。关闭时签发端点返回 403
  「当前批次未到下载开放时间」——替代现在手工改 HTML 的暂停方式，且 R2 侧同步失效
  （签不出新 URL，旧 URL 6h 内自然死透）。

### 4.3 端到端流程

```
考生浏览器                       lab (FastAPI)                 CF Worker (dl.*)        R2 (私有)
    │  ① 登录拿 Bearer（现有）        │                            │                     │
    │  ② POST /api/campaigns/{cid}/exam/kit-token                │                     │
    │ ───────────────────────────▶ │ 验证 require_applicant       │                     │
    │                              │ 限流 + 审计 + 窗口开关        │                     │
    │  ③ {url: "https://dl.…?exp&sig", sha256, size}             │                     │
    │ ◀─────────────────────────── │                            │                     │
    │  ④ 浏览器/aria2 请求签名 URL（可带 Range，可多连接）            │                     │
    │ ─────────────────────────────────────────────▶ │ 验签+验期 │                     │
    │                                              │ ⑤ bucket.get(key,{range}) 流式 ──▶ │
    │ ◀──────────────────── 200/206 流式回源 ◀─────────────────────────────────────── │
```

Worker 只验签和流式转发，**从不缓冲文件体**（不碰 `arrayBuffer()`）。

### 4.4 对两个消费方的改造

1. **官网入口页 `index.html:766`**：按钮不再硬编码 R2 URL，改为 JS 流程——
   已登录（localStorage 有考生 token）→ 调签发端点 → `window.location = 返回的 url`；
   未登录 → 跳 `__PREFIX__/login`。同时把 sha256 展示在按钮旁供下载器用户校验。
2. **exam_kit OTA**：`kit-version.json` 的 `url` 字段改为签发端点标记
   （如 `"url": "auth-required:kit-token"` 或新增 `url_endpoint` 字段），
   `download_kit_installer()` 先带 `Authorization: Bearer <state.auth_token>`
   调签发端点换真 URL 再 curl 下载；sha256 校验逻辑不变。
   **版本兼容**：旧包仍按裸 URL 下载——上线时保留旧字段或让 Worker 对旧 key
   短暂双跑一个宽限期，新版本包抬 `KIT_VERSION` 后自然切换（现有 OTA 机制正好干这个）。

---

## 5. 代码草稿

### 5.1 lab 签发端点草稿（`server/app/routes/exam.py` 追加，风格对齐现有代码）

```python
# ---- 机试安装包下载签发（R2 私有 + HMAC 签名 URL，Worker 验签）----
# config.py 新增：
#   KIT_SIGNING_SECRET = os.environ.get("KIT_SIGNING_SECRET", "")
#   KIT_DL_BASE = os.environ.get("KIT_DL_BASE",
#       "https://dl.alethicinsight.com").rstrip("/")
#   KIT_OBJECT_KEY = os.environ.get("KIT_OBJECT_KEY",
#       "AlethicInsight-ExamKit-2026-setup.exe")
#   KIT_URL_TTL_SECONDS = int(os.environ.get("KIT_URL_TTL_SECONDS", "21600"))  # 6h
#   KIT_DOWNLOAD_OPEN = os.environ.get("KIT_DOWNLOAD_OPEN", "1").strip().lower() \
#       not in ("0", "false", "no", "off")

import hashlib
import hmac as _hmac

KIT_TOKEN_LIMIT_PER_HOUR = 10
_kit_token_calls: dict[str, collections.deque] = {}


def _check_kit_token_rate(student_id: str) -> None:
    """与 _check_p2p_key_rate 同款的学号维度内存限流。"""
    now = time.monotonic()
    calls = _kit_token_calls.setdefault(student_id, collections.deque())
    while calls and calls[0] <= now - 3600:
        calls.popleft()
    if len(calls) >= KIT_TOKEN_LIMIT_PER_HOUR:
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    calls.append(now)


def sign_kit_url(key: str, exp: int) -> str:
    """与 CF Worker 共享的签名格式：hex(HMAC_SHA256(secret, "{key}\n{exp}"))。"""
    msg = f"{key}\n{exp}".encode()
    sig = _hmac.new(config.KIT_SIGNING_SECRET.encode(), msg,
                    hashlib.sha256).hexdigest()
    return f"{config.KIT_DL_BASE}/{key}?exp={exp}&sig={sig}"


@router.post("/{cid}/exam/kit-token")
def exam_kit_token(cid: str, student_id: str = Depends(require_applicant)):
    """考生换取机试安装包的短时签名下载 URL（发放窗口外 403，不下发任何地址）。"""
    if not config.KIT_DOWNLOAD_OPEN:
        raise HTTPException(status_code=403,
                            detail="当前不在安装包下载开放时段，请关注招新群通知")
    if not config.KIT_SIGNING_SECRET:
        raise HTTPException(status_code=503, detail="kit download unavailable")
    with db.connection() as conn:
        get_campaign_or_404(conn, cid)
        app = conn.execute(
            "SELECT 1 FROM application WHERE campaign_id = ? AND student_id = ?",
            (cid, student_id)).fetchone()
        if app is None:
            raise HTTPException(status_code=404, detail="未找到该学号的报名记录")
    _check_kit_token_rate(student_id)
    exp = int(time.time()) + config.KIT_URL_TTL_SECONDS
    with db.connection() as conn:
        auth.write_audit(conn, student_id, "exam_kit_token", target=cid,
                         detail=f"kit url signed, exp={exp}")
    return {"url": sign_kit_url(config.KIT_OBJECT_KEY, exp),
            "expires_at": exp,
            "ttl_seconds": config.KIT_URL_TTL_SECONDS}
```

配套测试（`server/tests/` 新增，沿用现有 client fixture）：未登录 401 /
未报名 404 / 窗口关闭 403 / 限流第 11 次 429 / 签名字符串重放验算一致 /
审计行落库。

### 5.2 Cloudflare Worker 草稿（dl.alethicinsight.com 路由，流式回源）

```js
// wrangler.toml 关键配置：
//   name = "kit-dl-gate"
//   route = "dl.alethicinsight.com/*"
//   [[r2_buckets]] binding = "KIT_BUCKET", bucket_name = "<私有 bucket>"
//   secrets: KIT_SIGNING_SECRET（与 lab 环境变量同值）

const te = new TextEncoder();

async function hmacHex(secret, msg) {
  const key = await crypto.subtle.importKey(
    "raw", te.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const buf = await crypto.subtle.sign("HMAC", key, te.encode(msg));
  return [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, "0")).join("");
}

// 恒定时间比较，防时序侧信道
function timingSafeEqual(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const key = decodeURIComponent(url.pathname.slice(1));
    if (!key || key.includes("..")) {
      return new Response("Not Found", { status: 404 });
    }
    const exp = Number(url.searchParams.get("exp") || 0);
    const sig = (url.searchParams.get("sig") || "").toLowerCase();
    if (!exp || !/^[0-9a-f]{64}$/.test(sig) || Date.now() / 1000 > exp) {
      return new Response("下载链接已过期或未授权，请回招新平台重新获取。",
                          { status: 403 });
    }
    const want = await hmacHex(env.KIT_SIGNING_SECRET, `${key}\n${exp}`);
    if (!timingSafeEqual(want, sig)) {
      return new Response("下载链接已过期或未授权，请回招新平台重新获取。",
                          { status: 403 });
    }
    // 流式回源私有 bucket；Range 头透传 → aria2/IDM 分段下载天然可用。
    // 绝不 arrayBuffer()：body 是流，331MB 不进 Worker 内存。
    const object = await env.KIT_BUCKET.get(key, { range: request.headers });
    if (object === null) return new Response("Not Found", { status: 404 });
    const headers = new Headers();
    object.writeHttpMetadata(headers);
    headers.set("etag", object.httpEtag);
    headers.set("accept-ranges", "bytes");
    headers.set("cache-control", "private, no-store");  // 签名 URL 不进共享缓存
    headers.set("content-disposition",
                `attachment; filename="${key.split("/").pop()}"`);
    let status = 200;
    if (object.range) {
      // R2Range: {offset, length} 或 {suffix}
      const offset = object.range.offset ?? Math.max(0, object.size - object.range.suffix);
      const length = object.range.length ?? object.range.suffix;
      headers.set("content-range", `bytes ${offset}-${offset + length - 1}/${object.size}`);
      status = 206;
    }
    return new Response(object.body, { status, headers });
  },
};
```

### 5.3 Worker 的替代变体：302 到 R2 预签名 URL（零流量中继）

若要求 Worker **连流式中继都不做**（只做验签 + 302），在 Worker 内用
[`aws4fetch`](https://github.com/mhart/aws4fetch) 对 R2 S3 端点预签名后 302：

```js
import { AwsClient } from "aws4fetch";
// secrets 增加：R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY / R2_ACCOUNT_ID
const r2 = new AwsClient({
  accessKeyId: env.R2_ACCESS_KEY_ID,
  secretAccessKey: env.R2_SECRET_ACCESS_KEY,
  service: "s3",
  region: "auto",
});
// 验签通过后：
const remaining = Math.max(60, exp - Math.floor(Date.now() / 1000));
const signed = await r2.sign(
  new Request(`https://${env.R2_ACCOUNT_ID}.r2.cloudflarestorage.com/<bucket>/${key}`
              + `?X-Amz-Expires=${remaining}`),
  { aws: { signQuery: true } });
return Response.redirect(signed.url, 302);
```

权衡：

| 维度 | 5.2 流式回源（推荐） | 5.3 302 + 预签名 |
|---|---|---|
| 流量是否过 Worker | 网内流穿（不缓冲、R2 出口零费用、不占 CPU 时长） | 完全不过 |
| 额外凭据 | 只要一把 HMAC 密钥 | 还要 R2 S3 Access Key 对 |
| 对外暴露的 URL | 始终是 `dl.alethicinsight.com` | 302 后露出 `*.r2.cloudflarestorage.com` 与 account id |
| Range / 多线程 | `range: request.headers` 透传，语义最干净 | 预签名不覆盖 Range 头，aria2/IDM 可用 |
| 实现复杂度 | 无三方依赖 | 引入 aws4fetch |

两者安全性等价（都只有验签通过才拿得到数据）。默认建议 5.2：依赖最少、
域名不露底、Range 语义最直白；「Worker 不中转流量」若被上升为硬约束再切 5.3。

### 5.4 exam_kit OTA 适配草稿（`exam_kit/exam_server.py`）

```python
def _resolve_kit_download_url(cfg: dict, info: dict) -> str:
    """kit-version.json 的 url 为 "auth-required:kit-token" 时，
    带考生 Bearer 调签发端点换短时签名 URL；裸 http(s) 直链保持旧行为。"""
    url = str(info.get("url") or "").strip()
    if url.startswith(("http://", "https://")):
        return url  # 旧格式：公开直链（过渡期兼容）
    if url == "auth-required:kit-token":
        token = str(load_state().get("auth_token") or "")
        if not token:
            raise CloudError("请先登录后再下载新版安装包。", friendly=True)
        _, data = cloud_request(
            cfg, "POST",
            f"/api/campaigns/{cfg['campaign_id']}/exam/kit-token", bearer=token)
        signed = str(data.get("url") or "").strip()
        if signed.startswith(("http://", "https://")):
            return signed
    raise CloudError("新版安装包的下载地址缺失或不正确，请联系组织者处理。",
                     friendly=True)
# download_kit_installer() 首行改为：url = _resolve_kit_download_url(cfg, info)
# 403（窗口关闭/签名过期）由 _parse_cloud_error 透传出后端白话 detail。
```

---

## 6. 落地步骤清单（本次不执行，仅备查）

1. **R2 侧**：bucket 关闭公开访问（保留对象与新自定义域解析）；创建 Worker
   `kit-dl-gate`，绑定 `KIT_BUCKET`、路由 `dl.alethicinsight.com/*`、写入
   secret `KIT_SIGNING_SECRET`。
2. **lab 侧**：`config.py` 加 5 个 env；`routes/exam.py` 加签发端点（§5.1）；
   补 `server/tests/` 测试；部署环境写入同值 `KIT_SIGNING_SECRET` 与
   `KIT_DOWNLOAD_OPEN=0`（保持当前暂停语义上线，灰度后再开）。
3. **前端**：`index.html:766` 按钮改 JS 换取流程（未登录跳 login，已登录换签名 URL
   后跳转，按钮旁展示 sha256）；`kit-version.json` 的 `url` 切换为
   `auth-required:kit-token` 的时机与新安装包发版同步（旧包宽限期内 Worker 可
   临时放行旧裸链或双字段并行）。
4. **exam_kit**：加 `_resolve_kit_download_url`（§5.4），抬 `KIT_VERSION` 与
   `kit-version.json`，补 `test_exam_server.py` 用例。
5. **验证清单**：
   - 未带/带错/过期 `sig` 请求 → 403；正确签名 → 200 且 sha256 与清单一致；
   - `curl -H "Range: bytes=0-1048575"` → 206 + 正确 Content-Range；
   - `aria2c -x16 -c <签名URL>` 全程下载 + 中途 kill 续传，sha256 校验一致；
   - Worker 日志确认响应为流式（无内存告警）、签发审计行按学号落库；
   - `KIT_DOWNLOAD_OPEN=0` 时签发 403、已发出的 URL 到期后 R2 彻底不可匿名达。
6. **回收口径**：招新收口（数据按 11 月 15 日口径回收）后 `KIT_DOWNLOAD_OPEN=0`
   即可，无需再改静态页。

## 7. 明确不做的事

- 不追踪/水印 exe 本体（官方 QQ 群渠道存在，投入产出比为零）；
- 不做一次性 token（与多线程下载器根本冲突，见 §4.2）；
- 不动 `kit-version.json` 的公开可读性（version/notes/sha256 非敏感，
  exam_kit 匿名轮询依赖它）。
