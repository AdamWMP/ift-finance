// IFT Finance dashboard — UX layer (search modal, theme toggle, keyboard nav,
// filter pills, notes popover). All progressive enhancement.

// Restore scroll position on browser back/forward navigation.
if ('scrollRestoration' in history) history.scrollRestoration = 'auto';

(function () {
  // ---------- theme toggle ----------
  const themeBtn = document.getElementById("themeBtn");
  if (themeBtn) {
    themeBtn.addEventListener("click", () => {
      const isLight = document.documentElement.classList.toggle("light");
      localStorage.setItem("ift-theme", isLight ? "light" : "dark");
    });
  }

  // ---------- search modal ----------
  const modal = document.getElementById("searchModal");
  const input = document.getElementById("searchInput");
  const results = document.getElementById("searchResults");
  const openSearch = () => { if (!modal) return; modal.hidden = false; input.value = ""; results.innerHTML = ""; setTimeout(() => input.focus(), 50); };
  const closeSearch = () => { if (modal) modal.hidden = true; };

  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
      e.preventDefault(); openSearch();
    } else if (e.key === "Escape" && modal && !modal.hidden) {
      closeSearch();
    }
  });
  const searchBtn = document.getElementById("searchBtn");
  if (searchBtn) searchBtn.addEventListener("click", openSearch);
  if (modal) modal.addEventListener("click", (e) => { if (e.target === modal) closeSearch(); });

  let searchTimer = null, currentResults = [], focusedIdx = -1;
  const renderResults = () => {
    if (!results) return;
    if (!currentResults.length) {
      results.innerHTML = `<div class="muted small" style="padding:12px;text-align:center">No matches</div>`;
      return;
    }
    results.innerHTML = currentResults.map((r, i) => `
      <div class="search-result ${i === focusedIdx ? "kb-focus" : ""}" data-url="${r.url}">
        <div><span class="kind">${r.kind}</span><b>${r.label}</b><div class="muted small">${r.sub || ""}</div></div>
      </div>
    `).join("");
    results.querySelectorAll(".search-result").forEach((el) => {
      el.addEventListener("click", () => { window.location.href = el.dataset.url; });
    });
  };
  if (input) {
    input.addEventListener("input", () => {
      clearTimeout(searchTimer);
      const q = input.value.trim();
      if (q.length < 2) { results.innerHTML = ""; currentResults = []; return; }
      searchTimer = setTimeout(async () => {
        try {
          const r = await fetch("/api/search?q=" + encodeURIComponent(q));
          const j = await r.json();
          currentResults = j.results || [];
          focusedIdx = currentResults.length ? 0 : -1;
          renderResults();
        } catch (e) { /* ignore */ }
      }, 120);
    });
    input.addEventListener("keydown", (e) => {
      if (!currentResults.length) return;
      if (e.key === "ArrowDown") { e.preventDefault(); focusedIdx = (focusedIdx + 1) % currentResults.length; renderResults(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); focusedIdx = (focusedIdx - 1 + currentResults.length) % currentResults.length; renderResults(); }
      else if (e.key === "Enter") {
        e.preventDefault();
        const r = currentResults[focusedIdx]; if (r) window.location.href = r.url;
      }
    });
  }

  // ---------- filter pills ----------
  // Any element with class .filterbar uses data-filter on its rows.
  document.querySelectorAll(".filterbar").forEach((bar) => {
    const targetSel = bar.dataset.target; if (!targetSel) return;
    bar.querySelectorAll(".chip").forEach((chip) => {
      chip.addEventListener("click", () => {
        bar.querySelectorAll(".chip").forEach((c) => c.classList.remove("active"));
        chip.classList.add("active");
        const v = chip.dataset.value || "";
        document.querySelectorAll(targetSel + " tbody tr").forEach((tr) => {
          if (!v) tr.style.display = "";
          else {
            const hay = (tr.dataset.filter || "").toLowerCase();
            tr.style.display = hay.includes(v.toLowerCase()) ? "" : "none";
          }
        });
      });
    });
  });

  // ---------- keyboard nav for tables ----------
  // Tables with class .kb-table get j/k row focus, Enter opens [data-href].
  document.querySelectorAll("table.kb-table").forEach((tbl) => {
    const rows = () => Array.from(tbl.querySelectorAll("tbody tr")).filter(r => r.offsetParent !== null);
    let idx = -1;
    const setFocus = (i) => {
      const rs = rows(); if (!rs.length) return;
      idx = (i + rs.length) % rs.length;
      rs.forEach((r, j) => r.classList.toggle("kb-focus", j === idx));
      rs[idx].scrollIntoView({block: "nearest"});
    };
    document.addEventListener("keydown", (e) => {
      if (modal && !modal.hidden) return;
      if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
      const k = e.key.toLowerCase();
      if (k === "j" || k === "arrowdown") { e.preventDefault(); setFocus(idx + 1); }
      else if (k === "k" || k === "arrowup") { e.preventDefault(); setFocus(idx - 1); }
      else if (e.key === "Enter") {
        const rs = rows(); if (idx < 0 || !rs[idx]) return;
        const href = rs[idx].dataset.href; if (href) window.location.href = href;
      } else if (k === "w") {
        const rs = rows(); const wa = rs[idx]?.dataset.whatsapp; if (wa) window.open(wa, "_blank");
      }
    });
  });

  // ---------- notes popover ----------
  document.querySelectorAll(".note-icon").forEach((icon) => {
    icon.addEventListener("click", async (e) => {
      e.stopPropagation();
      document.querySelectorAll(".note-pop").forEach((p) => p.remove());
      const cid = icon.dataset.cid; const stream = icon.dataset.stream || "";
      const r = await fetch(`/api/notes?contact_id=${cid}&stream=${encodeURIComponent(stream)}`);
      const j = await r.json();
      const pop = document.createElement("div"); pop.className = "note-pop";
      pop.innerHTML = `
        <textarea placeholder="Notes about this student…">${j.body || ""}</textarea>
        <div class="row">
          <span class="muted small">Saves automatically</span>
          <button class="csv-btn" data-act="close">Close</button>
        </div>`;
      icon.parentElement.appendChild(pop);
      const ta = pop.querySelector("textarea");
      ta.focus();
      let saveTimer = null;
      ta.addEventListener("input", () => {
        clearTimeout(saveTimer);
        saveTimer = setTimeout(async () => {
          const fd = new FormData();
          fd.append("contact_id", cid); fd.append("stream", stream); fd.append("body", ta.value);
          await fetch("/api/notes", { method: "POST", body: fd });
          icon.classList.toggle("has", !!ta.value.trim());
        }, 400);
      });
      pop.querySelector("[data-act=close]").addEventListener("click", () => pop.remove());
      document.addEventListener("click", function clo(e) {
        if (!pop.contains(e.target) && e.target !== icon) { pop.remove(); document.removeEventListener("click", clo); }
      });
    });
  });
})();
