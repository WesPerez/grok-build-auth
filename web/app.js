const $ = (id) => document.getElementById(id);
const BASE = location.pathname.startsWith("/grok/") ? "/grok" : "";
let state = { task: null, batches: [], checks: [] };

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
  };
  return map[status] || [status || "未知", "pending"];
}

function renderChecks() {
  $("checks").innerHTML = state.checks.map(item => `<div class="check-row"><span class="${item.warning ? "warn" : item.ok ? "ok" : "bad"}">${item.warning ? "!" : item.ok ? "✓" : "×"}</span><div><strong>${esc(item.name)}</strong><span>${esc(item.detail)}</span></div></div>`).join("");
  const good = state.checks.filter(x => x.ok).length;
  const warnings = state.checks.filter(x => x.warning).length;
  const blocking = state.checks.filter(x => !x.ok && x.blocking !== false).length;
  $("systemLine").textContent = blocking ? `${good}/${state.checks.length} 项检查通过，请先处理阻塞异常` : warnings ? `核心环境正常，可操作；另有 ${warnings} 项非阻塞告警` : "运行环境正常，可开始新批次";
}

function attemptRow(item) {
  const stage = item.failed_stage || item.stage || (item.status === "registered" ? "已注册" : "等待");
  return `<div class="attempt"><span>${esc(item.email || "正在分配邮箱")}</span><span>${esc(item.status || "等待")}</span><span>${esc(stage)}</span>${item.error ? `<span class="error">${esc(item.error)}</span>` : ""}</div>`;
}

function renderBatches() {
  $("batchCount").textContent = `${state.batches.length} 个批次`;
  $("batches").innerHTML = state.batches.map(batch => {
    const [label, cls] = statusInfo(batch.status);
    const requested = batch.requested_attempts || batch.attempts.length;
    const probes = batch.preimport_auth_probes;
    return `<article class="batch"><div class="batch-head"><div><h3>${esc(batch.batch_id)}</h3><div class="batch-meta"><span>注册成功 ${batch.registered_count || 0}/${requested}</span><span>失败 ${batch.failed_count || 0}</span><span>导入 ${batch.imported_ids?.length || 0}</span><span>${probes ? `上游预检 ${probes.http_200_completed}/${probes.tested}` : "上游未预检"}</span><span>${batch.has_backup ? "已有备份" : "尚无备份"}</span></div></div><div><span class="badge ${cls}">${label}</span>${batch.resumable ? ` <button class="resume" data-batch="${esc(batch.batch_id)}">继续导入</button>` : ""}</div></div>${batch.error_summary ? `<p class="bad">${esc(batch.error_summary)}</p>` : ""}<div class="attempts">${batch.attempts.map(attemptRow).join("")}</div></article>`;
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
  badge.textContent = task.running ? "运行中" : task.exit_code === 0 ? "已完成" : "执行失败";
  badge.className = `badge ${task.running ? "running" : task.exit_code === 0 ? "success" : "failure"}`;
  const latest = state.batches.find(batch => task.batch_id && batch.batch_id === task.batch_id) || state.batches.find(batch => ["running","importing","resuming-import"].includes(batch.status)) || state.batches[0] || {};
  const total = latest.requested_attempts || 0, success = latest.registered_count || 0, failed = latest.failed_count || 0;
  const done = success + failed;
  const importFinished = ["completed", "imported-preprobed", "imported-not-probed"].includes(latest.status);
  $("progressBar").style.width = `${total ? Math.min(100, done / total * 85 + (importFinished ? 15 : 0)) : 5}%`;
  $("activeMetrics").innerHTML = [[success,"注册成功"],[failed,"注册失败"],[Math.max(0,total-done),"剩余"],[latest.imported_ids?.length || 0,"已导入"]].map(x => `<div class="metric"><strong>${x[0]}</strong><span>${x[1]}</span></div>`).join("");
}

async function refresh() {
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
    await post(`${BASE}/api/start`, {count, import_partial:$("partial").checked, cleanup_failed_mailboxes:$("cleanup").checked});
    toast(`已开始处理 ${count} 个账号`); await refresh();
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
$("startButton").onclick = startBatch;
$("refreshButton").onclick = refresh;
$("doctorButton").onclick = refresh;
refresh(); setInterval(refresh, 1500);
