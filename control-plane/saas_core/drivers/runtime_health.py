"""Read-only tenant observations. Lifecycle intent never proves availability."""
import json
from urllib.parse import urlsplit

import requests


def observe_runtime(driver, handle, url, lifecycle):
    def result(state, reason, message, workload='unknown', reachable=None):
        return dict(runtime_state=state, runtime_reason=reason,
                    runtime_message=message, runtime_workload=workload,
                    runtime_reachable=reachable)

    try:
        health = driver.health(handle)
    except Exception:
        return result('unknown', 'cluster_unreachable',
                      'Cannot check the tenant workload: cluster access failed.')
    workload = health.status or 'unknown'
    if lifecycle in ('stopped', 'suspended'):
        if workload == 'exited':
            return result(lifecycle, lifecycle, 'Tenant workload is stopped.', workload)
        if workload in ('not_found', 'missing'):
            return result('unavailable', 'workload_missing', 'Tenant workload is missing.', workload)
        return result('stopping', 'stopping', 'Waiting for the tenant workload to stop.', workload)
    if not health.running:
        if workload in ('not_found', 'missing'):
            return result('unavailable', 'workload_missing', 'Tenant workload is missing.', workload)
        if workload in ('dead', 'exited') or health.detail in (
                'CrashLoopBackOff', 'ImagePullBackOff', 'ErrImagePull', 'InvalidImageName',
                'CreateContainerConfigError', 'CreateContainerError', 'RunContainerError'):
            return result('unavailable', 'workload_failed', 'Tenant workload has stopped or failed.', workload)
        return result('starting', 'pod_not_ready', 'Tenant workload is not ready to serve requests.', workload)

    # Use only the configured tenant origin. No redirects, cookies, credentials,
    # or disabled TLS verification. Odoo's health endpoint checks PostgreSQL too.
    origin = urlsplit(url or '')
    if origin.scheme not in ('http', 'https') or not origin.hostname or origin.username or origin.password:
        return result('unknown', 'endpoint_missing', 'Tenant endpoint is not configured.', workload)
    endpoint = '%s://%s/web/health?db_server_status=1' % (origin.scheme, origin.netloc)
    try:
        with requests.get(endpoint, timeout=(2, 3), allow_redirects=False, stream=True,
                          headers={'Accept': 'application/json'}) as response:
            if response.status_code != 200:
                return result('unavailable', 'endpoint_error',
                              'Tenant endpoint returned HTTP %s.' % response.status_code, workload, True)
            # Bound response size; an arbitrary web page is not Odoo readiness.
            body = bytearray()
            for chunk in response.iter_content(chunk_size=8193):
                body.extend(chunk)
                if len(body) > 8192:
                    raise ValueError('health response too large')
            payload = json.loads(body)
            if not isinstance(payload, dict) or payload.get('status') != 'pass' or payload.get('db_server_status') is not True:
                return result('unavailable', 'invalid_health_response',
                              'Tenant endpoint did not confirm Odoo and database health.', workload, True)
    except requests.RequestException:
        return result('unreachable', 'endpoint_unreachable',
                      'Tenant URL is unreachable from the platform (DNS, TLS, or connection failure).', workload, False)
    except (ValueError, StopIteration):
        return result('unavailable', 'invalid_health_response',
                      'Tenant endpoint did not return a valid Odoo health response.', workload, True)
    return result('online', 'healthy', 'Tenant workload, public endpoint, and database checks passed.', workload, True)
