from odoo import fields, models, _
from odoo.exceptions import UserError


class SaasDockerContainer(models.Model):
    _name = 'saas.docker.container'
    _description = 'Docker Container'
    _order = 'name'

    server_id = fields.Many2one(
        'saas.server',
        string='Docker Server',
        required=True,
        ondelete='cascade',
        help='The Docker host server where this container is running.',
    )
    container_id = fields.Char(
        string='Container ID',
        readonly=True,
        help='Short 12-character Docker container identifier.',
    )
    name = fields.Char(
        string='Container Name',
        readonly=True,
        help='Docker container name assigned at creation.',
    )
    image = fields.Char(
        string='Image',
        readonly=True,
        help='Docker image and tag this container was started from.',
    )
    command = fields.Char(
        string='Command',
        readonly=True,
        help='Entrypoint command running inside the container.',
    )
    created = fields.Char(
        string='Created',
        readonly=True,
        help='Date and time when the container was created.',
    )
    status = fields.Char(
        string='Status',
        readonly=True,
        help='Current container status reported by Docker (e.g. "Up 3 hours", "Exited (0)").',
    )
    ports = fields.Char(
        string='Ports',
        readonly=True,
        help='Port mappings between the host and the container.',
    )

    def _driver_handle(self):
        """Build a ComputeDriver + ComputeHandle for this raw container record.
        Container-level ops (stop/restart) need only server + container name."""
        from ..drivers.ssh_docker_driver import SshDockerDriver
        from ..drivers.base import ComputeHandle
        return (SshDockerDriver(self.server_id),
                ComputeHandle(server_id=self.server_id.id,
                              container_name=self.name, instance_path=''))

    def action_stop_container(self):
        """Stop this Docker container via the ComputeDriver."""
        self.ensure_one()
        driver, handle = self._driver_handle()
        try:
            driver.stop(handle)
        except Exception as e:
            raise UserError(_("Failed to stop container '%s':\n%s") % (self.name, e))
        return self.server_id.action_refresh_containers()

    def action_restart_container(self):
        """Restart this Docker container via the ComputeDriver."""
        self.ensure_one()
        driver, handle = self._driver_handle()
        try:
            driver.restart(handle)
        except Exception as e:
            raise UserError(_("Failed to restart container '%s':\n%s") % (self.name, e))
        return self.server_id.action_refresh_containers()

    def action_view_logs(self):
        """Open a live log stream for this container."""
        self.ensure_one()
        return {
            'type': 'ir.actions.client',
            'tag': 'container_logs_stream',
            'name': _("Logs: %s") % self.name,
            'context': {
                'stream_url': '/saas/container/%d/logs/stream' % self.id,
                'container_name': self.name,
                'tail': 100,
            },
        }
