const $ = (id) => document.getElementById(id);
const BASE = location.pathname.startsWith("/grok/") ? "/grok" : "";
let state = { task: null, batches: [], checks: [] };
let refreshing = false;

const STAGES = {
  registration:"准备注册", mailbox:"创建邮箱", signup:"初始化注册", "email-verification":"等待邮箱验证码",
  turnstile:"处理 Turnstile", "account-creation":"创建账号", sso:"获取 SSO", oauth:"授权 Grok Build",
  "import-preflight":"导入预检", "sub2api-preflight":"Sub2API 预检", "sub2api-import":"导入 Sub2API",
  "upstream-preprobe":"验证新 OAuth 凭据", "grok-reconcile":"核对账号与分组",
  "sub2api-postprobe":"验证 Sub2API Grok 请求", completed:"全部完成"
};

function esc(value) {
  return String(value ?? "").replace(/[&<>'"]/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
}

function toast(message) {
  const node = $("toast"); node.textContent = message; node.classList.add("show");
  setTimeout(() => node.classList.remove("show"), 3200);
}

function statusInfo(status) {
  const map = {
    completed: ["完成", "success"], "imported-preprobed": ["已导入 · 上游预检通过", "success"], "imported-not-probed": ["已导入 · 未探测", "pending"], running: ["注册中", "running"], importing: ["导入中", "running"],
    "resuming-import": ["继续导入", "running"], "import-failed": ["导入失败", "failure"],
    "import-verification-failed": ["验证失败", "failure"], "registered-not-imported": ["待导入", "pending"],
    "registration-failed-import-skipped": ["部分失败", "failure"], "failed-no-successes": ["注册失败", "failure"],
    interrupted: ["执行中断", "failure"], stalled: ["运行较慢", "pending"],
  };
  return map[status] || [status || "未知", "pending"];
}

function ageSeconds(value) {
  const time = Date.parse(value || "");
  return Number.isFinite(time) ? Math.max(0, Math.floor((Date.now() - time) / 1000)) : null;
}

function duration(value) {
  const seconds = ageSeconds(value);
  if (seconds == null) return "时间未知";
  if (seconds < 60) return `${seconds} 秒`;
  return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}

function actionFor(item) {
  if (item.action_hint) return item.action_hint;
  const stage = item.failed_stage || item.stage;
  if (stage === "email-verification") return "确认邮箱服务与收信链路正常，然后新建批次重试。";
  if (stage === "turnstile") return "检查验证码服务余额、网络和任务错误码后重试。";
  if (stage === "oauth" || stage === "sso") return "账号可能已创建；请先核对日志，避免直接重复注册同一邮箱。";
  if (String(stage || "").includes("import") || stage === "grok-reconcile") return "注册凭据已保留。修复 Sub2API 问题后使用“继续导入”，不要重新注册。";
  if (item.abort_reason === "max-consecutive-failures") return `连续失败已触发熔断，${item.unstarted_attempts || 0} 个请求未启动。修复共同根因后新建批次。`;
  return "查看该账号错误和任务日志，修复原因后再启动新批次。";
}

function renderChecks() {
  $("checks").innerHTML = state.checks.map(item => `<div class="check-row"><span class="${item.warning ? "warn" : item.ok ? "ok" : "bad"}">${item.warning ? "!" : item.ok ? "✓" : "×"}</span><div><strong>${esc(item.name)}</strong><span>${esc(item.detail)}</span></div></div>`).join("");
  const good = state.checks.filter(x => x.ok).length;
  const warnings = state.checks.filter(x => x.warning).length;
  const blocking = state.checks.filter(x => !x.ok && x.blocking !== false).length;
  $("systemLine").textContent = blocking ? `${good}/${state.checks.length} 项检查通过，请先处理阻塞异常` : warnings ? `核心环境正常，可操作；另有 ${warnings} 项非阻塞告警` : "运行环境正常，可开始新批次";
  const proxy = state.checks.find(item => item.name === "注册代理池");
  $("proxyPoolLine").textContent = proxy ? proxy.detail : "代理池状态未知";
}

function attemptRow(item) {
  const stage = item.failed_stage || item.stage || (item.status === "registered" ? "已注册" : "等待");
  const timing = item.status === "running" ? ` · ${duration(item.stage_started_at || item.started_at)}` : item.duration_seconds != null ? ` · ${esc(item.duration_seconds)} 秒` : "";
  const proxy = item.proxy_ref ? ` · ${item.proxy_ref === "direct" ? "直连" : `代理 ${item.proxy_ref}`}` : "";
  return `<div class="attempt"><span>${esc(item.email || "正在分配邮箱")}</span><span>${esc(item.status || "等待")}${esc(proxy)}</span><span>${esc(STAGES[stage] || stage)}${timing}</span>${item.error ? `<span class="error">${esc(item.error)}<small>${esc(actionFor(item))}</small></span>` : ""}</div>`;
}

function renderBatches() {
  $("batchCount").textContent = `${state.batches.length} 个批次`;
  $("batches").innerHTML = state.batches.map(batch => {
    const displayStatus = batch.runtime_state === "interrupted" ? "interrupted" : batch.status;
    const [label, cls] = statusInfo(displayStatus);
    const requested = batch.requested_attempts || batch.attempts.length;
    const probes = batch.preimport_auth_probes;
    const action = (batch.error_summary || batch.runtime_state === "interrupted") ? `<p class="action-hint">${esc(batch.error_summary || actionFor(batch))}</p>` : "";
    const unstarted = batch.unstarted_attempts ? `<span>未启动 ${esc(batch.unstarted_attempts)}</span>` : "";
    const network = batch.proxy_pool?.configured ? `代理池 ${batch.proxy_pool.enabled_nodes} 节点` : "直连";
    return `<article class="batch"><div class="batch-head"><div><h3>${esc(batch.batch_id)}</h3><div class="batch-meta"><span>注册成功 ${batch.registered_count || 0}/${requested}</span><span>失败 ${batch.failed_count || 0}</span>${unstarted}<span>并发 ${batch.workers || 1}</span><span>${esc(network)}</span><span>导入 ${batch.imported_ids?.length || 0}</span><span>${probes ? `上游预检 ${probes.http_200_completed}/${probes.tested}` : "上游未预检"}</span><span>${batch.has_backup ? "已有备份" : "尚无备份"}</span></div></div><div><span class="badge ${cls}">${label}</span>${batch.resumable ? ` <button class="resume" data-batch="${esc(batch.batch_id)}">继续导入</button>` : ""}</div></div>${action}<div class="attempts">${batch.attempts.map(attemptRow).join("")}</div></article>`;
  }).join("") || "<p>还没有批次记录。</p>";
  document.querySelectorAll(".resume").forEach(button => button.addEventListener("click", () => resumeBatch(button.dataset.batch)));
}

function renderActive() {
  const task = state.task;
  const section = $("activeSection");
  if (!task) { section.classList.add("hidden"); return; }
  section.classList.remove("hidden");
  $("activeTitle").textContent = task.subject;
  const badge = $("activeBadge");
  const latest = state.batches.find(batch => task.batch_id && batch.batch_id === task.batch_id) || state.batches.find(batch => ["running","importing","resuming-import"].includes(batch.status)) || state.batches[0] || {};
  const heartbeatAge = ageSeconds(latest.last_activity_at || task.started_at);
  const stalled = task.running && heartbeatAge != null && heartbeatAge > 20;
  badge.textContent = task.interrupted ? "执行中断" : stalled ? "运行较慢" : task.running ? "运行中" : task.exit_code === 0 ? "已完成" : "执行失败";
  badge.className = `badge ${task.running && !stalled ? "running" : task.exit_code === 0 && !task.interrupted ? "success" : stalled ? "pending" : "failure"}`;
  const runningAttempt = latest.attempts?.find(item => item.status === "running");
  const stage = runningAttempt?.stage || latest.current_stage || "registration";
  $("activeStage").textContent = `${STAGES[stage] || stage} · 阶段已用 ${duration(runningAttempt?.stage_started_at || latest.stage_started_at || latest.started_at)} · 最近活动 ${duration(latest.last_activity_at || task.started_at)}前`;
  const needsAction = task.interrupted || (!task.running && task.exit_code !== 0) || latest.error_summary;
  $("activeAction").textContent = needsAction ? actionFor(latest) : stalled ? "当前阶段超过 20 秒没有心跳，请观察日志；超过该步骤正常超时后再按失败处理。" : "";
  $("activeAction").classList.toggle("hidden", !needsAction && !stalled);
  const total = latest.requested_attempts || 0, success = latest.registered_count || 0, failed = latest.failed_count || 0;
  const done = success + failed;
  const importFinished = ["completed", "imported-preprobed", "imported-not-probed"].includes(latest.status);
  $("progressBar").style.width = `${total ? Math.min(100, done / total * 85 + (importFinished ? 15 : 0)) : 5}%`;
  $("activeMetrics").innerHTML = [[success,"注册成功"],[failed,"注册失败"],[Math.max(0,total-done),"剩余"],[latest.imported_ids?.length || 0,"已导入"]].map(x => `<div class="metric"><strong>${x[0]}</strong><span>${x[1]}</span></div>`).join("");
}

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    state = await (await fetch(`${BASE}/api/state`, {cache:"no-store"})).json();
    renderChecks(); renderBatches(); renderActive();
    if (state.task) {
      const data = await (await fetch(`${BASE}/api/task-log`, {cache:"no-store"})).json();
      $("liveLog").textContent = data.log || "任务已启动，等待输出...";
      $("liveLog").scrollTop = $("liveLog").scrollHeight;
    }
    $("startButton").disabled = Boolean(state.task?.running) || state.checks.some(x => !x.ok && x.blocking !== false);
  } catch (error) { toast(`刷新失败：${error.message}`); }
  finally { refreshing = false; }
}

async function refreshDoctor() {
  try {
    const data = await post(`${BASE}/api/doctor`);
    state.checks = data.checks || [];
    renderChecks(); toast("环境检查已更新");
  } catch (error) { toast(error.message); }
}

async function post(url, body={}) {
  const response = await fetch(url, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "操作失败");
  return data;
}

async function startBatch() {
  try {
    const count = Number($("count").value);
    if (!window.confirm(`将注册 ${count} 个外部账号，并在成功后写入生产 Sub2API。是否继续？`)) return;
    const workers = Number($("workers").value);
    const registration_backend = $("registrationBackend").value;
    await post(`${BASE}/api/start`, {count, workers, registration_backend, import_partial:$("partial").checked, cleanup_failed_mailboxes:$("cleanup").checked});
    toast(`已开始处理 ${count} 个账号，并发 ${workers} 路`); await refresh();
  } catch (error) { toast(error.message); }
}

async function resumeBatch(id) {
  try {
    if (!window.confirm(`将复用批次 ${id} 的现有账号继续写入生产 Sub2API，不会重新注册。是否继续？`)) return;
    await post(`${BASE}/api/resume/${id}`); toast("已继续导入，不会重新注册"); await refresh();
  }
  catch (error) { toast(error.message); }
}

$("minus").onclick = () => $("count").value = Math.max(1, Number($("count").value) - 1);
$("plus").onclick = () => $("count").value = Math.min(100, Number($("count").value) + 1);
$("registrationBackend").onchange = () => {
  if ($("registrationBackend").value === "browser-playwright-edge") $("workers").value = "1";
  $("workers").disabled = $("registrationBackend").value === "browser-playwright-edge";
};
$("startButton").onclick = startBatch;
$("refreshButton").onclick = refresh;
$("doctorButton").onclick = refreshDoctor;
refresh(); setInterval(refresh, 1500);
