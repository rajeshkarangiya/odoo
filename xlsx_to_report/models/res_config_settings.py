from odoo import fields, models

class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    xlsx_to_report_ai_api_key = fields.Char(
        string="AI API Key",
        config_parameter="xlsx_to_report.ai_api_key",
        help="API Key for the AI provider (e.g. Google Gemini)"
    )
