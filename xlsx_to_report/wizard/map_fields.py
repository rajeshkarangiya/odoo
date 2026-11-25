from odoo import _, api, fields, models


class MapFieldsWizard(models.TransientModel):
    _name = 'map.fields.wizard'
    _description = 'Map Fields Wizard'

    model_id = fields.Many2one('ir.model', string="Model", required=True)
    line_ids = fields.One2many('map.fields.wizard.line', 'wizard_id', string="Mappings")
    report_spreadsheet_binary_data = fields.Binary()
    report_spreadsheet_id = fields.Many2one('report.spreadsheet')

    def action_confirm(self):
        self.ensure_one()
        mapping_dict = {}
        for line in self.line_ids:
            if line.label and line.field_id:
                path = line.field_id.name
                if line.line_field_id:
                    path += "." + line.line_field_id.name
                mapping_dict[line.label] = path
        report = self.report_spreadsheet_id
        report.model_id = self.model_id
        action = report.create_report(mapping_dict)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "success",
                "title": _("Report ready"),
                "message": _("Open any %(model)s record and use Print > %(report)s.",
                             model=self.model_id.name, report=action.name),
                "next": {"type": "ir.actions.act_window_close"},
            },
        }

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)

        cells = self.env.context.get("cells_data", {})

        line_vals = []
        for column, value in cells.items():
            label = value.get("content") if isinstance(value, dict) else str(value)
            if label and "[[" not in label:
                line_vals.append((0, 0, {
                    "label": label,
                }))

        res["line_ids"] = line_vals

        return res


class MapFieldsWizardLine(models.TransientModel):
    _name = 'map.fields.wizard.line'
    _description = 'Map Fields Wizard Line'

    wizard_id = fields.Many2one('map.fields.wizard', required=True, ondelete='cascade')

    label = fields.Char("Excel Field / Label", required=True)

    field_id = fields.Many2one(
        'ir.model.fields',
        string="Odoo Field",
        domain="[('model_id', '=', parent.model_id)]",
    )
    field_type = fields.Selection(related="field_id.ttype")
    field_relation = fields.Char(related="field_id.relation")
    line_field_id = fields.Many2one(
        'ir.model.fields',
        string="Line Field",
        domain="[('model', '=', field_relation)]",
        help="For a list such as Order Lines: the field of each line printed "
             "under this column header. Leave empty to print the line number.",
    )

    @api.onchange('field_id')
    def _onchange_field_id(self):
        if self.line_field_id and self.line_field_id.model != self.field_id.relation:
            self.line_field_id = False