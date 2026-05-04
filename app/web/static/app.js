function chartTheme() {
  const styles = window.getComputedStyle(document.documentElement);
  return {
    accent: styles.getPropertyValue("--accent").trim() || "#c86b3c",
    accentSoft: styles.getPropertyValue("--accent-soft").trim() || "rgba(200, 107, 60, 0.12)",
    inkSoft: styles.getPropertyValue("--ink-soft").trim() || "#51616d",
    line: styles.getPropertyValue("--line").trim() || "rgba(20, 37, 49, 0.14)",
  };
}

async function renderLineChart(canvasId, endpoint, label) {
  const canvas = document.getElementById(canvasId);
  if (!canvas || typeof Chart === "undefined") {
    return;
  }

  const response = await fetch(endpoint);
  const payload = await response.json();
  const theme = chartTheme();
  new Chart(canvas, {
    type: "line",
    data: {
      labels: payload.labels,
      datasets: [
        {
          label,
          data: payload.data,
          borderColor: theme.accent,
          backgroundColor: theme.accentSoft,
          fill: true,
          pointRadius: 0,
          pointHoverRadius: 4,
          borderWidth: 2.2,
          tension: 0.28,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: {
          labels: {
            color: theme.inkSoft,
          },
        },
      },
      scales: {
        x: {
          ticks: {
            color: theme.inkSoft,
            maxTicksLimit: 8,
          },
          grid: {
            color: theme.line,
          },
        },
        y: {
          ticks: {
            color: theme.inkSoft,
          },
          grid: {
            color: theme.line,
          },
        },
      },
    },
  });
}

let dashboardRefreshHandle = null;
let dashboardRefreshPending = false;

function getDashboardSection() {
  return document.getElementById("dashboard-live");
}

function getDashboardRefreshSeconds() {
  const section = getDashboardSection();
  if (!section) {
    return null;
  }

  const parsed = Number(section.dataset.refreshSeconds || "60");
  if (!Number.isFinite(parsed) || parsed <= 0) {
    return 60;
  }
  return parsed;
}

function scheduleDashboardRefresh() {
  if (dashboardRefreshHandle !== null) {
    window.clearTimeout(dashboardRefreshHandle);
    dashboardRefreshHandle = null;
  }

  const refreshSeconds = getDashboardRefreshSeconds();
  if (refreshSeconds === null) {
    return;
  }

  dashboardRefreshHandle = window.setTimeout(() => {
    void refreshDashboardSection();
  }, Math.max(refreshSeconds, 5) * 1000);
}

async function fetchDashboardFallback(section) {
  const partialUrl = section.dataset.partialUrl || section.getAttribute("hx-get") || "/partials/dashboard";
  const separator = partialUrl.includes("?") ? "&" : "?";
  const response = await fetch(`${partialUrl}${separator}_ts=${Date.now()}`, {
    cache: "no-store",
    headers: {
      "Cache-Control": "no-cache",
      Pragma: "no-cache",
    },
  });
  if (!response.ok) {
    throw new Error(`Dashboard refresh failed with status ${response.status}`);
  }
  section.outerHTML = await response.text();
}

async function refreshDashboardSection() {
  const section = getDashboardSection();
  if (!section || dashboardRefreshPending) {
    return;
  }

  dashboardRefreshPending = true;
  section.dataset.refreshState = "refreshing";
  try {
    if (window.htmx) {
      document.body.dispatchEvent(new CustomEvent("refresh-dashboard", { bubbles: true }));
      return;
    }
    await fetchDashboardFallback(section);
  } catch (error) {
    console.warn("BotYo dashboard refresh failed", error);
  } finally {
    if (!window.htmx) {
      dashboardRefreshPending = false;
      const latestSection = getDashboardSection();
      if (latestSection) {
        latestSection.dataset.refreshState = "idle";
        latestSection.classList.remove("refresh-flash");
        window.requestAnimationFrame(() => latestSection.classList.add("refresh-flash"));
      }
      scheduleDashboardRefresh();
    }
  }
}

function bootstrapDashboardRefresh() {
  if (!getDashboardSection()) {
    return;
  }

  if (window.htmx) {
    document.body.addEventListener("htmx:afterSwap", (event) => {
      if (event.target && event.target.id === "dashboard-live") {
        dashboardRefreshPending = false;
        event.target.dataset.refreshState = "idle";
        event.target.classList.remove("refresh-flash");
        window.requestAnimationFrame(() => event.target.classList.add("refresh-flash"));
        scheduleDashboardRefresh();
      }
    });
    document.body.addEventListener("htmx:responseError", () => {
      dashboardRefreshPending = false;
      const section = getDashboardSection();
      if (section) {
        section.dataset.refreshState = "idle";
      }
      scheduleDashboardRefresh();
    });
    document.body.addEventListener("htmx:sendError", () => {
      dashboardRefreshPending = false;
      const section = getDashboardSection();
      if (section) {
        section.dataset.refreshState = "idle";
      }
      scheduleDashboardRefresh();
    });
  }

  scheduleDashboardRefresh();
}

function setReadyState() {
  window.requestAnimationFrame(() => {
    document.body.classList.add("is-ready");
  });
}

function getAdminFeedbackNode() {
  return document.querySelector("[data-admin-feedback]");
}

function setAdminFeedback(message, isError = false) {
  const node = getAdminFeedbackNode();
  if (!node) {
    return;
  }
  node.textContent = message;
  node.dataset.state = isError ? "error" : "success";
}

async function runAdminAction(button) {
  const endpoint = button.dataset.action;
  if (!endpoint) {
    return;
  }

  const confirmMessage = button.dataset.confirm;
  if (confirmMessage && !window.confirm(confirmMessage)) {
    return;
  }

  const label = button.textContent;
  button.disabled = true;
  setAdminFeedback(`${label} en cours...`);
  try {
    const response = await fetch(endpoint, {
      method: "POST",
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const message = payload.detail?.message || payload.detail || `Erreur ${response.status}`;
      throw new Error(message);
    }
    setAdminFeedback(payload.message || payload.status || "Action executee.");
  } catch (error) {
    setAdminFeedback(error.message || "Action impossible.", true);
  } finally {
    button.disabled = false;
  }
}

function bootstrapAdminActions() {
  const buttons = document.querySelectorAll("[data-action]");
  if (!buttons.length) {
    return;
  }

  buttons.forEach((button) => {
    button.addEventListener("click", () => {
      void runAdminAction(button);
    });
  });
}

document.addEventListener("DOMContentLoaded", () => {
  setReadyState();
  renderLineChart("winrate-chart", "/api/journal/chart/winrate", "Win rate");
  renderLineChart("rr-chart", "/api/journal/chart/rr", "R/R");
  bootstrapDashboardRefresh();
  bootstrapAdminActions();
});
