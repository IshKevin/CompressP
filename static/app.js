(function () {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const loginView = $("#login-view");
  const appView = $("#app-view");
  const loginForm = $("#login-form");
  const loginError = $("#login-error");
  const fileInput = $("#file-input");
  const dropzone = $("#dropzone");
  const uploadList = $("#upload-list");
  const rows = $("#file-rows");
  const listStatus = $("#list-status");

  function fmtSize(bytes) {
    if (bytes < 1024) return bytes + " B";
    const units = ["KB", "MB", "GB", "TB"];
    let n = bytes / 1024;
    let i = 0;
    while (n >= 1024 && i < units.length - 1) {
      n /= 1024;
      i++;
    }
    return n.toFixed(n >= 10 ? 0 : 1) + " " + units[i];
  }

  function fmtDate(unixSeconds) {
    return new Date(unixSeconds * 1000).toLocaleString();
  }

  function showLogin(message) {
    appView.hidden = true;
    loginView.hidden = false;
    loginError.textContent = message || "";
    $("#password").value = "";
    $("#password").focus();
  }

  function showApp() {
    loginView.hidden = true;
    appView.hidden = false;
    loadFiles();
  }

  async function checkSession() {
    try {
      const res = await fetch("/api/session", { credentials: "same-origin" });
      const data = await res.json();
      if (data.authenticated) showApp();
      else showLogin();
    } catch (err) {
      showLogin("Cannot reach the server.");
    }
  }

  loginForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    loginError.textContent = "";
    const password = $("#password").value;
    try {
      const res = await fetch("/api/login", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password }),
      });
      if (res.ok) {
        showApp();
        return;
      }
      const data = await res.json().catch(() => ({}));
      showLogin(data.error || "Login failed.");
    } catch (err) {
      showLogin("Cannot reach the server.");
    }
  });

  $("#logout-btn").addEventListener("click", async () => {
    await fetch("/api/logout", { method: "POST", credentials: "same-origin" });
    showLogin();
  });

  $("#refresh-btn").addEventListener("click", loadFiles);

  async function loadFiles() {
    listStatus.textContent = "Loading...";
    try {
      const res = await fetch("/api/files", { credentials: "same-origin" });
      if (res.status === 401) {
        showLogin("Your session expired. Please log in again.");
        return;
      }
      const data = await res.json();
      renderFiles(data.files);
      listStatus.textContent = data.files.length
        ? data.files.length + " file(s)"
        : "No files yet.";
    } catch (err) {
      listStatus.textContent = "Could not load files.";
    }
  }

  function renderFiles(files) {
    rows.textContent = "";
    for (const f of files) {
      const tr = document.createElement("tr");

      const name = document.createElement("td");
      name.className = "name";
      name.textContent = f.name;

      const size = document.createElement("td");
      size.textContent = fmtSize(f.size);

      const date = document.createElement("td");
      date.textContent = fmtDate(f.uploaded_at);

      const hash = document.createElement("td");
      hash.className = "hash";
      hash.textContent = f.sha256.slice(0, 12) + "...";
      hash.title = f.sha256;

      const actions = document.createElement("td");
      actions.className = "actions";

      const dl = document.createElement("a");
      dl.className = "button";
      dl.href = "/api/files/" + encodeURIComponent(f.id);
      dl.textContent = "Download";

      const del = document.createElement("button");
      del.type = "button";
      del.className = "danger";
      del.textContent = "Delete";
      del.addEventListener("click", () => deleteFile(f));

      actions.append(dl, del);
      tr.append(name, size, date, hash, actions);
      rows.append(tr);
    }
  }

  async function deleteFile(file) {
    if (!confirm('Delete "' + file.name + '"? This cannot be undone.')) return;
    const res = await fetch("/api/files/" + encodeURIComponent(file.id), {
      method: "DELETE",
      credentials: "same-origin",
    });
    if (res.status === 401) return showLogin("Your session expired. Please log in again.");
    if (!res.ok) alert("Delete failed.");
    loadFiles();
  }

  function uploadOne(file, item) {
    return new Promise((resolve) => {
      const xhr = new XMLHttpRequest();
      xhr.open("PUT", "/api/files");
      xhr.withCredentials = true;
      xhr.setRequestHeader("X-Filename", encodeURIComponent(file.name));
      xhr.upload.addEventListener("progress", (e) => {
        if (e.lengthComputable) {
          item.bar.firstChild.style.width = Math.round((e.loaded / e.total) * 100) + "%";
          item.status.textContent = Math.round((e.loaded / e.total) * 100) + "%";
        }
      });
      xhr.addEventListener("load", () => {
        if (xhr.status === 201) {
          item.status.textContent = "Done";
          item.status.className = "";
          resolve(true);
        } else if (xhr.status === 401) {
          item.status.textContent = "Session expired";
          item.status.className = "danger";
          resolve("auth");
        } else {
          let msg = "Failed";
          try { msg = JSON.parse(xhr.responseText).error || msg; } catch (e) {}
          item.status.textContent = msg;
          item.status.className = "danger";
          resolve(false);
        }
      });
      xhr.addEventListener("error", () => {
        item.status.textContent = "Network error";
        item.status.className = "danger";
        resolve(false);
      });
      xhr.send(file);
    });
  }

  async function uploadFiles(fileList) {
    const files = Array.from(fileList);
    if (!files.length) return;

    for (const file of files) {
      const li = document.createElement("li");
      const label = document.createElement("span");
      label.textContent = file.name + " (" + fmtSize(file.size) + ")";
      const status = document.createElement("span");
      status.textContent = "Waiting...";
      const wrap = document.createElement("div");
      wrap.className = "progress";
      const bar = document.createElement("div");
      wrap.append(bar);
      const top = document.createElement("div");
      top.style.width = "100%";
      top.append(label, " ", status);
      li.append(top, wrap);
      uploadList.prepend(li);

      const item = { status, bar: wrap };
      const result = await uploadOne(file, item);
      if (result === "auth") return showLogin("Your session expired. Please log in again.");
    }
    fileInput.value = "";
    loadFiles();
  }

  fileInput.addEventListener("change", () => uploadFiles(fileInput.files));

  ["dragenter", "dragover"].forEach((type) =>
    dropzone.addEventListener(type, (e) => {
      e.preventDefault();
      dropzone.classList.add("over");
    })
  );
  ["dragleave", "drop"].forEach((type) =>
    dropzone.addEventListener(type, (e) => {
      e.preventDefault();
      dropzone.classList.remove("over");
    })
  );
  dropzone.addEventListener("drop", (e) => uploadFiles(e.dataTransfer.files));

  checkSession();
})();
