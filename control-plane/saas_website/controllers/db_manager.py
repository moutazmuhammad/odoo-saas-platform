# -*- coding: utf-8 -*-
"""White-label the Odoo database selector / manager pages.

Odoo's `Database` controller (`/web/database/selector` and
`/web/database/manager`) does not use Odoo's view-inheritance system —
it loads three plain HTML files from disk via `file_open()` and feeds
them straight into the QWeb compiler. That means an XML <template> with
`inherit_id` cannot reach them.

So we subclass the controller and override `_render_template`, swapping
the source paths to point at our customized copies under
`saas_website/static/src/public/`. The rest of the original logic
(databases list, master-password check, neutralize, etc.) is preserved
verbatim by reproducing the same context-setup before the QWeb render.
"""

import odoo
from odoo import http
from odoo.http import request
from odoo.tools.misc import file_open

from lxml import html
from odoo.addons.base.models.ir_qweb import render as qweb_render
from odoo.addons.web.controllers.database import Database, DBNAME_PATTERN


# The three templates that together render the database manager page.
# Keep paths Odoo-module-relative so `file_open` resolves them via the
# usual loader (no need for absolute filesystem paths).
_TEMPLATE_PATHS = {
    "database_manager": "saas_website/static/src/public/database_manager.qweb.html",
    "master_input": "saas_website/static/src/public/database_manager.master_input.qweb.html",
    "create_form": "saas_website/static/src/public/database_manager.create_form.qweb.html",
}


class VeltnexDatabase(Database):

    def _render_template(self, **d):
        # --- Reproduce the parent's context-building (same as core) ---
        d.setdefault("manage", True)
        d["insecure"] = odoo.tools.config.verify_admin_password("admin")
        d["list_db"] = odoo.tools.config["list_db"]
        d["langs"] = odoo.service.db.exp_list_lang()
        d["countries"] = odoo.service.db.exp_list_countries()
        d["pattern"] = DBNAME_PATTERN
        try:
            d["databases"] = http.db_list()
            d["incompatible_databases"] = (
                odoo.service.db.list_db_incompatible(d["databases"])
            )
        except odoo.exceptions.AccessDenied:
            d["databases"] = [request.db] if request.db else []

        # --- Load OUR HTML files instead of Odoo's defaults ------------
        templates = {}
        for key, path in _TEMPLATE_PATHS.items():
            with file_open(path, "r") as fd:
                templates[key] = fd.read()

        def load(template_name):
            fromstring = (
                html.document_fromstring
                if template_name == "database_manager"
                else html.fragment_fromstring
            )
            return (fromstring(templates[template_name]), template_name)

        return qweb_render("database_manager", d, load)
