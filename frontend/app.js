const tokenKey = "methane_token";
let token = localStorage.getItem(tokenKey) || "";
let role = localStorage.getItem("methane_role") || "";
let currentPage = "records";
let liveData = null;

const loginBox = document.querySelector("#login");
const appBox = document.querySelector("#app");
const rows = document.querySelector("#rows");
const liveRows = document.querySelector("#live-rows");
const archiveRows = document.querySelector("#archive-rows");
const live = document.querySelector("#live");
const form = document.querySelector("#form");
const nav = document.querySelector("#nav");

function pairRow(p, { frozen = false } = {}) {
  return `<tr>
    <td>${p.site}</td>
    <td>#${p.prev_reading_id}</td>
    <td>#${p.curr_reading_id}</td>
    <td>${p.prev_ch4}</td>
    <td>${p.curr_ch4}</td>
    <td>${p.diff}</td>
    ${frozen ? `<td>${p.threshold}</td><td>${p.detected_at.replace("T", " ").slice(0, 19)} UTC</td>` : ""}
  </tr>`;
}

function paint(list) {
  rows.innerHTML = list
    .map(
      (r) =>
        `<tr><td>#${r.id}</td><td>${r.site}</td><td>${r.ch4_pct}</td><td class="${r.level === "报警" ? "alarm" : "ok"}">${r.level}</td><td>${r.note}</td></tr>`,
    )
    .join("");
}

function paintLive(data) {
  liveData = data;
  document.querySelector("#live-threshold").textContent = data.threshold;
  document.querySelector("#threshold").value = data.threshold;
  liveRows.innerHTML = data.pairs.length
    ? data.pairs.map((p) => pairRow(p)).join("")
    : `<tr><td colspan="6" class="muted">当前门槛下没有可疑相邻对</td></tr>`;
}

function paintArchive(list) {
  archiveRows.innerHTML = list.length
    ? list.map((p) => pairRow(p, { frozen: true })).join("")
    : `<tr><td colspan="8" class="muted">可疑册为空</td></tr>`;
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
  } else if (currentPage === "archive") {
    paintArchive(await api("/api/sticky/archive"));
  }
}

function showPage(page) {
  currentPage = page;
  document.querySelectorAll(".page").forEach((el) => (el.hidden = true));
  document.querySelector(`#page-${page}`).hidden = false;
  document.querySelectorAll("#nav button").forEach((b) => {
    b.classList.toggle("active", b.dataset.page === page);
  });
  refreshPage().catch((err) => {
    if (page === "live") live.textContent = err.message;
  });
}

function showApp() {
  loginBox.hidden = true;
  appBox.hidden = false;
  nav.hidden = false;
  document.querySelector("#who").textContent = role === "writer" ? "检查员" : "查看";
  document.querySelector("#out").hidden = false;
  form.hidden = role !== "writer";
  const canEdit = role === "writer";
  document.querySelector("#threshold-form").hidden = !canEdit;
  document.querySelector("#threshold-readonly").hidden = canEdit;
  connect();
  showPage("records");
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/alerts`);
  ws.onmessage = (ev) => {
    const row = JSON.parse(ev.data);
    live.textContent = `刚推送：${row.site} ${row.level}`;
    if (currentPage === "records") refreshPage();
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
    document.querySelector("#ch4").value = "";
  } catch (err) {
    live.textContent = err.message;
  }
};

document.querySelector("#threshold-form").onsubmit = async (e) => {
  e.preventDefault();
  const msg = document.querySelector("#threshold-msg");
  try {
    const value = Number(document.querySelector("#threshold").value);
    const data = await api("/api/sticky/threshold", {
      method: "PUT",
      body: JSON.stringify({ threshold: value }),
    });
    msg.textContent = `已保存：${data.threshold}`;
    paintLive(await api("/api/sticky/live"));
  } catch (err) {
    msg.textContent = err.message;
  }
};

document.querySelectorAll("#nav button").forEach((b) => {
  b.onclick = () => showPage(b.dataset.page);
});

document.querySelector("#out").onclick = () => {
  localStorage.clear();
  location.reload();
};

if (token) showApp();
