import base64
import json
import io
import html
import logging
import re
import xml.etree.ElementTree as ET
import xlsxwriter
import requests
from odoo import api, fields, models, _
from odoo.exceptions import UserError

from .grid_layout import GridLayout

_logger = logging.getLogger(__name__)


class ReportSpreadsheet(models.Model):
    _name = 'report.spreadsheet'
    _description = "report spreadsheet"
    _order = "name"

    name = fields.Char(required=True, translate=True)
    spreadsheet_id = fields.Many2one('spreadsheet.template')
    report_spreadsheet_binary_data = fields.Binary()
    model_id = fields.Many2one('ir.model', ondelete='cascade')
    report_action_id = fields.Many2one('ir.actions.report', string="Report", readonly=True, ondelete='set null')
    field_mapping = fields.Json(readonly=True)
    use_company_layout = fields.Boolean(
        "Company Header", default=True,
        help="Print the company's standard header and footer around the layout. "
             "Turn it off when the spreadsheet draws its own header.")
    json_file_name = fields.Char(compute="_compute_json_file_name")
    file_name = fields.Char("Filename")
    file_data = fields.Binary("File")

    @api.depends("name")
    def _compute_json_file_name(self):
        for rec in self:
            rec.json_file_name = f"{rec.name}.osheet.json"

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            if not rec.spreadsheet_id:
                rec.spreadsheet_id = (
                    self.env["spreadsheet.template"]
                    .with_user(self.env.user.id)
                    .create({
                        "name": rec.name,
                        "spreadsheet_data": r"{}",
                    })
                )
        return records

    def write(self, vals):
        res = super().write(vals)
        # Changing the header choice or the name re-renders the built report.
        if {"use_company_layout", "name"} & set(vals) and not self.env.context.get("skip_report_sync"):
            for rec in self.filtered(lambda r: r.report_action_id and r.model_id and r.report_spreadsheet_binary_data):
                rec.with_context(skip_report_sync=True).create_report(rec.field_mapping or {}, use_ai=False)
        return res

    def action_edit_template(self):
        self.ensure_one()
        if self.spreadsheet_id.id:
            return {
                "type": "ir.actions.client",
                "tag": "action_open_template",
                "params": {
                    "spreadsheet_id": self.spreadsheet_id.id,
                },
            }

    def action_save_json(self, json_data):
        self.ensure_one()
        self.file_name = self.spreadsheet_id.name 
        self.report_spreadsheet_binary_data = base64.b64encode(json_data.encode())
        data = self.get_import_file()
        cells = self.get_cell_data(data)
        return {
                "default_model_id": self.model_id.id,
                "cells_data": cells,
                "default_report_spreadsheet_id": self.id,
            }

    def get_cell_data(self, data):
        cells = {}
        best_sheet = self._pick_best_sheet(data)
        if best_sheet:
            cells = self._extract_non_empty_cells(best_sheet)
        return cells

    def get_import_file(self):
        self.ensure_one()
        raw = base64.b64decode(self.report_spreadsheet_binary_data or b"{}")
        data = json.loads(raw.decode("utf-8"))
        return data

    def _extract_non_empty_cells(self, sheet):
        cells = {}
        for cell, value in (sheet or {}).get("cells", {}).items():
            if isinstance(value, dict):
                cell_value = value.get("content") or value.get("value")
            else:
                cell_value = value
            if isinstance(cell_value, str):
                cell_value = cell_value.strip()
            if cell_value not in (None, ""):
                cells[cell] = cell_value
        return cells

    def _cell_to_rowcol(self, cell):
        match = re.match(r"^([A-Za-z]+)(\d+)$", cell or "")
        if not match:
            return None
        letters, row_str = match.groups()
        col = 0
        for char in letters.upper():
            col = col * 26 + (ord(char) - ord("A") + 1)
        return int(row_str) - 1, col - 1

    def _pick_best_sheet(self, data):
        sheets = (data or {}).get("sheets", []) or []
        if not sheets:
            return {}

        # Pick the sheet with most non-empty cells for stable report generation.
        return max(sheets, key=lambda s: len(self._extract_non_empty_cells(s)))

    def _normalize_label(self, value):
        value = (value or "").strip().lower()
        return re.sub(r"\s+", " ", value)

    def _has_meaningful_table(self, xml_string):
        xml_text = (xml_string or "").lower()
        return "<table" in xml_text and ("t-field=" in xml_text or "t-esc=" in xml_text or "<td" in xml_text)

    def _build_grid_template(self, template_id, data, field_data):
        """Render the spreadsheet itself: merges, styles, borders, widths
        and [[placeholders]], wrapped in the chosen page layout."""
        layout = GridLayout(self.env, self.model_id.model, data,
                            self._pick_best_sheet(data), field_data)
        page_layout = "web.external_layout" if self.use_company_layout else "web.basic_layout"
        return """
<t t-name="{template_id}">
    <t t-call="web.html_container">
        <t t-foreach="docs" t-as="doc">
            <t t-call="{page_layout}">
                <div class="page">{body}</div>
            </t>
        </t>
    </t>
</t>""".format(template_id=template_id, page_layout=page_layout, body=layout.body()), layout

    def create_excel(self, data):
        output = io.BytesIO()
        workbook = xlsxwriter.Workbook(output, {'in_memory': True})
        sheet = workbook.add_worksheet()
        best_sheet = self._pick_best_sheet(data)
        cells = self._extract_non_empty_cells(best_sheet)
        for cell, cell_value in cells.items():
            coords = self._cell_to_rowcol(cell)
            if coords is None:
                continue
            row, col = coords
            sheet.write(row, col, cell_value)

        workbook.close()
        output.seek(0)
        return output.read()


    def create_report(self, field_data, use_ai=True):
        """Build (or rebuild) the QWeb PDF report for this spreadsheet.

        Uses the AI layout when an API key is configured and its answer is
        valid XML with a real table; otherwise builds the layout directly
        from the spreadsheet grid. Running it again updates the same report
        instead of creating a duplicate.
        """
        self.ensure_one()
        if not self.env.user.has_group("base.group_system"):
            raise UserError(_("Only administrators can generate reports."))
        if not self.model_id:
            raise UserError(_("Set the Model on the report before mapping fields."))

        data = self.get_import_file()
        name = re.sub(r'[^a-zA-Z0-9_]', '_', self.name).strip('_').lower() or "report"

        if self.report_action_id.report_name:
            # Keep the identifiers of the first build, so renaming the
            # report updates it instead of creating a second one.
            template_id = self.report_action_id.report_name
            report_name = template_id.split(".", 1)[1]
        else:
            report_name = f"report_{name}_{self.id}"
            template_id = f"xlsx_to_report.{report_name}"

        best_sheet = self._pick_best_sheet(data)
        simplified_cells = self._extract_non_empty_cells(best_sheet)

        grid_xml, layout = self._build_grid_template(template_id, data, field_data)
        # Placeholders are exact instructions; only a plain sheet goes to AI.
        generated_xml = False
        if use_ai and not layout.uses_placeholders():
            generated_xml = self._generate_ai_template(template_id, simplified_cells, field_data)
        if generated_xml:
            try:
                ET.fromstring(generated_xml.encode('utf-8'))
                if not self._has_meaningful_table(generated_xml):
                    _logger.warning("AI template has no table; using grid layout for report '%s'", self.name)
                    generated_xml = False
            except ET.ParseError:
                _logger.warning("AI returned invalid XML; using grid layout for report '%s'", self.name)
                generated_xml = False
        if not generated_xml:
            generated_xml = grid_xml
        try:
            ET.fromstring(generated_xml.encode("utf-8"))
        except ET.ParseError as e:
            raise UserError(_("The layout could not be turned into a report: %s", e)) from e

        view = self.env.ref(template_id, raise_if_not_found=False)
        if view:
            view.arch = generated_xml
        else:
            view = self.env["ir.ui.view"].create({
                "name": report_name,
                "type": "qweb",
                "arch": generated_xml,
            })
            self.env["ir.model.data"].create({
                "name": report_name,
                "module": "xlsx_to_report",
                "model": "ir.ui.view",
                "res_id": view.id,
                "noupdate": True,
            })

        Report = self.env["ir.actions.report"]
        action = Report.search([("report_name", "=", template_id)], limit=1)
        paperformat = (self.env.ref("xlsx_to_report.paperformat_spreadsheet_plain", raise_if_not_found=False)
                       if not self.use_company_layout else False)
        if action:
            action.write({"name": self.name, "model": self.model_id.model,
                          "paperformat_id": paperformat.id if paperformat else False})
        else:
            action = Report.create({
                "name": self.name,
                "model": self.model_id.model,
                "report_name": template_id,
                "report_type": "qweb-pdf",
                "paperformat_id": paperformat.id if paperformat else False,
            })
        action.create_action()
        self.with_context(skip_report_sync=True).write(
            {"report_action_id": action.id, "field_mapping": field_data or {}})
        return action

    def sync_from_spreadsheet(self, json_data):
        """Called by the spreadsheet editor after each change.

        Stores the sheet and, once the report has been built with Map Data,
        rebuilds it with the saved mapping - so a border or a new placeholder
        reaches the PDF without going through the wizard again. Never raises:
        a half-typed layout must not interrupt the person editing it.
        """
        self.ensure_one()
        self.report_spreadsheet_binary_data = base64.b64encode(json_data.encode())
        if not self.report_action_id:
            # Reports built before the link was stored.
            self.report_action_id = self.env["ir.actions.report"].search(
                [("report_name", "=like", "xlsx_to_report.report_%%_%d" % self.id)], limit=1)
        if not (self.report_action_id and self.model_id):
            return {"synced": False}
        try:
            with self.env.cr.savepoint():
                self.create_report(self.field_mapping or {}, use_ai=False)
        except UserError as e:
            return {"synced": False, "error": str(e.args[0] if e.args else e)}
        return {"synced": True}

    def _generate_ai_template(self, template_id, simplified_cells, field_data):
        """Ask the AI for a layout. Returns the XML string, or False."""
        api_key = self.env['ir.config_parameter'].sudo().get_param('xlsx_to_report.ai_api_key')
        if not api_key:
            return False
        prompt = f"""
        You are an Odoo technical expert.

        I will describe an Odoo report layout that is currently designed in a spreadsheet. Your job is to convert this layout into a valid Odoo QWeb XML template for a PDF report.

        Context:
        - Report model: {self.model_id.model}
        - Report type: QWeb PDF (using web.external_layout)
        - Template Name Variable: {template_id}

        Instructions:
        1. Translate the spreadsheet structure into an HTML structure inside a QWeb template.
        2. Keep the layout simple and printable. Use standard bootstrap table classes (e.g. `table`, `table-sm`).
        3. Assume the following variables are available in the template:
           - `doc` → the main record.
           - `docs` → the recordset.
        4. Output ONLY the XML template code exactly as a valid `<t>` node inside a `<template>` or directly use:
        <t t-name="{template_id}">
            <t t-call="web.html_container">
                <t t-foreach="docs" t-as="doc">
                    <t t-call="web.external_layout">
                        <div class="page">
                            ...
                        </div>
                    </t>
                </t>
            </t>
        </t>
        
        DO NOT provide the `<odoo>` or `<template>` wrapping tags, JUST return the outermost `<t t-name="{template_id}">` tag and its contents. Ensure valid XML. Provide absolutely NO MARKDOWN format and NO explanation, just the raw XML.

        Spreadsheet Cell Data: {simplified_cells}

        Mapping Fields Data (Spreadsheet Label -> Odoo Field Name): {field_data}
        """

        url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent"
        headers = {'Content-Type': 'application/json', 'x-goog-api-key': api_key}
        payload = {
            "contents": [{
                "parts": [{"text": prompt}]
            }],
            "generationConfig": {
                "temperature": 0.2
            }
        }

        try:
            response = requests.post(url, headers=headers, json=payload, timeout=60)
            response.raise_for_status()
            res_json = response.json()
            generated_xml = res_json.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
        except (requests.RequestException, ValueError, IndexError, AttributeError) as e:
            _logger.warning("AI request failed, using grid layout: %s", e)
            return False

        # Clean up Markdown blocks if AI provided them
        generated_xml = generated_xml.strip()
        if generated_xml.startswith('```xml'):
            generated_xml = generated_xml[6:]
        if generated_xml.startswith('```'):
            generated_xml = generated_xml[3:]
        if generated_xml.endswith('```'):
            generated_xml = generated_xml[:-3]
        generated_xml = generated_xml.strip()
        return generated_xml or False
