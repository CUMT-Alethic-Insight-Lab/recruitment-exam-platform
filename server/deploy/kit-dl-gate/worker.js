// kit-dl-gate —— dl.alethicinsight.com 的下载闸门（Cloudflare Worker）。
//
// R2 bucket 转私有后，本 Worker 是唯一公网入口：只做「HMAC 验签 + 流式回源」，
// 绝不缓冲文件体（全程无 arrayBuffer()/text()，331MB 在 CF 网内流穿，不进内存）。
// 签名格式与 lab（server/app/routes/exam.py 的 sign_kit_url）共享：
//   sig = hex(HMAC_SHA256(KIT_SIGNING_SECRET, "{key}\n{exp}"))
//   url = https://dl.alethicinsight.com/{key}?exp={unix 秒}&sig={sig}
// Range 头原样透传给 R2（get() 的 range 参数官方支持 Headers），
// aria2/IDM 多线程分段下载共享同一条签名 URL 天然可用。
//
// 部署（与 lab 一起做，方案见 docs/PROPOSAL_r2_download_gating.md §6）：
//   wrangler secret put KIT_SIGNING_SECRET   # 与 lab 环境变量同值（256-bit 随机）
//   wrangler deploy                          # 路由/绑定见 wrangler.toml 草稿

// 旧安装包在 R2 里的对象名（lab 侧 config.KIT_OBJECT_KEY 同名）。
const LEGACY_KIT_KEY = "AlethicInsight-ExamKit-2026-setup.exe";

// ---------------------------------------------------------------------------
// 旧裸链宽限期（需求：放行旧裸链免签，硬截止后一律 403）
//
// 背景：入口页 index.html 与已发出的旧机试包（OTA 里的 kit-version.json）都硬编码
// 了不带任何参数的裸直链。R2 转私有 + 本 Worker 上线后，为让存量链接平滑过渡，
// 对且仅对「旧裸链」短时放行：
//   - 只放行对象名 == LEGACY_KIT_KEY 的裸请求（不带 exp、不带 sig，原样旧链接）；
//   - 只放行到 2026-11-16 00:00 Asia/Shanghai（= 2026-11-15T16:00:00Z，UTC+8 无夏令时）；
//   - 截止后对同一裸请求硬 403，无任何开关、无需再部署——Date.now() 与下面的
//     截止时刻比较，时间一到自然生效；
//   - 带 exp/sig 参数但验签失败/过期的请求即使在宽限期内也直接 403（fail closed）：
//     宽限期只豁免「完全没有凭证」的旧链接，不给拼凑凭证者任何试探空间。
// 宽限期内的裸请求与签名请求走同一条流式回源路径（serve()），行为除验签外无差别。
// ---------------------------------------------------------------------------
const GRACE_DEADLINE_MS = Date.parse("2026-11-15T16:00:00Z"); // 2026-11-16 00:00 Asia/Shanghai

function isBareLink(url) {
  // 「旧裸链」= 完全不带签名参数（exp 与 sig 都缺席）。带了哪怕一个参数就算
  // 凭证请求，验签失败即 403，不适用宽限。
  return !url.searchParams.has("exp") && !url.searchParams.has("sig");
}

function withinGracePeriod() {
  return Date.now() < GRACE_DEADLINE_MS;
}

const FORBIDDEN_MESSAGE =
  "下载链接已过期或未授权，请回招新平台重新获取。";

function forbidden() {
  return new Response(FORBIDDEN_MESSAGE, {
    status: 403,
    headers: { "content-type": "text/plain; charset=utf-8" },
  });
}

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

async function verifySig(url, env, key) {
  const exp = Number(url.searchParams.get("exp") || 0);
  const sig = (url.searchParams.get("sig") || "").toLowerCase();
  if (!exp || !/^[0-9a-f]{64}$/.test(sig)) return false;
  if (Date.now() / 1000 > exp) return false; // 过期：now > exp 即拒
  const want = await hmacHex(env.KIT_SIGNING_SECRET, `${key}\n${exp}`);
  return timingSafeEqual(want, sig);
}

// 流式回源私有 bucket。Range 头透传（{ range: request.headers }，官方支持的
// Headers 形态），命中分段时回 206 + Content-Range；绝不把 body 读进内存。
async function serve(request, env, key) {
  if (request.method === "HEAD") {
    // HEAD 探测（下载器握手常见）：只查元数据，不触发 331MB 的流式读取
    const meta = await env.KIT_BUCKET.head(key);
    if (meta === null) return notFound();
    const headers = new Headers();
    meta.writeHttpMetadata(headers);
    headers.set("etag", meta.httpEtag);
    headers.set("accept-ranges", "bytes");
    headers.set("content-length", String(meta.size));
    return new Response(null, { status: 200, headers });
  }
  let object;
  try {
    object = await env.KIT_BUCKET.get(key, { range: request.headers });
  } catch (_exc) {
    // Range 不可满足等 R2 侧取数异常 → 416（多线程下载器分段越界时会出现）
    return new Response("Requested Range Not Satisfiable", { status: 416 });
  }
  if (object === null) return notFound();
  const headers = new Headers();
  object.writeHttpMetadata(headers);
  headers.set("etag", object.httpEtag);
  headers.set("accept-ranges", "bytes");
  headers.set("cache-control", "private, no-store"); // 签名 URL 不进共享缓存
  headers.set("content-disposition",
              `attachment; filename="${key.split("/").pop()}"`);
  let status = 200;
  if (object.range) {
    // R2Range: {offset, length} 或 {suffix}（请求带了 Range 且 R2 已应用）
    const offset = object.range.offset ?? Math.max(0, object.size - object.range.suffix);
    const length = object.range.length ?? object.range.suffix;
    headers.set("content-range", `bytes ${offset}-${offset + length - 1}/${object.size}`);
    status = 206;
  }
  return new Response(object.body, { status, headers });
}

function notFound() {
  return new Response("Not Found", { status: 404 });
}

export default {
  async fetch(request, env) {
    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response("Method Not Allowed", {
        status: 405,
        headers: { Allow: "GET, HEAD" },
      });
    }
    const url = new URL(request.url);
    let key;
    try {
      key = decodeURIComponent(url.pathname.slice(1));
    } catch (_exc) {
      return notFound();
    }
    if (!key || key.includes("..")) return notFound();

    if (isBareLink(url)) {
      // 旧裸链：仅宽限期内、仅旧安装包对象名放行（见顶部宽限期注释）
      if (key === LEGACY_KIT_KEY && withinGracePeriod()) {
        return serve(request, env, key);
      }
      return forbidden();
    }
    // 凭证请求（带 exp/sig）：验签不过（含格式错/过期/签错）一律 403，
    // 宽限期不豁免
    if (!(await verifySig(url, env, key))) return forbidden();
    return serve(request, env, key);
  },
};
