// Radio Now landing player: one episode at a time, newest first.
(function () {
    const episodes = JSON.parse(document.getElementById("episodes-data").textContent || "[]");
    if (!episodes.length) return;

    const $ = (id) => document.getElementById(id);
    const audio = $("audio");
    const body = document.body;
    const player = document.querySelector(".player");
    let index = 0;

    const fmt = (s) => {
        s = Math.max(0, Math.floor(s || 0));
        const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = String(s % 60).padStart(2, "0");
        return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
    };
    const esc = (t) => String(t).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

    function render() {
        const ep = episodes[index];
        $("ep-kicker").textContent = ep.daily ? "Morning Broadcast" : "Broadcast";
        $("ep-title").textContent = ep.title;
        $("ep-meta").textContent = `${ep.host}  ·  ${ep.date}`;
        $("t-dur").textContent = fmt(ep.duration);
        $("t-cur").textContent = "0:00";
        $("progress-fill").style.width = "0";
        $("btn-prev").disabled = index === 0;
        $("btn-next").disabled = index === episodes.length - 1;
        document.title = `${ep.title} | Radio Now`;
        renderScript();
        document.querySelectorAll("#ep-list li").forEach((li, i) => li.classList.toggle("current", i === index));
        if ("mediaSession" in navigator) {
            navigator.mediaSession.metadata = new MediaMetadata({ title: ep.title, artist: ep.host, album: "Radio Now" });
        }
    }

    function renderScript() {
        const ep = episodes[index];
        $("script-title").textContent = ep.title;
        const paras = (ep.script || "").split(/\n\s*\n/).filter((p) => p.trim());
        $("script-body").innerHTML =
            (ep.summary ? `<p class="summary">${esc(ep.summary)}</p>` : "") +
            paras.map((p) => `<p>${esc(p.trim())}</p>`).join("");
    }

    function load(i, autoplay) {
        if (i < 0 || i >= episodes.length) return;
        const changed = i !== index || !audio.src;
        index = i;
        if (changed) {
            player.classList.add("fading");
            setTimeout(() => { render(); player.classList.remove("fading"); }, audio.src ? 250 : 0);
            audio.src = episodes[i].audio;
            history.replaceState(null, "", `#${episodes[i].id}`);
        }
        if (autoplay) audio.play().catch(() => {});
    }

    function toggle() {
        if (!audio.src) load(index, false);
        audio.paused ? audio.play().catch(() => {}) : audio.pause();
    }

    // Episode list
    $("ep-list").innerHTML = episodes.map((ep, i) => `
        <li><button data-i="${i}">
            <span class="t">${esc(ep.title)}</span><span class="d">${fmt(ep.duration)}</span>
            <span class="m">${esc(ep.host)} · ${esc(ep.date)}${ep.daily ? " · Morning Broadcast" : ""}</span>
            ${ep.summary ? `<span class="s">${esc(ep.summary)}</span>` : ""}
        </button></li>`).join("");
    $("ep-list").addEventListener("click", (e) => {
        const btn = e.target.closest("button[data-i]");
        if (!btn) return;
        load(Number(btn.dataset.i), true);
        closePanels();
    });

    // Panels
    const panels = { list: $("panel-list"), script: $("panel-script") };
    function openPanel(name) {
        const wasOpen = panels[name].classList.contains("open");
        closePanels();
        if (!wasOpen) {
            panels[name].classList.add("open");
            panels[name].setAttribute("aria-hidden", "false");
            if (name === "list") panels.list.querySelector(".current button")?.focus({ preventScroll: false });
        }
    }
    function closePanels() {
        Object.values(panels).forEach((p) => { p.classList.remove("open"); p.setAttribute("aria-hidden", "true"); });
    }
    $("btn-list").addEventListener("click", () => openPanel("list"));
    $("btn-script").addEventListener("click", () => openPanel("script"));
    document.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", closePanels));

    // Transport
    $("btn-play").addEventListener("click", toggle);
    $("btn-prev").addEventListener("click", () => load(index - 1, !audio.paused));
    $("btn-next").addEventListener("click", () => load(index + 1, !audio.paused));

    audio.addEventListener("play", () => body.classList.add("playing"));
    audio.addEventListener("pause", () => body.classList.remove("playing"));
    audio.addEventListener("waiting", () => body.classList.add("loading"));
    audio.addEventListener("playing", () => body.classList.remove("loading"));
    audio.addEventListener("loadedmetadata", () => { $("t-dur").textContent = fmt(audio.duration); });
    audio.addEventListener("timeupdate", () => {
        const pct = audio.duration ? (audio.currentTime / audio.duration) * 100 : 0;
        $("progress-fill").style.width = `${pct}%`;
        $("progress").setAttribute("aria-valuenow", Math.round(pct));
        $("t-cur").textContent = fmt(audio.currentTime);
    });
    audio.addEventListener("ended", () => {
        body.classList.remove("playing");
        if (index < episodes.length - 1) load(index + 1, true);
    });

    // Seeking
    const progress = $("progress");
    function seekTo(clientX) {
        if (!audio.duration) return;
        const r = progress.getBoundingClientRect();
        audio.currentTime = Math.min(Math.max((clientX - r.left) / r.width, 0), 1) * audio.duration;
    }
    progress.addEventListener("pointerdown", (e) => {
        if (!audio.src) load(index, false);
        seekTo(e.clientX);
        progress.setPointerCapture(e.pointerId);
        const move = (ev) => seekTo(ev.clientX);
        progress.addEventListener("pointermove", move);
        progress.addEventListener("pointerup", () => progress.removeEventListener("pointermove", move), { once: true });
    });
    progress.addEventListener("keydown", (e) => {
        if (e.key === "ArrowRight") audio.currentTime += 10;
        if (e.key === "ArrowLeft") audio.currentTime -= 10;
    });

    // Keyboard
    document.addEventListener("keydown", (e) => {
        if (e.target.closest("input, textarea")) return;
        if (e.key === "Escape") closePanels();
        if (e.key === " " && !e.target.closest("button, [role=slider]")) { e.preventDefault(); toggle(); }
    });

    if ("mediaSession" in navigator) {
        navigator.mediaSession.setActionHandler("play", toggle);
        navigator.mediaSession.setActionHandler("pause", toggle);
        navigator.mediaSession.setActionHandler("previoustrack", () => load(index - 1, true));
        navigator.mediaSession.setActionHandler("nexttrack", () => load(index + 1, true));
    }

    // Deep link: #<episode id>
    const fromHash = episodes.findIndex((ep) => ep.id === location.hash.slice(1));
    index = fromHash >= 0 ? fromHash : 0;
    audio.src = episodes[index].audio;
    render();
})();
