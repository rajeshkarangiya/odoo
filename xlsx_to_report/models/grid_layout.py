"""Turn an exported o-spreadsheet sheet into a QWeb report body.

Kept apart from the model so the layout rules can be read on their own.

Placeholders
    [[field.path]]          value of the record, e.g. [[partner_id.vat]]
    [[field.path:words]]    an amount spelled out in the record's currency
    [[field.path:date]]     a date or datetime shown as a date
    [[field.path:image]]    an image field, e.g. [[company_id.logo:image]]
    [[#]]                   line number inside a repeated row

A row whose placeholders start with a one2many/many2many field
(e.g. [[order_line.product_id.name]]) is printed once per line.
"""
import html
import re

from odoo import _
from odoo.exceptions import UserError

PLACEHOLDER = re.compile(r"\[\[\s*([#A-Za-z0-9_.]+)\s*(?::\s*(words|date|image))?\s*\]\]")
FORMATS = ("words", "date", "image")


def col_letters_to_index(letters):
    col = 0
    for char in letters.upper():
        col = col * 26 + (ord(char) - ord("A") + 1)
    return col - 1


def parse_ref(ref):
    match = re.match(r"^([A-Za-z]+)(\d+)$", ref or "")
    if not match:
        return None
    return int(match.group(2)) - 1, col_letters_to_index(match.group(1))


def parse_zone(zone):
    """'A1:C3' or 'B2' -> (top, left, bottom, right), all inclusive."""
    parts = (zone or "").split(":")
    start = parse_ref(parts[0])
    end = parse_ref(parts[-1])
    if not start or not end:
        return None
    return (min(start[0], end[0]), min(start[1], end[1]),
            max(start[0], end[0]), max(start[1], end[1]))


def iter_zone(zone):
    box = parse_zone(zone)
    if not box:
        return
    top, left, bottom, right = box
    for row in range(top, bottom + 1):
        for col in range(left, right + 1):
            yield row, col


def has_placeholder(text):
    return bool(PLACEHOLDER.search(str(text or "")))


class GridLayout:
    def __init__(self, env, model_name, data, sheet, label_map=None):
        self.env = env
        self.model = env[model_name]
        self.sheet = sheet or {}
        self.styles = (data or {}).get("styles", {}) or {}
        self.borders = (data or {}).get("borders", {}) or {}
        self.label_map = label_map or {}

        self.cells = {}
        for ref, value in (self.sheet.get("cells") or {}).items():
            pos = parse_ref(ref)
            if isinstance(value, dict):
                value = value.get("content") or value.get("value")
            if pos and value not in (None, ""):
                self.cells[pos] = str(value)

        self.cell_style = self._zone_map("styles", "style")
        self.cell_border = self._zone_map("borders", "border")

        self.spans, self.covered = {}, set()
        for zone in self.sheet.get("merges") or []:
            box = parse_zone(zone)
            if not box:
                continue
            top, left, bottom, right = box
            self.spans[(top, left)] = (bottom - top + 1, right - left + 1)
            for row, col in iter_zone(zone):
                if (row, col) != (top, left):
                    self.covered.add((row, col))

        used = set(self.cells) | set(self.cell_border) | {
            pos for pos, style_id in self.cell_style.items()
            if (self.styles.get(str(style_id)) or {}).get("fillColor")}
        for (row, col), (rspan, cspan) in self.spans.items():
            used.add((row + rspan - 1, col + cspan - 1))
        self.max_row = max((r for r, _c in used), default=-1)
        self.max_col = max((c for _r, c in used), default=-1)

    # -- reading the export -------------------------------------------------

    def _zone_map(self, key, legacy_key):
        """Style/border ids per cell, from either export format."""
        result = {}
        for zone, ident in (self.sheet.get(key) or {}).items():
            for pos in iter_zone(zone):
                result[pos] = ident
        for ref, value in (self.sheet.get("cells") or {}).items():
            if isinstance(value, dict) and value.get(legacy_key) is not None:
                pos = parse_ref(ref)
                if pos:
                    result[pos] = value[legacy_key]
        return result

    # -- placeholders ---------------------------------------------------------

    def _resolve(self, path, allow_loop=True):
        """Validate a dotted path against the model.

        Returns (loop_field or None, rest_path, last_field, crosses_many).
        """
        names = path.split(".")
        model = self.model
        loop = None
        rest = []
        crosses_many = False
        field = None
        for index, name in enumerate(names):
            field = model._fields.get(name)
            if not field:
                raise UserError(_("Unknown field '%(field)s' in placeholder [[%(path)s]] on %(model)s.",
                                  field=name, path=path, model=model._description))
            if allow_loop and index == 0 and field.type in ("one2many", "many2many") and len(names) > 1:
                loop = name
            else:
                rest.append(name)
                if field.type in ("one2many", "many2many"):
                    crosses_many = True
            if field.relational:
                model = self.env[field.comodel_name]
            elif index < len(names) - 1:
                raise UserError(_("'%(field)s' is not a relation, so [[%(path)s]] cannot go past it.",
                                  field=name, path=path))
        return loop, ".".join(rest), field, crosses_many

    def _currency_expr(self, var):
        if "currency_id" in self.model._fields:
            return "%s.currency_id" % var if var == "doc" else "doc.currency_id"
        return "doc.env.company.currency_id"

    def _render_placeholder(self, path, fmt, var):
        if path == "#":
            return '<t t-out="line_index + 1"/>'
        _loop, rest, field, crosses_many = self._resolve(path, allow_loop=False)
        expr = "%s.%s" % (var, rest)
        if fmt == "words":
            return '<t t-out="%s.amount_to_text(%s)"/>' % (self._currency_expr(var), expr)
        if fmt == "image":
            return ('<img t-if="%s" t-att-src="image_data_uri(%s)" '
                    'style="max-height:70px;max-width:100%%"/>' % (expr, expr))
        if crosses_many:
            mapped = "%s.mapped('%s')" % (var, rest)
            if field.relational:
                mapped += ".mapped('display_name')"
            return '<t t-out="\', \'.join(str(v) for v in %s)"/>' % mapped
        if fmt == "date" or field.type in ("date", "datetime"):
            return '<span t-field="%s" t-options="{\'widget\': \'date\'}"/>' % expr
        return '<span t-field="%s"/>' % expr

    def _row_loop(self, row):
        loops = set()
        for col in range(self.max_col + 1):
            for match in PLACEHOLDER.finditer(self.cells.get((row, col), "")):
                if match.group(1) == "#":
                    continue
                loop, _rest, _field, _many = self._resolve(match.group(1))
                loops.add(loop)
        loops.discard(None)
        if len(loops) > 1:
            raise UserError(_("Row %(row)s repeats over more than one list (%(lists)s). Use one list per row.",
                              row=row + 1, lists=", ".join(sorted(loops))))
        return loops.pop() if loops else None

    def _cell_content(self, text, loop):
        var = "line" if loop else "doc"
        out, last = [], 0
        for match in PLACEHOLDER.finditer(text):
            out.append(html.escape(text[last:match.start()]))
            path = match.group(1)
            if loop and path != "#" and path.split(".")[0] == loop:
                path = path.split(".", 1)[1]
                saved, self.model = self.model, self.env[self.model._fields[loop].comodel_name]
                try:
                    out.append(self._render_placeholder(path, match.group(2), var))
                finally:
                    self.model = saved
            else:
                out.append(self._render_placeholder(path, match.group(2), "doc"))
            last = match.end()
        out.append(html.escape(text[last:]))
        return "".join(out).replace("\n", "<br/>")

    # -- styling --------------------------------------------------------------

    def _css(self, pos):
        css = ["padding:2px 4px", "vertical-align:middle", "border:0 none"]
        style = self.styles.get(str(self.cell_style.get(pos)), {}) if pos in self.cell_style else {}
        if style.get("bold"):
            css.append("font-weight:bold")
        if style.get("italic"):
            css.append("font-style:italic")
        if style.get("underline"):
            css.append("text-decoration:underline")
        if style.get("fontSize"):
            css.append("font-size:%spt" % style["fontSize"])
        if style.get("textColor"):
            css.append("color:%s" % style["textColor"])
        if style.get("fillColor"):
            css.append("background-color:%s" % style["fillColor"])
        if style.get("align") in ("left", "center", "right"):
            css.append("text-align:%s" % style["align"])
        if style.get("verticalAlign") in ("top", "middle", "bottom"):
            css[1] = "vertical-align:%s" % style["verticalAlign"]
        # A merged cell takes each side from the cells along that edge.
        rspan, cspan = self.spans.get(pos, (1, 1))
        row, col = pos
        edges = {"top": (row, col), "left": (row, col),
                 "bottom": (row + rspan - 1, col), "right": (row, col + cspan - 1)}
        for side in ("top", "right", "bottom", "left"):
            edge = edges[side]
            border = self.borders.get(str(self.cell_border.get(edge))) if edge in self.cell_border else None
            spec = (border or {}).get(side)
            if not spec:
                continue
            if isinstance(spec, dict):
                kind, color = spec.get("style", "thin"), spec.get("color", "#000000")
            else:
                kind, color = (list(spec) + ["thin", "#000000"])[:2]
            width = {"medium": "2px", "thick": "3px"}.get(kind, "1px")
            line = {"dashed": "dashed", "dotted": "dotted"}.get(kind, "solid")
            css.append("border-%s:%s %s %s" % (side, width, line, color))
        return ";".join(css)

    # -- labels mapped in the wizard -----------------------------------------

    def _apply_label_map(self):
        """Place the wizard's mappings in the grid as placeholders.

        A field of the record prints in the empty cell right of its label
        (or replaces the label when that cell is taken). A field of a list,
        e.g. order_line.price_unit, is a column header: it prints in the
        cell just below, and that row then repeats once per line. A list
        mapped with no line field gives the line number.
        """
        if not self.label_map:
            return
        normal = {re.sub(r"\s+", " ", k.strip().lower()): v for k, v in self.label_map.items() if k and v}
        for (row, col), text in list(self.cells.items()):
            path = normal.get(re.sub(r"\s+", " ", text.strip().lower()))
            if not path or has_placeholder(text):
                continue
            first = self.model._fields.get(path.split(".")[0])
            if first is not None and first.type in ("one2many", "many2many"):
                below = (row + (self.spans.get((row, col), (1, 1))[0]), col)
                if below in self.covered:
                    continue
                self.cells[below] = "[[#]]" if "." not in path else "[[%s]]" % path
                self.max_row = max(self.max_row, below[0])
                continue
            target = (row, col + self.spans.get((row, col), (1, 1))[1])
            if target not in self.cells and target not in self.covered:
                self.cells[target] = "[[%s]]" % path
                self.max_col = max(self.max_col, target[1])
            else:
                self.cells[(row, col)] = "[[%s]]" % path

    # -- output ---------------------------------------------------------------

    def body(self):
        self._apply_label_map()
        if self.max_row < 0:
            return "<p>No content found in the spreadsheet.</p>"

        cols = self.sheet.get("cols") or {}
        widths = [float((cols.get(str(c)) or {}).get("size") or 96) for c in range(self.max_col + 1)]
        total = sum(widths) or 1
        colgroup = "".join('<col style="width:%.2f%%"/>' % (w * 100 / total) for w in widths)

        row_sizes = self.sheet.get("rows") or {}
        rows_xml = []
        for row in range(self.max_row + 1):
            loop = self._row_loop(row)
            height = (row_sizes.get(str(row)) or {}).get("size")
            tr_attrs = ' style="border:0 none;%s"' % ("height:%spx" % int(height) if height else "")
            if loop:
                tr_attrs += ' t-foreach="doc.%s" t-as="line"' % loop
            tds = []
            for col in range(self.max_col + 1):
                pos = (row, col)
                if pos in self.covered:
                    continue
                span = ""
                if pos in self.spans:
                    rspan, cspan = self.spans[pos]
                    span = "".join([' rowspan="%d"' % rspan if rspan > 1 else "",
                                    ' colspan="%d"' % cspan if cspan > 1 else ""])
                content = self._cell_content(self.cells.get(pos, ""), loop)
                tds.append('<td%s style="%s">%s</td>' % (span, self._css(pos), content))
            rows_xml.append("<tr%s>%s</tr>" % (tr_attrs, "".join(tds)))

        return ('<table class="o_ignore_layout_styling" style="border:0 none;width:100%%;table-layout:fixed;border-collapse:collapse;font-size:10pt">'
                '<colgroup>%s</colgroup><tbody style="border:0 none">%s</tbody></table>' % (colgroup, "".join(rows_xml)))

    def uses_placeholders(self):
        return any(has_placeholder(text) for text in self.cells.values())
