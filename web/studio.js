const state = {
  templates: [],
  template: null,
  project: null,
  renders: [],
};

const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
    ...options,
  });

  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || body.error || res.statusText);
  }

  return res.json();
}

function renderTemplates() {
  $("templates").innerHTML = state.templates
    .map(
      (template) => `
        <button class="card template ${state.template?.id === template.id ? "active" : ""}" data-id="${template.id}">
          <h3>${template.name}</h3>
          <p>${template.description}</p>
          <p class="muted">${template.defaultOutput.width}×${template.defaultOutput.height}</p>
        </button>
      `,
    )
    .join("");

  document.querySelectorAll(".template").forEach((button) => {
    button.onclick = () => selectTemplate(button.dataset.id);
  });
}

async function selectTemplate(id) {
  state.template = state.templates.find((template) => template.id === id);
  renderTemplates();

  state.project = await api("/api/projects", {
    method: "POST",
    body: JSON.stringify({ templateId: id }),
  });

  $("fields").innerHTML = state.template.fields
    .map((field) => {
      const required = field.required ? "required" : "";
      const maxLength = field.maxLength || "";
      const input =
        field.type === "textarea"
          ? `<textarea name="${field.key}" maxlength="${maxLength}" ${required}></textarea>`
          : `<input name="${field.key}" maxlength="${maxLength}" ${required}>`;
      return `<label>${field.label}${input}</label>`;
    })
    .join("");

  $("assets").innerHTML =
    "<h3>Assets</h3>" +
    state.template.requiredAssets
      .map(
        (asset) => `
          <label>
            ${asset.label}
            <input type="file" data-asset="${asset.key}" accept="${asset.accept || ""}" ${asset.multiple ? "multiple" : ""}>
          </label>
        `,
      )
      .join("");

  $("player").setAttribute("src", `/api/projects/${state.project.id}/preview`);
  $("player").setAttribute("width", state.template.defaultOutput.width);
  $("player").setAttribute("height", state.template.defaultOutput.height);
  document
    .querySelector(".preview")
    .classList.toggle(
      "vertical",
      state.template.defaultOutput.height > state.template.defaultOutput.width,
    );
}

async function saveDraft() {
  const copy = Object.fromEntries(new FormData($("projectForm")).entries());
  const selectedAssets = [];

  for (const input of document.querySelectorAll("[data-asset]")) {
    for (const file of input.files) {
      const ticket = await api("/api/assets/upload-url", {
        method: "POST",
        body: JSON.stringify({
          projectId: state.project.id,
          key: input.dataset.asset,
          filename: file.name,
          contentType: file.type,
        }),
      });

      await fetch(ticket.uploadUrl, {
        method: "PUT",
        headers: { "Content-Type": file.type },
        body: file,
      });

      selectedAssets.push(ticket.asset);
    }
  }

  state.project = await api(`/api/projects/${state.project.id}`, {
    method: "PATCH",
    body: JSON.stringify({ copy, assets: selectedAssets }),
  });

  $("player").setAttribute(
    "src",
    `/api/projects/${state.project.id}/preview?ts=${Date.now()}`,
  );
  $("status").textContent = "Draft saved.";
}

async function refreshRender(id) {
  const render = await api(`/api/renders/${id}`);
  state.renders = [render, ...state.renders.filter((item) => item.id !== id)];
  drawHistory();
  return render;
}

function drawHistory() {
  $("history").innerHTML = state.renders
    .map(
      (render) => `
        <div class="row">
          <span>${render.templateId} · ${render.status}</span>
          <span>
            ${render.status === "failed" ? `<button class="secondary" onclick="retry('${render.id}')">Retry</button>` : ""}
            ${render.downloadUrl ? `<a href="${render.downloadUrl}">Download</a>` : ""}
          </span>
        </div>
      `,
    )
    .join("");
}

async function startRender() {
  await saveDraft();

  const render = await api(`/api/projects/${state.project.id}/render`, {
    method: "POST",
  });

  state.renders.unshift(render);
  drawHistory();
  $("status").textContent = "Queued…";

  const timer = setInterval(async () => {
    const latest = await refreshRender(render.id);
    $("status").textContent = latest.status;

    if (["complete", "failed"].includes(latest.status)) {
      clearInterval(timer);
      if (latest.downloadUrl) {
        $("download").hidden = false;
        $("download").href = latest.downloadUrl;
      }
    }
  }, 2500);
}

window.retry = async (id) => {
  const render = await api(`/api/renders/${id}/retry`, { method: "POST" });
  await refreshRender(render.id);
};

$("signin").onclick = async () => {
  try {
    await api("/api/login", {
      method: "POST",
      body: JSON.stringify({ password: $("password").value }),
    });
    $("login").hidden = true;
    $("studio").hidden = false;
    $("logout").hidden = false;
    state.templates = (await api("/api/templates")).templates;
    renderTemplates();
  } catch (error) {
    $("loginStatus").textContent = error.message;
  }
};

$("logout").onclick = async () => {
  await api("/api/logout", { method: "POST" });
  location.reload();
};

$("projectForm").onsubmit = async (event) => {
  event.preventDefault();
  await saveDraft();
};

$("render").onclick = startRender;
