(() => {
    "use strict";

    const marker = document.getElementById("tb-generation-poll");
    if (!marker) {
        return;
    }

    const configured = Number.parseInt(marker.dataset.intervalMs || "8000", 10);
    const intervalMs = Number.isFinite(configured)
        ? Math.max(2000, Math.min(30000, configured))
        : 8000;
    let timer = null;
    let formIsDirty = false;

    document.addEventListener("input", () => {
        formIsDirty = true;
    });
    document.addEventListener("change", () => {
        formIsDirty = true;
    });

    const schedule = () => {
        if (timer !== null) {
            window.clearTimeout(timer);
            timer = null;
        }
        if (document.visibilityState !== "visible") {
            return;
        }
        timer = window.setTimeout(() => {
            const activeTag = document.activeElement?.tagName || "";
            if (
                formIsDirty
                || activeTag === "INPUT"
                || activeTag === "TEXTAREA"
                || activeTag === "SELECT"
            ) {
                schedule();
                return;
            }
            window.location.reload();
        }, intervalMs);
    };

    document.addEventListener("visibilitychange", schedule);
    schedule();
})();
