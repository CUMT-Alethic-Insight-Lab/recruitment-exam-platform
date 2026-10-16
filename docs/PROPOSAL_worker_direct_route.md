# 方案：用 Cloudflare Worker 把 /recruitment/* 从腾讯云 VPS 砍成直连 lab-origin

> 状态：草稿，未部署。本文只出方案 + Worker 草稿，不改动任何服务器配置。

## 0. 背景与动机

现有链路（每请求 2 次 CF 边缘跳转 + 1 次 VPS 中转）：

```
用户 → CF 边缘(alethicinsight.com) → CF Tunnel → 腾讯云 VPS nginx
     → CF 边缘(lab-origin.alethicinsight.com, CF Access) → cloudflared → 实验室小主机 uvicorn:8901
```

目标链路（砍掉 VPS nginx 一跳）：

```
用户 → CF 边缘(alethicinsight.com, Worker)
     → CF 边缘(lab-origin.alethicinsight.com, CF Access) → cloudflared → uvicorn:8901
```

Worker 到 lab-origin 的 fetch 全程在 Cloudflare 骨干网内部完成，省掉「CF 边缘 → 腾讯
云 → CF 边缘」这段公网/跨境往返和 VPS 上的一次 TLS 握手 + 隧道回源。冷连接 ~2s+
的主要可压缩部分就是这一段。

## 1. 摸底结论（2026-10-16，VPS 只读侦察）

### 1.1 nginx 现有路径映射（`/etc/nginx/sites-available/alethicinsight`）

| 入口路径 | 行为 | 转发目标 |
|---|---|---|
| `= /recruitment` | 301 → `/recruitment/` | — |
| `^~ /recruitment/*` | strip 前缀反代 | `https://lab-origin.alethicinsight.com/*` |
| `= /recruitment/admin` | 301 → `/admin/recruitment` | — |
| `= /admin/recruitment/` | 301 → `/admin/recruitment` | — |
| `= /admin/recruitment` | 精确反代 | `https://lab-origin.alethicinsight.com/admin` |

反代细节（Worker 必须等价实现）：

- `Host` 固定为 `lab-origin.alethicinsight.com`（否则 CF 按 Host 路由回 VPS 隧道
  造成环路）；SNI 显式指定同名（`proxy_ssl_server_name` + `proxy_ssl_name`）。
  **Worker 里不存在此问题**：`fetch("https://lab-origin.alethicinsight.com/...")`
  天然带上正确 Host/SNI。
- 携带 CF Access service token：`CF-Access-Client-Id` / `CF-Access-Client-Secret`
  （明文存于 `/etc/nginx/snippets/cf-access-lab-origin.conf`）。**Worker 方案里
  改存 wrangler secret，不硬编码进代码**；建议迁移时顺便轮换该 token（它目前以
  明文躺在 nginx snippet 里，且出现在多处）。
- `proxy_connect_timeout 5s` / `proxy_read_timeout 60s`；upstream keepalive 16。
- 转发 `X-Forwarded-Host` / `X-Forwarded-For` / `X-Forwarded-Proto: https`。
- 热备：上游返回 502/503/504/520–526/530 时 `error_page` 回落命名 location：
  - 仅放行 GET/HEAD，其余方法一律 503；
  - `rewrite` 还原应用内部路径后反代本机 `127.0.0.1:8901`（lab 每 5 分钟推送的
    只读快照，edge 模式）；
  - 响应带 `X-Alethic-Origin: cloud-standby`。

### 1.2 机试上传的 body 量级（关键可行性数据）

- 交卷端点 `POST /api/campaigns/{cid}/sessions`（`server/app/routes/uploads.py`），
  multipart：`cleaned`（.jsonl/.md 清洗件）+ 可选 `attachment`（zip）。
- 附件 zip 双端硬上限 50MB：`server/app/constants.py` `MAX_ATTACHMENT_BYTES =
  50 * 1024 * 1024`（413 超限），`exam_kit/exam_server.py` `ATTACHMENT_MAX_BYTES`
  同值。multipart 总 body 理论上限 ≈ 52MB。
- 实测 VPS `data/uploads`（standby 同步副本）：47 个文件共 **4.9MB**，单文件
  1–2MB（cleaned_q*.jsonl）。当前量纲下附件 zip 尚未出现大文件。
- nginx `client_max_body_size 100m`（注释明确是为 CF 免费版 100MB 上传上限对齐）。
- 另有 LAN 交卷（`lan-uploaded`，文件留考场局域网接收机，云端只记状态，无 body）
  与 P2P 直传（Headscale mesh 直推实验室，不过云端）两条旁路，均不经此链路传大文件。

**结论：单请求 body ≤ ~52MB，远低于 CF 免费版 100MB 上传上限；常规请求只有
KB 级。Worker 免费版限制（见 §3）不构成障碍，不需要「上传留 VPS」的混合方案。**

### 1.3 应用侧前提（已核对，Worker 无需改写响应体）

- 应用内部挂在 `/`，HTML 里的链接/资源路径由 `PUBLIC_PREFIX`（`/recruitment`）
  与 admin 路径占位符在服务端渲染时注入（`server/app/main.py`），即 lab 返回的
  页面 URL 已自带 `/recruitment` 前缀，Worker 原样透传即可。
- 认证用 `Authorization: Bearer`，**无 Cookie**，无需处理 cookie domain/path 改写。
- 无 WebSocket 端点，纯 HTTP 请求/响应，Worker 全兼容。

## 2. Worker 草稿（JavaScript）

路由绑定（`wrangler.toml`）：

```toml
name = "alethic-recruitment-direct"
main = "src/worker.js"
compatibility_date = "2026-10-16"

# 仅拦截招新平台前缀，主站静态资源不受影响
routes = [
  { pattern = "alethicinsight.com/recruitment*",  zone_name = "alethicinsight.com" },
  { pattern = "alethicinsight.com/admin/recruitment*", zone_name = "alethicinsight.com" },
]

# 密钥经 `wrangler secret put` 注入（取自 VPS /etc/nginx/snippets/cf-access-lab-origin.conf）：
#   CF_ACCESS_CLIENT_ID, CF_ACCESS_CLIENT_SECRET

[vars]
LAB_ORIGIN = "https://lab-origin.alethicinsight.com"
# 热备入口：VPS 上新增的 standby vhost（见 §4 迁移步骤 2），DNS 走同一 CF Tunnel 到 VPS。
# 必须是不挂本 Worker 路由的独立主机名，否则 failover 请求会被 Worker 再次拦截造成环路。
STANDBY_ORIGIN = "https://standby.alethicinsight.com"
```

`src/worker.js`：

```javascript
// /recruitment/* 与 /admin/recruitment* 直连 lab-origin 的路由 Worker。
// 路径映射与腾讯云 VPS nginx 现状严格一致；lab 不可达时 GET/HEAD 回落 VPS 只读热备。

const FAILOVER_STATUSES = new Set([
  502, 503, 504,
  520, 521, 522, 523, 524, 525, 526, 530, // CF 侧源站/隧道故障码
]);

// 入口路径映射：返回 { upstreamPath } 或 Response（跳转）
function mapRequest(url) {
  const p = url.pathname;
  if (p === "/recruitment") {
    return Response.redirect(url.origin + "/recruitment/" + url.search, 301);
  }
  if (p === "/recruitment/admin") {
    return Response.redirect(url.origin + "/admin/recruitment" + url.search, 301);
  }
  if (p === "/admin/recruitment/") {
    return Response.redirect(url.origin + "/admin/recruitment" + url.search, 301);
  }
  if (p === "/admin/recruitment") {
    return { upstreamPath: "/admin" + url.search };
  }
  if (p.startsWith("/recruitment/")) {
    // strip /recruitment 前缀，与 nginx proxy_pass https://lab_origin/ 一致
    return { upstreamPath: p.slice("/recruitment".length) + url.search };
  }
  return null; // 不应到达：route 已限定前缀
}

function buildUpstreamHeaders(request, env) {
  const h = new Headers(request.headers);
  // 防走私：剥掉客户端自带的 Access 头，再统一换成 service token
  h.delete("CF-Access-Client-Id");
  h.delete("CF-Access-Client-Secret");
  h.set("CF-Access-Client-Id", env.CF_ACCESS_CLIENT_ID);
  h.set("CF-Access-Client-Secret", env.CF_ACCESS_CLIENT_SECRET);
  h.set("X-Forwarded-Host", new URL(request.url).host);
  h.set("X-Forwarded-Proto", "https");
  // Host 由 fetch 目标 URL 自动决定（= lab-origin），不可也不需手动设置
  return h;
}

async function fetchUpstream(originBase, upstreamPath, request, env) {
  const headers = buildUpstreamHeaders(request, env);
  return fetch(originBase + upstreamPath, {
    method: request.method,
    headers,
    // 流式透传请求体（含 multipart 上传）；Workers 运行时原生支持，无需缓冲整包
    body: ["GET", "HEAD"].includes(request.method) ? undefined : request.body,
    redirect: "manual", // 重定向原样回客户端，与 nginx 行为一致
  });
}

function withStandbyMarker(resp) {
  const h = new Headers(resp.headers);
  h.set("X-Alethic-Origin", "cloud-standby"); // 与 nginx add_header 对齐
  return new Response(resp.body, { status: resp.status, statusText: resp.statusText, headers: h });
}

async function handle(request, env) {
  const mapped = mapRequest(new URL(request.url));
  if (mapped === null) return new Response("not found", { status: 404 });
  if (mapped instanceof Response) return mapped;

  const { upstreamPath } = mapped;
  let resp;
  try {
    resp = await fetchUpstream(env.LAB_ORIGIN, upstreamPath, request, env);
  } catch (err) {
    resp = null; // 网络层失败（隧道断、TLS 失败等）== nginx 的 502/521/522
  }

  const needFailover = resp === null || FAILOVER_STATUSES.has(resp.status);
  if (!needFailover) {
    // 流式透传响应体（含大文件下载），不整读
    return new Response(resp.body, resp);
  }

  // —— 热备等价物（对应 nginx error_page → @*_fallback）——
  if (!["GET", "HEAD"].includes(request.method)) {
    // 与 nginx 热备语义一致：回退模式下写操作一律 503
    return new Response("standby is read-only, primary unreachable", { status: 503 });
  }
  try {
    // standby vhost 上 nginx 直接把 /recruitment/* 与 /admin/recruitment*
    // 映射到本地 8901（不经 lab），因此 failover 沿用「公开路径」原样转发
    const url = new URL(request.url);
    const sresp = await fetch(env.STANDBY_ORIGIN + url.pathname + url.search, {
      method: request.method,
      headers: request.headers, // GET/HEAD 无 body；不带 Access token（standby 无 Access）
      redirect: "manual",
    });
    if (FAILOVER_STATUSES.has(sresp.status)) throw new Error("standby also down");
    return withStandbyMarker(sresp);
  } catch (err) {
    return new Response("origin unreachable and standby failed", { status: 503 });
  }
}

export default { fetch: (request, env) => handle(request, env) };
```

> 说明（部署前要核对的点）：
> 1. nginx 默认 `proxy_redirect default` 会把上游 `Location` 里的
>    `https://lab-origin.alethicinsight.com/...` 改写回 `/recruitment/...`。
>    因 lab 应用经 `PUBLIC_PREFIX` 渲染、重定向多为相对路径，实测若发现绝对
>    `Location` 漏改写，在 Worker 出口加一段：`Location` 以
>    `https://lab-origin.alethicinsight.com/` 开头的，替换为
>    `https://alethicinsight.com/recruitment/`（admin 同理）。
> 2. `proxy_read_timeout 60s` 在 Worker 侧无直接等价物；CF 对空闲流有内部超时，
>    长上传期间只要 body 在流动就不会被掐。如要保守，可对非上传请求加
>    `AbortSignal.timeout(30000)`，上传路径（multipart、Content-Length > 1MB）
>    不加。
> 3. 草稿故意不缓存任何响应（`Cache-Control` 透传），行为与 nginx 现状一致；
>    `/recruitment/static/*` 若要边缘缓存可后续加 `caches.default`，非本次范围。

## 3. 关键约束分析：Workers 免费版 vs 机试上传

| 约束 | 免费版数值 | 本场景 | 判定 |
|---|---|---|---|
| CPU 时间 | 10ms/请求（默认） | 纯流式转发，几乎零 CPU；I/O 等待不计 CPU | 通过 |
| 请求体大小 | 受 zone 上传上限约束，免费版 100MB | 实测 ≤2MB；理论上限 ~52MB（50MB 附件 zip 硬顶） | 通过 |
| 响应体 | 无大小上限，流式 | 下载附件 zip ≤50MB，流式透传 | 通过 |
| 每请求子请求数 | 50 | 主路径 1 次 fetch，failover 最多 2 次 | 通过 |
| 请求量 | 10 万次/天 | 招新平台量纲（日活数百） | 通过 |
| Worker 存活时长 | 流式期间不受 CPU 限制，仅空闲超时 | 上传期间 body 持续流动 | 通过 |

**结论：上传路径完全可以在 Worker 上跑，不需要「上传留 VPS、其余走 Worker」的
混合方案。** 唯一接近上限的场景是「附件 zip 顶满 50MB + multipart 开销」，
离 100MB 红线仍有近一倍余量；若未来放宽附件上限，需同步复核 CF 免费版上传
上限（届时混合方案是兜底选项：把 `POST .../sessions` 且 Content-Length 大于阈值
的请求在 Worker 里转回 VPS 入口）。

## 4. 迁移步骤

1. **建 Worker**：`wrangler init`（或 dashboard），粘贴 §2 草稿；
   `wrangler secret put CF_ACCESS_CLIENT_ID / CF_ACCESS_CLIENT_SECRET`
   （值取自 VPS `/etc/nginx/snippets/cf-access-lab-origin.conf`）。
   **趁机轮换该 service token**（CF Zero Trust → Access → Service Auth 里重建，
   新值同时写入 wrangler secret 与 VPS snippet——VPS 暂留期间两边都要能工作）。
2. **VPS 加 standby vhost**（唯一的服务器侧变更，用于 Worker 热备回路）：
   - DNS：`standby.alethicinsight.com` 走现有 CF Tunnel 指到 VPS（proxied）；
   - 该主机名**不绑 Worker 路由**（防环路）；
   - nginx 新 server 块：`/recruitment/*` 与 `/admin/recruitment*` 直接映射到
     `127.0.0.1:8901` 只读副本（映射规则复用现有 `@*_fallback` 的 rewrite），
     仍限 GET/HEAD、写操作 503、带 `X-Alethic-Origin: cloud-standby`。
3. **灰度验证**：先把 Worker 绑到测试主机名（如 `dev-worker.alethicinsight.com`
   同 zone）验证四件事：页面 GET、登录 POST、multipart 上传（造一个 50MB zip
   压上限）、kill lab 的 cloudflared 验证 failover 到 standby 且带
   `X-Alethic-Origin` 头、POST 在 failover 时 503。
4. **切换**：给 Worker 挂上 §2 的两条正式 route。Worker route 优先于隧道回源，
   即刻生效；**VPS nginx 现有 `/recruitment` 配置原样保留不动**（作为回滚兜底
   与 standby 回路的一部分）。
5. **观察**：CF dashboard Worker 的 errors/subrequests 指标 + lab uvicorn 日志 +
   VPS standby 8901 访问日志（failover 时不应为零增长误判）。

## 5. 回滚

- 在 CF dashboard 删除（或禁用）Worker 的两条 route，流量立即恢复
  「隧道 → VPS nginx → lab」原链路——nginx 配置全程未动，零残留。
- standby vhost 留着无害（独立主机名，无公网入口指向它时可不下线）。
- 若做了 token 轮换，回滚只需保证 VPS snippet 里的 token 仍有效。

## 6. 结论：值不值得做

**值得做，且可以做全量（含上传），不需要混合方案。**

- 可行性：实测上传量纲（1–2MB 清洗件、附件 50MB 硬顶）离 Worker 免费版所有
  红线都有数倍余量，纯流式转发对 10ms CPU 限制无感。
- 收益：每个请求省掉「CF 边缘 → 腾讯云 VPS → CF 边缘」一整跳（一次公网往返
  + 一次 TLS 握手 + 一次隧道回源），冷连接 ~2s+ 中最可压缩的一段；热连接也因
  RTT 减半而受益。所有页面/API/上传/下载请求均等受益。
- 代价：一个 ~100 行 Worker + VPS 上一个 standby vhost；Access token 从 nginx
  snippet 平移到 wrangler secret（安全性反而提升，建议顺手轮换）。
- 不变量：lab-origin 的 CF Access 与隧道安全边界原样保留；热备语义（GET/HEAD
  只读、写 503）等价迁移；回滚是删一条 route 的一键操作。
- 剩余地板：CF 边缘 → 国内实验室隧道的 RTT 无法消除（物理距离），这是优化后
  的延迟下限；P2P/LAN 旁路不受影响。
