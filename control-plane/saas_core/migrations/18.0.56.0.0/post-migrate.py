"""Drop tables of removed models (18.0.56.0.0).

Odoo removes a deleted model's fields and ir.model rows on upgrade but
leaves its table behind:
- saas_instance_package: the Python Packages list, removed with the
  pip_packages / extra_config fields (nothing on Kubernetes applied them;
  a repo's requirements.txt covers Python dependencies).
- saas_kubeconfig: replaced by the kubeconfig on saas.server in
  18.0.55.0.0, whose migration already copied the data over.
"""


def migrate(cr, version):
    cr.execute("DROP TABLE IF EXISTS saas_instance_package CASCADE")
    cr.execute("DROP TABLE IF EXISTS saas_kubeconfig CASCADE")
