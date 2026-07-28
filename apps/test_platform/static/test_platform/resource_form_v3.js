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
        const mode = document.querySelector('input[name="ui_execution_mode"]:checked')?.value;
        ["ui_procedure_database", "ui_agent_asset_file", "ui_agent_asset_text"].forEach((name) => {
            const row = document.querySelector(`.form-row.field-${name}`);
            if (!row) {
                return;
            }
            row.hidden = mode === "agent" ? name === "ui_procedure_database" : name !== "ui_procedure_database";
        });
    }

    function initialize() {
        document.querySelectorAll('input[name="resource_types"]').forEach((input) => {
            input.addEventListener("change", refreshSections);
        });
        document.querySelectorAll('input[name="ui_execution_mode"]').forEach((input) => {
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
