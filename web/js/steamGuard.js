let steamGuardCode = '';
let steamGuardPeriod = 30;
let steamGuardServerOffsetMs = 0;
let steamGuardTimer = null;
let steamGuardVisible = false;
let steamGuardSlice = null;
let steamGuardHideTimer = null;
let selectedSteamGuardAccountId = '';

function updateSteamGuardDisplay() {
  const textEl = el("steam-code-text");
  const subEl = el("steam-code-subtitle");
  const tipEl = el("steam-token-tip");
  const ringEl = el("steam-progress-circle");
  if (!textEl || !subEl || !tipEl || !ringEl) return;
  if (!steamGuardVisible) {
    textEl.textContent = "-----";
    subEl.textContent = "点击显示令牌";
    tipEl.textContent = "您的账号受到 Steam Guard 保护";
    ringEl.style.opacity = "0";
  } else {
    textEl.textContent = steamGuardCode || "-----";
    subEl.textContent = "点击复制";
    tipEl.textContent = "令牌将在倒计时结束后自动刷新";
    ringEl.style.opacity = "1";
  }
}

async function renderSteamGuardAccountsDropdown() {
  const sel = el("steam-guard-account-select");
  if (!sel) return;
  try {
    let accs = accountsCache;
    let curId = accountsCurrentId;
    if (!accs || accs.length === 0) {
      const d = await fetchJson(API + "/accounts");
      accs = d.accounts || [];
      curId = d.current_id || null;
      accountsCache = accs;
      accountsCurrentId = curId;
    }
    sel.innerHTML = "";
    if (accs.length === 0) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "默认/全局令牌";
      sel.appendChild(opt);
    } else {
      accs.forEach((a) => {
        const opt = document.createElement("option");
        opt.value = a.id;
        const name = a.display_name || a.username || a.steam_id || a.id;
        const isCur = a.id === curId;
        const hasSecret = !!a.shared_secret;
        opt.textContent = `${name}${isCur ? " (当前)" : ""}${hasSecret ? "" : " [未配专属密钥]"}`;
        sel.appendChild(opt);
      });
    }

    if (!selectedSteamGuardAccountId) {
      if (curId && accs.some((a) => a.id === curId)) {
        selectedSteamGuardAccountId = curId;
      } else if (accs.length > 0) {
        selectedSteamGuardAccountId = accs[0].id;
      }
    }
    if (selectedSteamGuardAccountId && Array.from(sel.options).some((o) => o.value === selectedSteamGuardAccountId)) {
      sel.value = selectedSteamGuardAccountId;
    }
  } catch (e) {
    // ignore
  }
}

async function refreshSteamGuardCode() {
  try {
    const q = selectedSteamGuardAccountId ? `?account_id=${encodeURIComponent(selectedSteamGuardAccountId)}` : "";
    const d = await fetchJson(API + "/steam_guard" + q);
    if (!d.ok) {
      throw new Error(d.error || "获取失败");
    }
    steamGuardCode = d.code || "";
    const serverTs = d.server_time || Math.floor(Date.now() / 1000);
    const period = d.period || 30;
    steamGuardPeriod = period;
    const nowMs = Date.now();
    steamGuardServerOffsetMs = serverTs * 1000 - nowMs;
    steamGuardSlice = Math.floor(serverTs / steamGuardPeriod);
    updateSteamGuardDisplay();
  } catch (e) {
    steamGuardCode = "";
    updateSteamGuardDisplay();
    toast("获取令牌失败", e.message || "");
  }
}

function startSteamGuardTimer() {
  if (steamGuardTimer) return;
  steamGuardTimer = setInterval(() => {
    const ringEl = el("steam-progress-circle");
    if (!ringEl) return;
    const now = Date.now();
    const serverNow = now + steamGuardServerOffsetMs;
    const periodMs = steamGuardPeriod * 1000;
    if (periodMs <= 0) return;
    const phaseMs = ((serverNow % periodMs) + periodMs) % periodMs;
    const radius = parseFloat(ringEl.getAttribute("r") || "0");
    if (!radius) return;
    const circumference = 2 * Math.PI * radius;
    const currentSlice = Math.floor(serverNow / 1000 / steamGuardPeriod);
    if (steamGuardSlice == null) steamGuardSlice = currentSlice;
    if (currentSlice !== steamGuardSlice) {
      steamGuardSlice = currentSlice;
      refreshSteamGuardCode();
    }
    if (!steamGuardVisible) return;
    ringEl.style.strokeDasharray = circumference + " " + circumference;
    ringEl.style.strokeDashoffset = String(circumference * (phaseMs / periodMs));
  }, 50);
}

function stopSteamGuardTimer() {
  if (steamGuardTimer) {
    clearInterval(steamGuardTimer);
    steamGuardTimer = null;
  }
}

function copySteamGuardCode() {
  const code = steamGuardCode || "";
  if (!code) return;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard
      .writeText(code)
      .then(() => {
        toast("已复制到剪贴板");
      })
      .catch(() => {
        const ta = document.createElement("textarea");
        ta.value = code;
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        ta.remove();
        toast("已复制到剪贴板");
      });
  } else {
    const ta = document.createElement("textarea");
    ta.value = code;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
    toast("已复制到剪贴板");
  }
}

function handleSteamGuardClick() {
  if (!steamGuardVisible) {
    if (steamGuardHideTimer) {
      clearTimeout(steamGuardHideTimer);
      steamGuardHideTimer = null;
    }
    steamGuardVisible = true;
    if (!steamGuardCode) {
      refreshSteamGuardCode();
    } else {
      updateSteamGuardDisplay();
    }
    startSteamGuardTimer();
    steamGuardHideTimer = setTimeout(() => {
      steamGuardVisible = false;
      steamGuardHideTimer = null;
      updateSteamGuardDisplay();
    }, 5000);
  } else {
    copySteamGuardCode();
  }
}

let steamGuardEventsBound = false;
function bindSteamGuardEvents() {
  if (steamGuardEventsBound) return;
  const sel = el("steam-guard-account-select");
  if (sel) {
    sel.addEventListener("change", (e) => {
      selectedSteamGuardAccountId = e.target.value;
      steamGuardCode = "";
      steamGuardSlice = null;
      refreshSteamGuardCode();
    });
  }
  steamGuardEventsBound = true;
}

async function initSteamGuardPanel() {
  bindSteamGuardEvents();
  await renderSteamGuardAccountsDropdown();
  updateSteamGuardDisplay();
  if (!steamGuardCode) {
    refreshSteamGuardCode();
  }
  if (steamGuardVisible) {
    startSteamGuardTimer();
  }
}
