(function () {
    "use strict";

    const categories = ["ui", "api", "database", "performance", "port"];

    function selectedTypes() {
        return new Set(
            Array.from(document.querySelectorAll('input[name="resource_types"]:checked'))
                .map((input) => input.value)
        );
    }

    function refreshSections() {
        const selected = selectedTypes();
        categories.forEach((category) => {
            document.querySelectorAll(`fieldset.resource-${category}`).forEach((section) => {
                section.hidden = !selected.has(category);
            });
        });
    }

    function initialize() {
        document.querySelectorAll('input[name="resource_types"]').forEach((input) => {
            input.addEventListener("change", refreshSections);
        });
        refreshSections();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initialize, {once: true});
    } else {
        initialize();
    }
})();
