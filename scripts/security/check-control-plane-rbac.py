#!/usr/bin/env python3
"""Read-only P0.1 permission checks and optional server-side admission dry runs.

Requires an administrator kubeconfig/context to impersonate the real account,
including its authenticated and ServiceAccount groups. No permissions are changed.
"""
import argparse
import json
import subprocess


def checks(tenants, monitoring, prometheus):
    result = [
        (True, ['create', 'odooinstances']),
        (True, ['patch', 'odooinstances']),
        (True, ['delete', 'odooinstances']),
        (True, ['list', 'namespaces']),
        (True, ['list', 'nodes']),
        (True, ['list', 'deployments.apps', '--all-namespaces']),
        (True, ['create', 'jobs.batch', '-n', 'odoo-builds']),
        (True, ['create', 'secrets', '-n', 'odoo-builds']),
        (True, ['get', 'pods', '--subresource=log', '-n', 'odoo-builds']),
        (True, ['create', 'pods', '-n', 'saas-toolbox']),
        (True, ['get', 'pods/saas-toolbox', '--subresource=exec', '-n', 'saas-toolbox']),
        (True, ['get', 'services/' + prometheus, '--subresource=proxy', '-n', monitoring]),
        (True, ['get', 'configmaps/cluster-autoscaler-status', '-n', 'kube-system']),
        (False, ['create', 'namespaces']),
        (False, ['delete', 'namespaces']),
        (False, ['create', 'deployments.apps', '--all-namespaces']),
        (False, ['list', 'secrets', '--all-namespaces']),
        (False, ['create', 'pods', '-n', 'odoo-system']),
        (False, ['create', 'pods', '-n', 'odoo-builds']),
        (False, ['create', 'rolebindings.rbac.authorization.k8s.io', '-n', 'odoo-builds']),
        (False, ['create', 'clusterrolebindings.rbac.authorization.k8s.io']),
        (False, ['create', 'serviceaccounts', '--subresource=token', '-n', 'kube-system']),
        (False, ['get', 'configmaps/unrelated', '-n', 'kube-system']),
        (False, ['get', 'services/unrelated', '--subresource=proxy', '-n', monitoring]),
    ]
    for namespace in ('ingress', 'cert-manager', 'veltnex-mail', monitoring, 'kube-system', 'odoo-system'):
        result.append((False, ['get', 'secrets', '-n', namespace]))
    for namespace in tenants:
        result.extend([
            (True, ['list', 'pods', '-n', namespace]),
            (True, ['get', 'pods', '--subresource=exec', '-n', namespace]),
            (True, ['get', 'secrets', '-n', namespace]),
            (True, ['create', 'jobs.batch', '-n', namespace]),
            (False, ['create', 'serviceaccounts', '--subresource=token', '-n', namespace]),
            (False, ['create', 'rolebindings.rbac.authorization.k8s.io', '-n', namespace]),
        ])
    return result


def impersonation(namespace, account):
    return ['--as=system:serviceaccount:' + namespace + ':' + account,
            '--as-group=system:authenticated', '--as-group=system:serviceaccounts',
            '--as-group=system:serviceaccounts:' + namespace]


def admission_checks(base):
    passed = True
    for namespace in ('odoo-builds', 'saas-toolbox'):
        pod = {'apiVersion': 'v1', 'kind': 'Pod',
               'metadata': {'generateName': 'veltnex-admission-check-', 'namespace': namespace},
               'spec': {'restartPolicy': 'Never', 'automountServiceAccountToken': False,
                        'securityContext': {'runAsNonRoot': True, 'runAsUser': 1000,
                                            'seccompProfile': {'type': 'RuntimeDefault'}},
                        'containers': [{'name': 'check', 'image': 'registry.k8s.io/pause:3.10',
                                        'securityContext': {'allowPrivilegeEscalation': False,
                                                            'capabilities': {'drop': ['ALL']}}}]}}
        if namespace == 'odoo-builds':
            pod['spec']['affinity'] = {'nodeAffinity': {'requiredDuringSchedulingIgnoredDuringExecution': {
                'nodeSelectorTerms': [{'matchExpressions': [
                    {'key': 'saas.platform/pool', 'operator': 'In', 'values': ['build']}]}]}}}
            pod['spec']['tolerations'] = [{'key': 'saas.platform/pool', 'operator': 'Equal',
                                         'value': 'build', 'effect': 'NoSchedule'}]
        for hostile in (False, True):
            pod['spec']['containers'][0]['securityContext']['privileged'] = hostile
            result = subprocess.run([*base, 'create', '--dry-run=server', '-f', '-', '-o', 'name'],
                                    input=json.dumps(pod), capture_output=True, text=True)
            ok = result.returncode == 0 if not hostile else (
                result.returncode != 0 and 'veltnex-control-plane-workloads' in result.stderr)
            print(('PASS' if ok else 'FAIL') + ' admission ' + namespace + (' privileged rejected' if hostile else ' normal allowed'))
            if not ok:
                print(result.stderr.strip())
            passed &= ok
    return passed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--context', required=True)
    parser.add_argument('--kubeconfig')
    parser.add_argument('--service-account', default='saas-control-plane')
    parser.add_argument('--service-account-namespace', default='kube-system')
    parser.add_argument('--tenant-namespace', action='append', default=[])
    parser.add_argument('--monitoring-namespace', default='monitoring')
    parser.add_argument('--prometheus-service', default='prometheus-server:80')
    parser.add_argument('--check-admission', action='store_true')
    args = parser.parse_args(argv)
    base = ['kubectl', '--context', args.context]
    if args.kubeconfig:
        base += ['--kubeconfig', args.kubeconfig]
    passed = True
    identities = [(args.service_account_namespace, args.service_account,
                   checks(args.tenant_namespace, args.monitoring_namespace, args.prometheus_service)),
                  ('saas-toolbox', 'saas-toolbox', [
                      (True, ['list', 'nodes']),
                      (False, ['list', 'secrets', '--all-namespaces']),
                      (False, ['create', 'pods', '-n', 'kube-system']),
                      (False, ['create', 'clusterrolebindings.rbac.authorization.k8s.io']),
                      (False, ['create', 'odooinstances']),
                  ])]
    for namespace, account, permissions in identities:
        for expected, request in permissions:
            result = subprocess.run([*base, *impersonation(namespace, account), 'auth', 'can-i', *request],
                                    capture_output=True, text=True)
            answer = result.stdout.strip()
            ok = ((result.returncode == 0 and answer == 'yes') if expected else
                  (result.returncode == 1 and answer == 'no'))
            print(('PASS' if ok else 'FAIL') + ' ' + namespace + '/' + account + ' ' + ' '.join(request)
                  + ' expected=' + ('yes' if expected else 'no'))
            if answer not in ('yes', 'no'):
                print(result.stderr.strip())
            passed &= ok
    if args.check_admission:
        passed &= admission_checks(base)
    if not args.tenant_namespace:
        print('Tenant permissions were not checked; pass --tenant-namespace for each test tenant.')
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
