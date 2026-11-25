/** @odoo-module **/

import { patch } from "@web/core/utils/patch";
import { registries } from "@odoo/o-spreadsheet";
import { AbstractSpreadsheetAction } from "@spreadsheet_edition/bundle/actions/abstract_spreadsheet_action";
const { topbarMenuRegistry } = registries;
import { _t } from "@web/core/l10n/translation";
import { onMounted, onWillUnmount, useSubEnv } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";

// Wait this long after the last edit before rebuilding the report.
const SYNC_DELAY = 1200;

patch(AbstractSpreadsheetAction.prototype, {
    setup() {
        super.setup()
        useSubEnv({
            mapData: this.mapData.bind(this),
        });
        this.action = useService("action");
        this.reportSync = { reportId: false, timer: null, lastData: null, lastError: null, busy: false };

        // Registered after the parent's hooks, so this.model exists by then.
        onMounted(async () => {
            if (this.resModel !== "spreadsheet.template") {
                return;
            }
            const [report] = await this.orm.searchRead(
                "report.spreadsheet",
                [["spreadsheet_id", "=", this.resId]],
                ["id"],
                { limit: 1 }
            );
            if (!report) {
                return;
            }
            this.reportSync.reportId = report.id;
            this.reportSync.lastData = JSON.stringify(this.model.exportData());
            this.model.on("update", this, () => this.scheduleReportSync());
        });
        onWillUnmount(() => {
            if (this.reportSync.timer) {
                clearTimeout(this.reportSync.timer);
                this.syncReport();
            }
            this.model?.off("update", this);
        });
    },

    scheduleReportSync() {
        clearTimeout(this.reportSync.timer);
        this.reportSync.timer = setTimeout(() => this.syncReport(), SYNC_DELAY);
    },

    async syncReport() {
        const sync = this.reportSync;
        sync.timer = null;
        const data = JSON.stringify(this.model.exportData());
        // "update" also fires on selection and scrolling: only real edits sync.
        if (!sync.reportId || data === sync.lastData) {
            return;
        }
        if (sync.busy) {
            this.scheduleReportSync();
            return;
        }
        sync.busy = true;
        try {
            const result = await this.orm.call("report.spreadsheet", "sync_from_spreadsheet", [
                sync.reportId,
                data,
            ]);
            sync.lastData = data;
            if (result.error && result.error !== sync.lastError) {
                this.notifications.add(result.error, {
                    title: _t("Report not updated"),
                    type: "warning",
                });
            }
            sync.lastError = result.error || null;
        } finally {
            sync.busy = false;
        }
    },

    async mapData() {
        // this.resId is resolved by AbstractSpreadsheetAction from params/props;
        // props.resId is undefined for client actions opened with params.
        const reportSpreadsheet = this.resModel === "spreadsheet.template"
            ? await this.orm.search("report.spreadsheet", [["spreadsheet_id", "=", this.resId]], { limit: 1 })
            : [];

        if (!reportSpreadsheet.length) {
            this.notifications.add(_t("This spreadsheet is not linked to a report."), {
                type: "warning",
            });
            return;
        }
        const data = JSON.stringify(this.model.exportData());
        this.reportSync.lastData = data;
        const context = await this.orm.call(
            "report.spreadsheet",
            "action_save_json",
            [reportSpreadsheet[0], data]
        );
        this.action.doAction({
            type: "ir.actions.act_window",
            name: _t("Map Fields"),
            res_model: "map.fields.wizard",
            views: [[false, "form"]],
            target: "new",
            context: context,
        });
    },

});

topbarMenuRegistry.addChild("map_data", ["file"], {
    name: _t("Map Data"),
    sequence: 100,
    execute: (env) => env.mapData(),
    icon: "o-spreadsheet-Icon.RANDOM",
});
