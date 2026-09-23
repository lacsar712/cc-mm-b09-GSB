const tokenKey = "methane_token";
let token = localStorage.getItem(tokenKey) || "";
let role = localStorage.getItem("methane_role") || "";
let currentPage = "records";

const loginBox = document.querySelector("#login");
const appBox = document.querySelector("#app");
const rows = document.querySelector("#rows");
const live = document.querySelector("#live");
const form = document.querySelector("#form");
const tabs = document.querySelector("#tabs");

function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleString("zh-CN", { hour12: false });
}

function paint(list) {
  rows.innerHTML = list
    .map(
      (r) =>
        `<tr><td>${r.site}</td><td>${r.ch4_pct}</td><td class="${r.level === "报警" ? "alarm" : "ok"}">${r.level}</td><td>${r.note}</td></tr>`,
    )
    .join("");
}

function paintLive(data) {
  document.querySelector("#threshold").value = data.threshold;
  const tbody = document.querySelector("#live-rows");
  if (data.pairs.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" class="muted">当前门槛下没有相邻粘滞对</td></tr>`;
    return;
  }
  tbody.innerHTML = data.pairs
    .map(
      (p) =>
        `<tr><td>${p.site}</td><td>${p.prev_reading_id}</td><td>${p.curr_reading_id}</td>` +
        `<td>${p.prev_ch4_pct}</td><td>${p.curr_ch4_pct}</td><td class="alarm">${p.diff}</td></tr>`,
    )
    .join("");
}

function paintLedger(data) {
  const tbody = document.querySelector("#ledger-rows");
  if (data.entries.length === 0) {
    tbody.innerHTML = `<tr><td colspan="9" class="muted">可疑册还是空的</td></tr>`;
    return;
  }
  tbody.innerHTML = data.entries
    .map(
      (e) =>
        `<tr><td>${e.site}</td><td class="alarm">${e.threshold}</td><td>${e.prev_reading_id}</td><td>${e.curr_reading_id}</td>` +
        `<td>${e.prev_ch4_pct ?? ""}</td><td>${e.curr_ch4_pct ?? ""}</td><td class="alarm">${e.diff}</td>` +
        `<td>${e.created_by}</td><td>${fmtTime(e.detected_at)}</td></tr>`,
    )
    .join("");
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(options.headers || {}),
    },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || "请求失败");
  return data;
}

async function refreshPage() {
  if (currentPage === "records") {
    paint(await api("/api/readings"));
  } else if (currentPage === "live") {
    paintLive(await api("/api/sticky/live"));
  } else if (currentPage === "ledger") {
    paintLedger(await api("/api/sticky/ledger"));
  }
}

function switchPage(page) {
  currentPage = page;
  for (const btn of tabs.querySelectorAll("button")) {
    btn.classList.toggle("on", btn.dataset.page === page);
  }
  for (const name of ["records", "live", "ledger"]) {
    document.querySelector(`#page-${name}`).hidden = name !== page;
  }
  refreshPage().catch((err) => {
    live.textContent = err.message;
  });
}

function showApp() {
  loginBox.hidden = true;
  appBox.hidden = false;
  tabs.hidden = false;
  document.querySelector("#who").textContent = role === "writer" ? "检查员" : "查看（只读）";
  document.querySelector("#out").hidden = false;
  form.hidden = role !== "writer";

  const thresholdInput = document.querySelector("#threshold");
  const saveBtn = document.querySelector("#save-threshold");
  const hint = document.querySelector("#threshold-hint");
  if (role === "writer") {
    thresholdInput.disabled = false;
    saveBtn.hidden = false;
    hint.textContent = "";
  } else {
    thresholdInput.disabled = true;
    saveBtn.hidden = true;
    hint.textContent = "只读登录不可改门槛";
  }

  connect();
  switchPage("records");
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/alerts`);
  ws.onmessage = (ev) => {
    const row = JSON.parse(ev.data);
    live.textContent = `刚推送：${row.site} ${row.level}${row.sticky ? "（检出粘滞，已入可疑册）" : ""}`;
    refreshPage().catch(() => {});
  };
}

document.querySelector("#go").onclick = async () => {
  const data = await api("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({
      username: document.querySelector("#user").value,
      password: document.querySelector("#pass").value,
    }),
  });
  token = data.access_token;
  role = data.role;
  localStorage.setItem(tokenKey, token);
  localStorage.setItem("methane_role", role);
  showApp();
};

form.onsubmit = async (e) => {
  e.preventDefault();
  try {
    await api("/api/readings", {
      method: "POST",
      body: JSON.stringify({
        site: document.querySelector("#site").value,
        ch4_pct: Number(document.querySelector("#ch4").value),
      }),
    });
  } catch (err) {
    live.textContent = err.message;
  }
};

document.querySelector("#save-threshold").onclick = async () => {
  const hint = document.querySelector("#threshold-hint");
  try {
    const value = Number(document.querySelector("#threshold").value);
    await api("/api/sticky/threshold", {
      method: "PUT",
      body: JSON.stringify({ threshold: value }),
    });
    hint.textContent = `门槛已改为 ${value}，现场可疑已重算`;
    await refreshPage();
  } catch (err) {
    hint.textContent = err.message;
  }
};

tabs.addEventListener("click", (e) => {
  if (e.target.dataset && e.target.dataset.page) {
    switchPage(e.target.dataset.page);
  }
});

document.querySelector("#out").onclick = () => {
  localStorage.clear();
  location.reload();
};

if (token) showApp();
