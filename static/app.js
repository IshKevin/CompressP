(function () {
  "use strict";

  const RETRY_LIMIT = 6;
  let uploading = false;
  let unfinished = [];

  const $ = (sel) => document.querySelector(sel);
  const loginView = $("#login-view");
  const appView = $("#app-view");
  const loginForm = $("#login-form");
  const loginError = $("#login-error");
  const fileInput = $("#file-input");
  const folderInput = $("#folder-input");
  const dropzone = $("#dropzone");
  const uploadList = $("#upload-list");
  const rows = $("#file-rows");
  const listStatus = $("#list-status");
  const unfinishedCard = $("#unfinished-card");
  const unfinishedList = $("#unfinished-list");

  class AuthError extends Error {}
  class FatalError extends Error {}

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

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

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  // ----- login -----

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
    loadUnfinished();
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
    try {
      const res = await fetch("/api/login", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: $("#password").value }),
      });
      if (res.ok) return showApp();
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

  $("#refresh-btn").addEventListener("click", () => {
    loadFiles();
    loadUnfinished();
  });

  // ----- stored files -----

  async function loadFiles() {
    listStatus.textContent = "Loading...";
    try {
      const res = await fetch("/api/files", { credentials: "same-origin" });
      if (res.status === 401) return showLogin("Your session expired. Please log in again.");
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
      const hash = el("td", "hash", f.sha256 ? f.sha256.slice(0, 12) + "..." : "-");
      if (f.sha256) hash.title = f.sha256;
      tr.append(
        el("td", "name", f.path || f.name),
        el("td", "", fmtSize(f.size)),
        el("td", "", fmtDate(f.uploaded_at)),
        hash
      );

      const actions = el("td", "actions");
      const dl = el("a", "button", "Download");
      dl.href = "/api/files/" + encodeURIComponent(f.id);
      dl.download = f.name;
      const del = el("button", "danger", "Delete");
      del.type = "button";
      del.addEventListener("click", () => deleteFile(f));
      actions.append(dl, del);

      tr.append(actions);
      rows.append(tr);
    }
  }

  async function deleteFile(file) {
    if (!confirm('Delete "' + (file.path || file.name) + '"? This cannot be undone.')) return;
    const res = await fetch("/api/files/" + encodeURIComponent(file.id), {
      method: "DELETE",
      credentials: "same-origin",
    });
    if (res.status === 401) return showLogin("Your session expired. Please log in again.");
    if (!res.ok) alert("Delete failed.");
    loadFiles();
  }

  // ----- unfinished uploads -----

  async function loadUnfinished() {
    try {
      const res = await fetch("/api/uploads", { credentials: "same-origin" });
      unfinished = res.ok ? (await res.json()).uploads : [];
    } catch (err) {
      unfinished = [];
    }
    renderUnfinished();
  }

  function renderUnfinished() {
    unfinishedList.textContent = "";
    unfinishedCard.hidden = unfinished.length === 0;
    for (const u of unfinished) {
      const li = el("li");
      const text = el(
        "span", "",
        `${u.path} (${fmtSize(u.size)}): ${u.received.length} of ${u.total_chunks} parts sent`
      );
      const discard = el("button", "danger", "Discard");
      discard.type = "button";
      discard.addEventListener("click", () => discardUpload(u.upload_id));
      li.append(text, discard);
      unfinishedList.append(li);
    }
  }

  async function discardUpload(uploadId) {
    if (!confirm("Discard this unfinished upload? Parts already sent will be deleted.")) return;
    await fetch("/api/uploads/" + uploadId, { method: "DELETE", credentials: "same-origin" });
    loadUnfinished();
  }

  // ----- uploading -----

  async function errorText(res) {
    try {
      const data = await res.json();
      return data.error || "Server error " + res.status;
    } catch (err) {
      return "Server error " + res.status;
    }
  }

  // Retries network failures and 5xx responses with growing delays.
  // Client errors (bad request, missing upload, disk full) fail immediately.
  async function requestWithRetry(url, options) {
    for (let attempt = 1; ; attempt++) {
      let res = null;
      try {
        res = await fetch(url, Object.assign({ credentials: "same-origin" }, options));
      } catch (err) {
        res = null; // network failure
      }
      if (res && res.status === 401) throw new AuthError();
      if (res && res.ok) return res;
      // 422 = a part arrived damaged, so sending it again is the right move.
      const fatal = res && ((res.status < 500 && res.status !== 422) || res.status === 507);
      if (fatal) throw new FatalError(await errorText(res));
      if (attempt >= RETRY_LIMIT) {
        throw new FatalError("Connection kept failing. Choose the file again to resume.");
      }
      await sleep(Math.min(30000, 1000 * Math.pow(2, attempt)));
    }
  }

  function makeItem(file, path) {
    const li = el("li");
    const row = el("div", "row");
    const label = el("span", "", `${path} (${fmtSize(file.size)})`);
    const status = el("span", "", "Waiting...");
    row.append(label, status);
    const bar = el("div", "progress");
    const fill = el("div");
    bar.append(fill);
    li.append(row, bar);
    uploadList.prepend(li);
    return { status, fill };
  }

  function setProgress(item, done, total) {
    const pct = total ? Math.floor((done / total) * 100) : 100;
    item.fill.style.width = pct + "%";
    item.status.textContent = pct + "%";
  }

  async function uploadOne(file, path, item) {
    let uploadId = null;
    let chunkSize = 0;
    let totalParts = 0;
    let received = new Set();

    // Resume a matching unfinished upload if there is one.
    const match = unfinished.find((u) => u.path === path && u.size === file.size);
    if (match) {
      try {
        const res = await requestWithRetry("/api/uploads/" + match.upload_id, {});
        const state = await res.json();
        uploadId = state.upload_id;
        chunkSize = state.chunk_size;
        totalParts = state.total_chunks;
        received = new Set(state.received);
      } catch (err) {
        if (err instanceof AuthError) throw err;
        uploadId = null; // no longer on the server; start a new upload
      }
    }

    if (!uploadId) {
      const res = await requestWithRetry("/api/uploads", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path, size: file.size }),
      });
      const state = await res.json();
      uploadId = state.upload_id;
      chunkSize = state.chunk_size;
      totalParts = state.total_chunks;
    }

    const partLength = (i) => Math.min(chunkSize, file.size - i * chunkSize);
    let done = 0;
    for (const i of received) done += partLength(i);
    setProgress(item, done, file.size);

    for (let i = 0; i < totalParts; i++) {
      if (received.has(i)) continue;
      const start = i * chunkSize;
      const end = start + partLength(i);
      const part = file.slice(start, end);
      const headers = { "Content-Type": "application/octet-stream" };
      const checksum = await sha256Hex(part);
      if (checksum) headers["X-Chunk-SHA256"] = checksum;
      await requestWithRetry("/api/uploads/" + uploadId + "/chunks/" + i, {
        method: "PUT",
        headers,
        body: part,
      });
      done += end - start;
      setProgress(item, done, file.size);
    }

    item.status.textContent = "Finishing...";
    await requestWithRetry("/api/uploads/" + uploadId + "/complete", { method: "POST" });
    item.status.textContent = "Done";
    item.fill.style.width = "100%";
  }

  // SHA-256 of one part, so the server can confirm it arrived unchanged.
  // Browsers only offer this on HTTPS or localhost; elsewhere the check is skipped.
  async function sha256Hex(blob) {
    if (!(window.crypto && window.crypto.subtle)) return null;
    const digest = await window.crypto.subtle.digest("SHA-256", await blob.arrayBuffer());
    return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
  }

  function beforeUnload(event) {
    event.preventDefault();
    event.returnValue = "";
  }

  async function uploadFiles(fileList) {
    const files = Array.from(fileList || []);
    if (!files.length || uploading) return;
    uploading = true;
    window.addEventListener("beforeunload", beforeUnload);
    try {
      await loadUnfinished();
      for (const file of files) {
        const path = file.webkitRelativePath || file.name;
        const item = makeItem(file, path);
        try {
          await uploadOne(file, path, item);
        } catch (err) {
          if (err instanceof AuthError) {
            return showLogin("Your session expired. Log in again, then choose the same files to resume.");
          }
          item.status.textContent = err.message || "Failed";
          item.status.className = "danger";
        }
      }
    } finally {
      uploading = false;
      window.removeEventListener("beforeunload", beforeUnload);
      fileInput.value = "";
      folderInput.value = "";
      loadFiles();
      loadUnfinished();
    }
  }

  fileInput.addEventListener("change", () => uploadFiles(fileInput.files));
  folderInput.addEventListener("change", () => uploadFiles(folderInput.files));

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
