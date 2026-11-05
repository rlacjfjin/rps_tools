/* rps_tools 前端：无构建步骤的单页应用。
 * 表单完全由 /api/meta 返回的工具参数表生成；新增工具/参数无需改这里。
 * 结果页按 tool.view 分三种：diff（分页差异表）/ report（报告 + 产物）/ text（文本输出）。
 */
"use strict";

const S = { meta: null, tools: {}, cleanup: [] };
const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => Array.from(el.querySelectorAll(sel));

// ------------------------------------------------------------------ helpers
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "html") el.innerHTML = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  const ct = res.headers.get("content-type") || "";
  const body = ct.includes("json") ? await res.json() : await res.text();
  if (!res.ok) throw new Error((body && body.detail) || body || `HTTP ${res.status}`);
  return body;
}

const store = {
  get(k, d) { try { const v = localStorage.getItem("rps:" + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("rps:" + k, JSON.stringify(v)); } catch { /* 隐私模式等 */ } },
};

function fmtNum(v) {
  if (v === null || v === undefined || v === "") return "";
  const n = Number(v);
  if (!Number.isFinite(n)) return String(v);
  if (n !== 0 && Math.abs(n) < 1e-4) return n.toExponential(3);
  if (Math.abs(n) >= 1e4) return n.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
  return String(Number(n.toPrecision(7)));
}
const fmtSigned = (v) => (v > 0 ? "+" : "") + fmtNum(v);
const fmtTime = (ts) => ts ? new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false }) : "";
function fmtDur(s) {
  if (s === null || s === undefined) return "";
  if (s < 60) return s.toFixed(1) + " 秒";
  return Math.floor(s / 60) + " 分 " + Math.round(s % 60) + " 秒";
}
function fmtSize(n) {
  if (n === null || n === undefined) return "";
  const u = ["B", "KB", "MB", "GB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i ? n.toFixed(1) : n) + " " + u[i];
}
const STATUS = { queued: "排队中", running: "运行中", succeeded: "成功", failed: "失败", cancelled: "已取消", interrupted: "已中断" };
const statusBadge = (st) => h("span", { class: "badge " + st }, STATUS[st] || st);
const kindLabel = (k) => (S.meta.kind_labels || {})[k] || k;
const groupTitle = (g) => (S.meta.groups.find((x) => x.id === g) || {}).title || g;

function onCleanup(fn) { S.cleanup.push(fn); }
function poll(fn, ms) {
  let stopped = false, timer = null;
  const tick = async () => { if (stopped) return; try { await fn(); } catch (e) { console.warn(e); } if (!stopped) timer = setTimeout(tick, ms); };
  tick();
  const stop = () => { stopped = true; clearTimeout(timer); };
  onCleanup(stop);
  return stop;
}

function jobSummary(j) {
  const r = j.result || {};
  if (j.status !== "succeeded") return j.error || j.message || "";
  if (j.tool_id === "diff.compare") return `${r.format_title || ""}：差异 ${(r.total ?? 0).toLocaleString()} 条`;
  if (j.group === "exp") { const o = (r.kpis || {}).OBJ; return o ? `ΔOBJ = ${fmtSigned(o.delta)}` : "完成"; }
  if (r.lines !== undefined) return `${r.lines} 行输出，⚠ ${r.warnings}，★ ${r.highlights}`;
  return "完成";
}

// ------------------------------------------------------------------ router
async function route() {
  S.cleanup.splice(0).forEach((f) => f());
  const [path, query] = location.hash.replace(/^#\/?/, "").split("?");
  const parts = path.split("/").filter(Boolean);
  const qs = new URLSearchParams(query || "");
  const app = $("#app");
  renderNav(parts);
  app.replaceChildren();
  try {
    if (!parts.length) return renderHome(app);
    if (parts[0] === "g") return renderGroup(app, parts[1], parts[2], qs);
    if (parts[0] === "jobs") return renderJobs(app);
    if (parts[0] === "job") return renderJob(app, parts[1], qs);
    app.append(h("div", { class: "alert err" }, "页面不存在"));
  } catch (e) {
    app.append(h("div", { class: "alert err" }, String(e.message || e)));
  }
}

function renderNav(parts) {
  const nav = $("#nav");
  const cur = parts[0] === "g" ? "g/" + parts[1] : parts[0] === "job" ? "jobs" : parts[0] || "";
  nav.replaceChildren(
    ...S.meta.groups.map((g, i) => h("a", { class: "nav-link" + (cur === "g/" + g.id ? " active" : ""), href: "#/g/" + g.id }, `${i + 1}. ${g.title}`)),
    h("a", { class: "nav-link" + (cur === "jobs" ? " active" : ""), href: "#/jobs" }, "任务记录"),
  );
}

// ------------------------------------------------------------------ home
async function renderHome(app) {
  app.append(
    h("div", { class: "card hero" },
      h("h1", {}, "rps_solver 诊断工具集"),
      h("p", {}, "三条功能线共用一套后台任务：大文件在独立进程中处理，页面可以随时离开，结果保存在「任务记录」里。")),
    h("div", { class: "grid grid-3" }, S.meta.groups.map((g, i) => {
      const tools = S.meta.tools.filter((t) => t.group === g.id);
      return h("div", { class: "card feature" },
        h("div", { class: "num" }, `功能 ${i + 1}`),
        h("h2", {}, g.title),
        h("p", {}, g.description),
        h("div", { class: "muted small" }, tools.map((t) => t.title).join(" · ")),
        h("div", {}, h("a", { class: "btn primary", href: "#/g/" + g.id }, "开始")));
    })),
  );
  const box = h("div", { class: "card" }, h("h3", {}, "最近任务"));
  app.append(box);
  const jobs = await api("/api/jobs?limit=10");
  box.append(jobsTable(jobs, false));
}

function jobsTable(jobs, withActions, onChange) {
  if (!jobs.length) return h("div", { class: "muted" }, "还没有任务。");
  return h("div", { class: "tablewrap" }, h("table", {},
    h("thead", {}, h("tr", {}, ["提交时间", "工具", "状态", "耗时", "摘要", ""].map((t) => h("th", {}, t)))),
    h("tbody", {}, jobs.map((j) => h("tr", {},
      h("td", { class: "num" }, fmtTime(j.created)),
      h("td", {}, h("a", { href: "#/job/" + j.id }, j.title), h("div", { class: "muted small" }, groupTitle(j.group))),
      h("td", {}, statusBadge(j.status)),
      h("td", { class: "num" }, fmtDur(j.duration)),
      h("td", { class: "small" }, jobSummary(j)),
      h("td", { class: "num" },
        h("a", { class: "btn sm", href: "#/job/" + j.id }, "查看"),
        withActions ? h("button", {
          class: "btn sm danger", onclick: async () => {
            if (!confirm(`删除任务 ${j.id} 及其全部产物？`)) return;
            await api("/api/jobs/" + j.id, { method: "DELETE" }); onChange && onChange();
          },
        }, "删除") : null),
    ))),
  ));
}

// ------------------------------------------------------------------ jobs page
async function renderJobs(app) {
  const filter = h("select", { onchange: () => load() },
    h("option", { value: "" }, "全部功能"), S.meta.groups.map((g) => h("option", { value: g.id }, g.title)));
  const holder = h("div");
  app.append(h("div", { class: "card" },
    h("div", { class: "row" }, h("h2", { style: "margin:0" }, "任务记录"), h("span", { class: "spacer" }), filter),
    h("p", { class: "muted small" }, "任务产物保存在 workspace/jobs/<任务ID>/，超过保留天数自动清理。"),
    holder));
  async function load() {
    const jobs = await api("/api/jobs?limit=300" + (filter.value ? "&group=" + filter.value : ""));
    holder.replaceChildren(jobsTable(jobs, true, load));
    return jobs.some((j) => j.status === "queued" || j.status === "running");
  }
  let busy = await load();
  poll(async () => { if (busy) busy = await load(); }, 3000);
}

// ------------------------------------------------------------------ group / tool form
async function renderGroup(app, groupId, toolId, qs) {
  const group = S.meta.groups.find((g) => g.id === groupId);
  if (!group) throw new Error("未知功能：" + groupId);
  const tools = S.meta.tools.filter((t) => t.group === groupId);
  const tool = tools.find((t) => t.id === toolId) || tools.find((t) => t.id === store.get("lastTool:" + groupId)) || tools[0];
  store.set("lastTool:" + groupId, tool.id);

  app.append(h("h1", {}, group.title), h("p", { class: "muted" }, group.description));
  if (tools.length > 1) {
    app.append(h("div", { class: "tabs" }, tools.map((t) =>
      h("button", { class: "tab" + (t.id === tool.id ? " active" : ""), onclick: () => { location.hash = `#/g/${groupId}/${t.id}`; } }, t.title))));
  }
  let initial = null;
  if (qs.get("from")) {
    try { initial = (await api("/api/jobs/" + qs.get("from"))).params; } catch { /* 原任务已删除 */ }
  }
  const card = h("div", { class: "card" }, h("h2", {}, tool.title), h("p", { class: "muted" }, tool.description));
  card.append(buildForm(tool, initial));
  app.append(card);

  const recent = h("div", { class: "card" }, h("h3", {}, "该功能的最近任务"));
  app.append(recent);
  const jobs = (await api("/api/jobs?limit=8&group=" + groupId));
  recent.append(jobsTable(jobs, false));
}

function buildForm(tool, initial) {
  const saved = Object.assign({}, store.get("form:" + tool.id, {}), initial ? fromJobParams(tool, initial) : {});
  const form = h("form", { onsubmit: (e) => { e.preventDefault(); submit(); } });
  const widgets = {};

  // 按 section 分组，保持参数声明顺序
  const sections = [];
  for (const p of tool.params) {
    let sec = sections.find((s) => s.name === p.section);
    if (!sec) { sec = { name: p.section, params: [] }; sections.push(sec); }
    sec.params.push(p);
  }
  for (const sec of sections) {
    const normal = sec.params.filter((p) => !p.advanced);
    const adv = sec.params.filter((p) => p.advanced);
    const body = [];
    if (normal.length) body.push(h("div", { class: "form-grid" }, normal.map((p) => field(p))));
    if (adv.length) {
      body.push(h("details", { class: "adv" }, h("summary", {}, `高级选项（${adv.length}）`),
        h("div", { class: "form-grid" }, adv.map((p) => field(p)))));
    }
    const wrap = sec.name ? h("fieldset", { "data-section": sec.name }, h("legend", {}, sec.name), body) : h("div", { style: "margin-bottom:14px" }, body);
    sec.el = wrap;
    form.append(wrap);
  }

  function field(p) {
    const w = makeWidget(p, saved[p.name], tool);
    widgets[p.name] = w;
    const wide = p.type === "text" || p.type === "multi";
    w.el = h("div", { class: "field" + (wide ? " wide" : "") },
      p.type === "bool" ? null : h("label", { class: "lbl" }, p.label, p.required ? h("span", { class: "req" }, "*") : null),
      w.node,
      p.help ? h("div", { class: "help" }, p.help) : null);
    w.param = p;
    return w.el;
  }

  // ---- 条件显示（show_if）+ diff 的「自动」格式识别
  const detect = tool.ui && tool.ui.format_detect;
  function effective(name) {
    const w = widgets[name];
    let v = w ? w.value() : undefined;
    if (detect && name === detect.param && v === "auto") v = guessFormat(detect, detect.files.map((f) => widgets[f] && widgets[f].fileName()));
    return v;
  }
  function refresh() {
    for (const w of Object.values(widgets)) {
      const cond = w.param.show_if;
      const show = !cond || Object.entries(cond).every(([k, vals]) => vals.includes(effective(k)));
      w.el.hidden = !show;
    }
    for (const sec of sections) {
      if (!sec.name) continue;
      sec.el.hidden = sec.params.every((p) => widgets[p.name].el.hidden);
    }
    if (detect) {
      const fw = widgets[detect.param];
      const auto = fw.value() === "auto";
      const guess = effective(detect.param);
      fw.hint.textContent = auto ? `当前识别为：${(fw.param.choices.find((c) => c.value === guess) || {}).label || guess}` : "";
    }
  }
  form.addEventListener("change", refresh);
  form.addEventListener("input", refresh);

  const err = h("div");
  const prog = h("div", { class: "progress", hidden: true }, h("div", { style: "width:0%" }));
  const progText = h("span", { class: "muted small" });
  const btn = h("button", { class: "btn primary", type: "submit" }, "提交任务");
  const reset = h("button", {
    class: "btn ghost", type: "button", onclick: () => {
      store.set("form:" + tool.id, {}); location.hash = `#/g/${tool.group}/${tool.id}`; route();
    },
  }, "恢复默认");
  form.append(err, h("div", { class: "submitbar" }, btn, reset, progText), prog);
  setTimeout(refresh);

  function submit() {
    err.replaceChildren();
    const fd = new FormData();
    const remember = {};
    for (const [name, w] of Object.entries(widgets)) {
      if (w.el.hidden) continue;
      w.append(fd);
      // 只记住改过的字段，这样以后调整默认值能对老用户生效
      const v = w.save();
      if (w.param.type === "file" || !sameAsDefault(v, w.param.default)) remember[name] = v;
    }
    store.set("form:" + tool.id, remember);
    btn.disabled = true;
    prog.hidden = false;
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/tools/${tool.id}/jobs`);
    xhr.upload.onprogress = (e) => {
      if (!e.lengthComputable) return;
      const pct = (e.loaded / e.total) * 100;
      prog.firstChild.style.width = pct + "%";
      progText.textContent = pct < 100 ? `上传中 ${pct.toFixed(0)}%（${fmtSize(e.loaded)} / ${fmtSize(e.total)}）` : "上传完成，创建任务…";
    };
    xhr.onload = () => {
      btn.disabled = false;
      let body = {};
      try { body = JSON.parse(xhr.responseText); } catch { /* 非 JSON */ }
      if (xhr.status >= 200 && xhr.status < 300) { location.hash = "#/job/" + body.id; return; }
      prog.hidden = true; progText.textContent = "";
      err.replaceChildren(h("div", { class: "alert err" }, body.detail || xhr.responseText || `提交失败 (HTTP ${xhr.status})`));
    };
    xhr.onerror = () => {
      btn.disabled = false; prog.hidden = true; progText.textContent = "";
      err.replaceChildren(h("div", { class: "alert err" }, "网络错误：无法连接服务"));
    };
    xhr.send(fd);
  }
  return form;
}

function sameAsDefault(v, d) {
  if (Array.isArray(v)) return JSON.stringify(v) === JSON.stringify(d || []);
  if (typeof v === "boolean") return v === !!d;
  if (v !== "" && d !== null && d !== undefined && !Number.isNaN(Number(v)) && typeof d === "number") return Number(v) === d;
  return String(v ?? "") === String(d ?? "");
}

function guessFormat(detect, names) {
  const one = (n) => {
    if (!n) return null;
    const low = n.toLowerCase();
    for (const [pat, fmt] of Object.entries(detect.patterns || {})) {
      const re = new RegExp("^" + pat.toLowerCase().replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*").replace(/\?/g, ".") + "$");
      if (re.test(low)) return fmt;
    }
    for (const [ext, fmt] of Object.entries(detect.extensions || {})) if (low.endsWith(ext)) return fmt;
    return null;
  };
  const got = names.map(one).filter(Boolean);
  return got[0] || detect.fallback;
}

// 把历史任务的参数转成表单初值：文件参数改成「服务器路径」模式
function fromJobParams(tool, params) {
  const out = {};
  for (const p of tool.params) {
    const v = params[p.name];
    if (v === undefined || v === null) continue;
    out[p.name] = p.type === "file" ? { mode: "path", path: v } : v;
  }
  return out;
}

function makeWidget(p, savedValue, tool) {
  const initial = savedValue !== undefined ? savedValue : p.default;
  const w = { value: () => null, append: () => {}, save: () => undefined, fileName: () => null };

  if (p.type === "file") {
    const st = (savedValue && typeof savedValue === "object") ? savedValue : { mode: "upload", path: "" };
    let mode = st.mode || "upload";
    const fileInput = h("input", { type: "file", accept: (p.accept || []).join(",") || null });
    const pathInput = h("input", { type: "text", value: st.path || "", placeholder: "服务器上的路径，如 export/xxx_model_output.json" });
    const browse = h("button", { class: "btn", type: "button", onclick: async () => {
      const got = await openBrowser({ mode: "file", accept: p.accept, start: pathInput.value });
      if (got) { pathInput.value = got; pathInput.dispatchEvent(new Event("input", { bubbles: true })); }
    } }, "浏览…");
    const upBox = h("div", {}, fileInput);
    const pathBox = h("div", { class: "pathrow" }, pathInput, browse);
    const bUp = h("button", { type: "button" }, "上传文件");
    const bPath = h("button", { type: "button" }, "服务器路径");
    const sync = () => {
      bUp.classList.toggle("on", mode === "upload"); bPath.classList.toggle("on", mode === "path");
      upBox.hidden = mode !== "upload"; pathBox.hidden = mode !== "path";
    };
    bUp.onclick = () => { mode = "upload"; sync(); bUp.dispatchEvent(new Event("change", { bubbles: true })); };
    bPath.onclick = () => { mode = "path"; sync(); bPath.dispatchEvent(new Event("change", { bubbles: true })); };
    sync();
    w.node = h("div", { class: "filebox" }, h("div", { class: "seg" }, bUp, bPath), upBox, pathBox);
    w.value = () => (mode === "upload" ? fileInput.files[0] : pathInput.value.trim());
    w.fileName = () => (mode === "upload" ? (fileInput.files[0] || {}).name : pathInput.value.trim().split(/[\\/]/).pop()) || null;
    w.append = (fd) => {
      if (mode === "upload" && fileInput.files[0]) fd.append(p.name, fileInput.files[0]);
      else if (mode === "path" && pathInput.value.trim()) fd.append(p.name + "__path", pathInput.value.trim());
    };
    w.save = () => ({ mode, path: pathInput.value.trim() });
    return w;
  }
  if (p.type === "dir") {
    const input = h("input", { type: "text", value: initial || "", placeholder: p.placeholder || "目录路径，如 experiments/<实验名>" });
    const browse = h("button", { class: "btn", type: "button", onclick: async () => {
      const got = await openBrowser({ mode: "dir", start: input.value });
      if (got) { input.value = got; input.dispatchEvent(new Event("input", { bubbles: true })); }
    } }, "浏览…");
    w.node = h("div", { class: "pathrow" }, input, browse);
    w.value = () => input.value.trim();
    w.append = (fd) => fd.append(p.name, input.value.trim());
    w.save = () => input.value.trim();
    return w;
  }
  if (p.type === "bool") {
    const input = h("input", { type: "checkbox", checked: !!initial });
    w.node = h("label", { class: "check" }, input, p.label);
    w.value = () => input.checked;
    w.append = (fd) => fd.append(p.name, input.checked ? "true" : "false");
    w.save = () => input.checked;
    return w;
  }
  if (p.type === "choice") {
    const sel = h("select", {}, p.choices.map((c) => h("option", { value: c.value, selected: String(initial ?? "") === c.value }, c.label)));
    w.hint = h("div", { class: "help" });
    w.node = h("div", {}, sel, w.hint);
    w.value = () => sel.value;
    w.append = (fd) => fd.append(p.name, sel.value);
    w.save = () => sel.value;
    return w;
  }
  if (p.type === "multi") {
    const cur = new Set(Array.isArray(initial) ? initial : []);
    const boxes = p.choices.map((c) => h("input", { type: "checkbox", value: c.value, checked: cur.has(c.value) }));
    w.node = h("div", { class: "multi" }, p.choices.map((c, i) => h("label", {}, boxes[i], c.label)));
    w.value = () => boxes.filter((b) => b.checked).map((b) => b.value);
    w.append = (fd) => boxes.filter((b) => b.checked).forEach((b) => fd.append(p.name, b.value));
    w.save = w.value;
    return w;
  }
  const input = p.type === "text"
    ? h("textarea", { placeholder: p.placeholder || null, rows: 3 })
    : h("input", { type: (p.type === "int" || p.type === "float") ? "number" : "text", step: p.type === "float" ? "any" : p.type === "int" ? "1" : null, placeholder: p.placeholder || null });
  input.value = initial ?? "";
  w.node = input;
  w.value = () => input.value;
  w.append = (fd) => fd.append(p.name, input.value);
  w.save = () => input.value;
  return w;
}

// ------------------------------------------------------------------ server file browser
function openBrowser({ mode, accept, start }) {
  const dlg = $("#browser"), list = $("#browser-list"), pathEl = $("#browser-path"), filter = $("#browser-filter");
  const exts = (accept || []).map((e) => e.toLowerCase());
  return new Promise((resolve) => {
    let current = null, entries = [];
    const finish = (v) => { dlg.close(); resolve(v); };
    dlg.onclose = () => resolve(null);
    filter.value = "";
    filter.oninput = () => draw();
    async function go(path) {
      try {
        const res = await api("/api/fs/list" + (path ? "?path=" + encodeURIComponent(path) : ""));
        current = res; entries = res.entries; draw();
      } catch (e) {
        if (path) return go(null);
        list.replaceChildren(h("li", {}, String(e.message)));
      }
    }
    function draw() {
      const f = filter.value.trim().toLowerCase();
      pathEl.replaceChildren(current.path ? current.path : "数据目录", " ",
        mode === "dir" && current.path ? h("button", { class: "btn sm primary", onclick: () => finish(current.path) }, "选择此目录") : "");
      const items = [];
      if (current.path) items.push(h("li", { class: "dir", onclick: () => go(current.parent || null) }, h("span", { class: "nm" }, "..（上一级）")));
      for (const e of entries) {
        if (f && !e.name.toLowerCase().includes(f)) continue;
        if (!e.dir && mode === "dir") continue;
        const dim = !e.dir && exts.length && !exts.some((x) => e.name.toLowerCase().endsWith(x));
        items.push(h("li", { class: (e.dir ? "dir" : "file") + (dim ? " dim" : ""), onclick: () => (e.dir ? go(e.path) : finish(e.path)) },
          h("span", { class: "nm", title: e.path }, e.name),
          e.dir ? null : h("span", { class: "muted small" }, fmtSize(e.size)),
          e.mtime ? h("span", { class: "muted small" }, fmtTime(e.mtime)) : null));
      }
      list.replaceChildren(...items);
    }
    // 从已填路径所在目录、或上次浏览的目录开始
    const startDir = start ? start.replace(/[\\/][^\\/]*\.[^\\/]*$/, "") : null;
    go(startDir || store.get("browser:last", null));
    dlg.addEventListener("close", () => { if (current && current.path) store.set("browser:last", current.path); }, { once: true });
    dlg.showModal();
  });
}

// ------------------------------------------------------------------ job page
async function renderJob(app, jobId, qs) {
  let job = await api("/api/jobs/" + jobId);
  const tool = S.tools[job.tool_id] || { params: [], view: "text", title: job.title };
  const head = h("div", { class: "card" });
  const progressBox = h("div");
  const logPre = h("pre", { class: "log mono" });
  const logBox = h("details", { class: "logbox", open: job.status !== "succeeded" }, h("summary", {}, "运行日志"), logPre);
  const resultBox = h("div");
  app.append(head, progressBox, h("div", { class: "card" }, logBox), resultBox);

  function drawHead() {
    const labels = Object.fromEntries(tool.params.map((p) => [p.name, p.label]));
    const params = Object.entries(job.display_params || {});
    const running = job.status === "queued" || job.status === "running";
    head.replaceChildren(
      h("div", { class: "jobhead" },
        h("div", {}, h("div", { class: "muted small" }, groupTitle(job.group)), h("h1", {}, job.title)),
        statusBadge(job.status),
        h("span", { class: "spacer" }),
        running ? h("button", { class: "btn danger", onclick: async () => { await api(`/api/jobs/${jobId}/cancel`, { method: "POST" }); } }, "取消任务") : null,
        h("a", { class: "btn", href: `#/g/${job.group}/${job.tool_id}?from=${jobId}` }, "用相同参数再跑"),
      ),
      h("dl", { class: "kv", style: "margin-top:12px" },
        h("dt", {}, "任务 ID"), h("dd", { class: "mono" }, job.id),
        h("dt", {}, "提交时间"), h("dd", {}, fmtTime(job.created)),
        job.duration !== null ? [h("dt", {}, "耗时"), h("dd", {}, fmtDur(job.duration))] : null,
        params.map(([k, v]) => [h("dt", {}, labels[k] || k), h("dd", { class: "mono" }, v)]),
      ),
    );
  }
  function drawProgress() {
    if (job.status === "queued" || job.status === "running") {
      const pct = job.progress;
      progressBox.replaceChildren(h("div", { class: "card" },
        h("div", { class: "row", style: "margin-bottom:8px" }, h("strong", {}, job.message || STATUS[job.status]), h("span", { class: "spacer" }),
          h("span", { class: "muted small" }, pct !== null && pct !== undefined ? pct.toFixed(0) + "%" : "")),
        h("div", { class: "progress" + (pct === null || pct === undefined ? " indeterminate" : "") }, h("div", { style: `width:${pct || 0}%` })),
        h("p", { class: "muted small", style: "margin:8px 0 0" }, "任务在后台进程中执行，可以离开本页，稍后在「任务记录」里查看。")));
    } else if (job.status !== "succeeded") {
      progressBox.replaceChildren(h("div", { class: "alert err" }, `${STATUS[job.status]}：${job.error || ""}`));
    } else {
      progressBox.replaceChildren();
    }
  }

  let logOffset = 0;
  async function pullLog() {
    for (let i = 0; i < 20; i++) {   // 一次最多读 20 块，避免巨型日志卡住
      const res = await api(`/api/jobs/${jobId}/log?offset=${logOffset}`);
      if (res.text) {
        const nearBottom = logPre.scrollHeight - logPre.scrollTop - logPre.clientHeight < 40;
        logPre.append(res.text);
        if (nearBottom) logPre.scrollTop = logPre.scrollHeight;
      }
      const more = res.offset < res.size;
      logOffset = res.offset;
      if (!more) break;
    }
  }

  drawHead(); drawProgress();
  let finished = false;
  const stop = poll(async () => {
    job = await api("/api/jobs/" + jobId);
    drawHead(); drawProgress();
    await pullLog();
    if (["succeeded", "failed", "cancelled", "interrupted"].includes(job.status) && !finished) {
      finished = true; stop();
      await pullLog();
      if (job.status === "succeeded") {
        logBox.open = false;
        const view = { diff: renderDiffResult, report: renderReportResult, text: renderTextResult }[tool.view] || renderTextResult;
        await view(resultBox, job, qs);
      } else {
        logBox.open = true;
      }
    }
  }, 1000);
}

// ---- 产物列表
async function artifactButtons(job, prefer) {
  const files = await api(`/api/jobs/${job.id}/files`);
  const order = (f) => { const i = prefer.findIndex((p) => p[0] === f.path); return i < 0 ? 999 : i; };
  files.sort((a, b) => order(a) - order(b) || a.path.localeCompare(b.path));
  return h("div", { class: "artifacts" }, files.map((f) => {
    const label = (prefer.find((p) => p[0] === f.path) || [])[1];
    const isHtml = f.path.endsWith(".html");
    const href = `/api/jobs/${job.id}/files/${encodeURI(f.path)}` + (isHtml ? "" : "?download=1");
    return h("a", { class: "btn" + (label && label.includes("⭐") ? " primary" : ""), href, target: isHtml ? "_blank" : null, title: fmtSize(f.size) },
      label ? `${label}` : f.path, h("span", { class: "muted small" }, isHtml ? "打开" : fmtSize(f.size)));
  }));
}

// ---- view: report (exp_analyzer)
async function renderReportResult(box, job) {
  const r = job.result || {};
  const kpis = r.kpis || {};
  const kpiRows = Object.entries(kpis);
  box.append(h("div", { class: "card" },
    h("h2", {}, r.experiment_name || "分析结果"),
    r.description ? h("p", { class: "muted" }, r.description) : null,
    r.entry_point ? h("p", { class: "small" }, `入口：${r.entry_point.type || ""} `, h("code", {}, r.entry_point.code || ""), `，方向 ${r.entry_point.direction || ""}`) : null,
    kpiRows.length ? h("div", { class: "tablewrap", style: "margin:12px 0" }, h("table", {},
      h("thead", {}, h("tr", {}, ["KPI", "baseline", "modified", "Δ", "Δ%"].map((t) => h("th", {}, t)))),
      h("tbody", {}, kpiRows.map(([k, v]) => h("tr", {},
        h("td", {}, k), h("td", { class: "num" }, fmtNum(v.base)), h("td", { class: "num" }, fmtNum(v.mod)),
        h("td", { class: "num" }, fmtSigned(v.delta)), h("td", { class: "num" }, v.pct === null ? "" : fmtSigned(v.pct) + "%")))))) : null,
    h("h3", {}, "产物"),
    await artifactButtons(job, [
      ["delta_chain_minimal.html", "⭐ 主因果故事 delta_chain_minimal"], ["delta_chain.html", "delta_chain（更宽）"],
      ["critical_path.html", "critical_path 关键路径"], ["chain_graph.html", "chain_graph 完整邻域"],
      ["report.md", "report.md"], ["report.json", "report.json"], ["obj_contributions.csv", "R2 obj_contributions.csv"],
      ["block_activation.csv", "R5 block_activation.csv"], ["patch.json", "patch.json"]]),
    h("p", { class: "muted small" }, "因果图使用 CDN 加载 mermaid，需要能访问外网。")));
  const md = h("div", { class: "card md" }, h("div", { class: "muted" }, "加载报告…"));
  box.append(md);
  try { md.innerHTML = await api(`/api/jobs/${job.id}/markdown/report.md`); }
  catch (e) { md.replaceChildren(h("div", { class: "alert warn" }, "report.md 渲染失败：" + e.message)); }
}

// ---- view: text (diagnose)
async function renderTextResult(box, job) {
  const r = job.result || {};
  const file = r.output || "output.txt";
  let text = "";
  try { text = await api(`/api/jobs/${job.id}/files/${file}`); } catch (e) { text = "（没有输出文件：" + e.message + "）"; }
  const lines = String(text).split(/\r?\n/);
  const search = h("input", { type: "text", placeholder: "搜索 / 过滤（例如物料 code）" });
  const only = h("input", { type: "checkbox" });
  const pre = h("pre", { class: "output mono" });
  const count = h("span", { class: "muted small" });
  const isHead = (ln) => /^(=|-){5,}/.test(ln) || /^\[[A-Z0-9]+\]/.test(ln) || /^>>>/.test(ln);
  function draw() {
    const q = search.value.trim().toLowerCase();
    const out = [];
    let shown = 0;
    for (const ln of lines) {
      const hit = !!q && ln.toLowerCase().includes(q);
      const important = ln.includes("⚠") || ln.includes("★") || isHead(ln);
      let show = true;
      if (filterOnly.checked && q) show = hit || isHead(ln);
      if (only.checked) show = show && (important || hit);
      if (!show) continue;
      shown++;
      let html = esc(ln);
      if (q && hit) html = html.replace(new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/[&<>"']/g, (c) => esc(c)), "gi"), (m) => `<mark>${m}</mark>`);
      const cls = isHead(ln) ? "h" : ln.includes("⚠") ? "w" : ln.includes("★") ? "s" : "";
      out.push(cls ? `<span class="${cls}">${html}</span>` : html);
    }
    pre.innerHTML = out.join("\n");
    count.textContent = `${shown} / ${lines.length} 行`;
  }
  const filterOnly = h("input", { type: "checkbox", onchange: draw });
  search.oninput = draw; only.onchange = draw;
  box.append(h("div", { class: "card" },
    h("div", { class: "row", style: "margin-bottom:10px" },
      h("h2", { style: "margin:0" }, "诊断输出"),
      r.warnings !== undefined ? h("span", { class: "badge" }, `⚠ ${r.warnings}`) : null,
      r.highlights !== undefined ? h("span", { class: "badge" }, `★ ${r.highlights}`) : null,
      h("span", { class: "spacer" }),
      h("a", { class: "btn", href: `/api/jobs/${job.id}/files/${file}?download=1` }, "下载 output.txt")),
    h("div", { class: "filters" }, search,
      h("label", { class: "check small" }, filterOnly, "只显示匹配行"),
      h("label", { class: "check small" }, only, "只看标题 / ⚠ / ★ 行"), count),
    pre));
  draw();
}

// ---- view: diff
async function renderDiffResult(box, job, qs) {
  const r = job.result || {};
  const isText = r.format === "text";
  const st = {
    kind: qs.get("kind") || "", section: qs.get("section") || "", field: qs.get("field") || "",
    q: qs.get("q") || "", sort: qs.get("sort") || "id", offset: Number(qs.get("offset") || 0),
    limit: Number(store.get("diff:limit", 200)),
  };
  const syncUrl = () => {
    const p = new URLSearchParams();
    for (const k of ["kind", "section", "field", "q", "sort"]) if (st[k] && !(k === "sort" && st[k] === "id")) p.set(k, st[k]);
    if (st.offset) p.set("offset", st.offset);
    history.replaceState(null, "", `#/job/${job.id}` + (p.toString() ? "?" + p : ""));
  };

  // 概览
  const stats = (r.stats || {});
  const statLine = isText
    ? `旧文件 ${(stats.lines_a ?? 0).toLocaleString()} 行 / 新文件 ${(stats.lines_b ?? 0).toLocaleString()} 行` + (stats.ignored_within_tolerance ? `，容差内忽略 ${stats.ignored_within_tolerance} 行` : "")
    : r.format === "json" ? `比较节点 ${(stats.nodes_compared ?? 0).toLocaleString()} 个`
      : r.format === "mps" ? `A: ${stats.a?.rows} 行 / ${stats.a?.columns} 列 / ${stats.a?.nonzeros} 非零；B: ${stats.b?.rows} / ${stats.b?.columns} / ${stats.b?.nonzeros}`
        : r.format === "pims" ? `目标函数 A ${fmtNum(stats.a?.objective)} → B ${fmtNum(stats.b?.objective)}；单元格 A ${(stats.a?.cells ?? 0).toLocaleString()} / B ${(stats.b?.cells ?? 0).toLocaleString()}` : "";
  const chips = h("div", { class: "stats" });
  const drawChips = () => chips.replaceChildren(
    h("div", { class: "stat click" + (!st.kind ? " on" : ""), onclick: () => { st.kind = ""; st.offset = 0; reload(); } }, h("div", { class: "v" }, (r.total ?? 0).toLocaleString()), h("div", { class: "k" }, "全部差异")),
    ...Object.entries(r.by_kind || {}).map(([k, n]) => h("div", { class: "stat click" + (st.kind === k ? " on" : ""), onclick: () => { st.kind = st.kind === k ? "" : k; st.offset = 0; reload(); } },
      h("div", { class: "v" }, n.toLocaleString()), h("div", { class: "k" }, kindLabel(k)))),
  );
  box.append(h("div", { class: "card" },
    h("div", { class: "row" }, h("h2", { style: "margin:0" }, r.format_title || "对比结果"), h("span", { class: "muted small" }, `用时 ${fmtDur(r.seconds)}`)),
    h("p", { class: "small" }, h("code", {}, r.file_a?.name), ` (${fmtSize(r.file_a?.size)})  →  `, h("code", {}, r.file_b?.name), ` (${fmtSize(r.file_b?.size)})`,
      statLine ? h("span", { class: "muted" }, "　" + statLine) : null),
    r.total === 0 ? h("div", { class: "alert info" }, "两份文件没有差异（在当前忽略规则 / 容差下）。") : null,
    r.truncated ? h("div", { class: "alert warn" }, `差异共 ${r.total.toLocaleString()} 条，只保存了前 ${r.stored.toLocaleString()} 条明细（统计数字是完整的）。可在高级选项里调大「最多保存差异条数」，或增加忽略规则。`) : null,
    chips));
  // 对比器生成的分析摘要（例如 PIMS 解报告对比的 summary.md）
  if (r.summary) {
    const md = h("div", { class: "md" }, h("div", { class: "muted" }, "加载分析摘要…"));
    box.append(h("details", { class: "card", open: true },
      h("summary", { style: "cursor:pointer;font-weight:600" }, "分析摘要",
        h("a", { class: "btn sm", style: "margin-left:12px", href: `/api/jobs/${job.id}/files/${r.summary}?download=1` }, "下载 " + r.summary)),
      md));
    try { md.innerHTML = await api(`/api/jobs/${job.id}/markdown/${r.summary}`); }
    catch (e) { md.replaceChildren(h("div", { class: "alert warn" }, "摘要渲染失败：" + e.message)); }
  }
  if (r.total === 0) return;

  // 维度分布（JSON / MPS）
  const facetBox = h("div", { class: "facets" });
  const hasFacets = Object.keys(r.by_section || {}).length > 1 || Object.keys(r.by_field || {}).length > 1;
  const drawFacets = () => {
    const facet = (title, data, key, total) => h("div", { class: "facet" },
      h("h4", {}, title + (total > Object.keys(data).length ? `（前 ${Object.keys(data).length} / ${total}）` : "")),
      Object.entries(data).map(([name, n]) => h("a", { href: "javascript:void 0", class: st[key] === name ? "on" : "", onclick: () => { st[key] = st[key] === name ? "" : name; st.offset = 0; reload(); } },
        h("span", { title: name }, name), h("span", { class: "muted" }, n.toLocaleString()))));
    facetBox.replaceChildren(
      Object.keys(r.by_section || {}).length ? facet(r.format === "mps" ? "按段 (section)" : r.format === "pims" ? "按报告" : "按顶层块", r.by_section, "section", r.n_sections) : "",
      Object.keys(r.by_field || {}).length ? facet("按字段名", r.by_field, "field", r.n_fields) : "",
    );
  };
  if (hasFacets) box.append(h("div", { class: "card" }, h("h3", {}, "差异分布（点击筛选）"), facetBox));

  // 筛选 + 表格
  const search = h("input", { type: "text", value: st.q, placeholder: isText ? "搜索行内容" : "搜索路径 / 值，例如物料 code 或 marginal_benefit" });
  let tmr = null;
  search.oninput = () => { clearTimeout(tmr); tmr = setTimeout(() => { st.q = search.value.trim(); st.offset = 0; reload(); }, 300); };
  const sortSel = h("select", { onchange: () => { st.sort = sortSel.value; st.offset = 0; reload(); } },
    [["id", "按出现顺序"], ["absdelta", "按 |Δ| 从大到小"], ["rel", "按相对差从大到小"], ["path", "按路径"]]
      .filter(([v]) => !(isText && (v === "rel" || v === "path")))
      .map(([v, l]) => h("option", { value: v, selected: st.sort === v }, l)));
  const limitSel = h("select", { onchange: () => { st.limit = Number(limitSel.value); store.set("diff:limit", st.limit); st.offset = 0; reload(); } },
    [100, 200, 500, 1000].map((n) => h("option", { value: n, selected: st.limit === n }, `${n} 条/页`)));
  const activeTags = h("span", { class: "row" });
  const exportBtn = h("a", { class: "btn" }, "导出 CSV（当前筛选）");
  const tableBox = h("div");
  const pager = h("div", { class: "pager" });
  box.append(h("div", { class: "card" },
    h("div", { class: "filters" }, search, sortSel, limitSel, activeTags, h("span", { class: "spacer" }), exportBtn),
    tableBox, pager));

  function filterQS() {
    const p = new URLSearchParams();
    for (const k of ["kind", "section", "field", "q"]) if (st[k]) p.set(k, st[k]);
    if (st.sort !== "id") p.set("sort", st.sort);
    return p;
  }
  async function reload() {
    syncUrl(); drawChips(); if (hasFacets) drawFacets();
    activeTags.replaceChildren(...["kind", "section", "field"].filter((k) => st[k]).map((k) =>
      h("button", { class: "btn sm", onclick: () => { st[k] = ""; st.offset = 0; reload(); } }, `${{ kind: "类型", section: "顶层", field: "字段" }[k]}: ${k === "kind" ? kindLabel(st[k]) : st[k]} ✕`)));
    exportBtn.href = `/api/jobs/${job.id}/diff/export.csv?` + filterQS();
    const p = filterQS(); p.set("offset", st.offset); p.set("limit", st.limit);
    tableBox.replaceChildren(h("div", { class: "muted" }, "查询中…"));
    const res = await api(`/api/jobs/${job.id}/diff/items?` + p);
    tableBox.replaceChildren(isText ? textTable(res.items) : pathTable(res.items));
    const pages = Math.max(1, Math.ceil(res.total / st.limit));
    const page = Math.floor(st.offset / st.limit) + 1;
    pager.replaceChildren(
      h("span", { class: "muted small" }, `筛选结果 ${res.total.toLocaleString()} 条，第 ${page} / ${pages} 页`),
      h("button", { class: "btn sm", disabled: page <= 1, onclick: () => { st.offset = 0; reload(); } }, "« 首页"),
      h("button", { class: "btn sm", disabled: page <= 1, onclick: () => { st.offset = Math.max(0, st.offset - st.limit); reload(); } }, "‹ 上一页"),
      h("button", { class: "btn sm", disabled: page >= pages, onclick: () => { st.offset += st.limit; reload(); } }, "下一页 ›"),
      h("button", { class: "btn sm", disabled: page >= pages, onclick: () => { st.offset = (pages - 1) * st.limit; reload(); } }, "末页 »"),
    );
  }
  function valueCells(old, neu) {
    if (old === null || neu === null) {
      return [h("td", { class: "val old" }, old === null ? h("span", { class: "absent" }, "（无）") : old),
        h("td", { class: "val new" }, neu === null ? h("span", { class: "absent" }, "（无）") : neu)];
    }
    const [a, b] = highlight(old, neu);
    return [h("td", { class: "val old", html: a }), h("td", { class: "val new", html: b })];
  }
  function pathTable(items) {
    return h("div", { class: "tablewrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ["#", "类型", "路径", "旧值", "新值", "Δ"].map((t) => h("th", {}, t)))),
      h("tbody", {}, items.map((it) => h("tr", {},
        h("td", { class: "num muted" }, it.id), h("td", { class: "nowrap" }, kindLabel(it.kind)),
        h("td", { class: "path", title: it.note || "" }, it.path),
        ...valueCells(it.old, it.new),
        h("td", { class: "num" }, it.delta === null ? "" : fmtSigned(it.delta)))))));
  }
  function textTable(items) {
    return h("div", { class: "tablewrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ["类型", "旧行号", "新行号", "旧内容", "新内容", "Δ"].map((t) => h("th", {}, t)))),
      h("tbody", {}, items.map((it) => h("tr", {},
        h("td", { class: "nowrap" }, kindLabel(it.kind)), h("td", { class: "num" }, it.line_a ?? ""), h("td", { class: "num" }, it.line_b ?? ""),
        ...valueCells(it.old, it.new),
        h("td", { class: "num" }, it.delta === null ? "" : fmtNum(it.delta)))))));
  }
  await reload();
}

// 行内高亮：标出两段文本中间不同的部分
function highlight(a, b) {
  a = String(a); b = String(b);
  let p = 0;
  while (p < a.length && p < b.length && a[p] === b[p]) p++;
  let s = 0;
  while (s < a.length - p && s < b.length - p && a[a.length - 1 - s] === b[b.length - 1 - s]) s++;
  const wrap = (str, cls) => {
    const mid = str.slice(p, str.length - s);
    return esc(str.slice(0, p)) + (mid ? `<mark class="${cls}">${esc(mid)}</mark>` : "") + esc(str.slice(str.length - s));
  };
  return [wrap(a, "o"), wrap(b, "n")];
}

// ------------------------------------------------------------------ boot
(async function boot() {
  try {
    S.meta = await api("/api/meta");
    for (const t of S.meta.tools) S.tools[t.id] = t;
  } catch (e) {
    $("#app").replaceChildren(h("div", { class: "alert err" }, "无法连接服务：" + e.message));
    return;
  }
  window.addEventListener("hashchange", route);
  route();
})();
