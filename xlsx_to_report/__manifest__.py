{
    'name': 'Spreadsheet Report Builder',
    'version': '19.0.1.1.0',
    'summary': 'Design PDF reports in an Odoo spreadsheet and print them from any record',
    'category': 'Productivity',
    'license': 'OPL-1',
    'depends': ['spreadsheet', 'documents_spreadsheet', 'spreadsheet_edition', 'base_setup'],
    'data': [
        'security/ir.model.access.csv',
        'data/report_paperformat.xml',
        'wizard/map_fields.xml',
        'views/report_spreadsheet.xml',
        'views/menu.xml',
        'views/res_config_settings_views.xml',
    ],
    'assets': {
        'spreadsheet.o_spreadsheet': [
            (
                'after',
                'spreadsheet/static/src/o_spreadsheet/o_spreadsheet.js',
                'xlsx_to_report/static/src/bundle/**/*',
            ),
        ],
    },
    'installable': True,
    'application': True,
}
