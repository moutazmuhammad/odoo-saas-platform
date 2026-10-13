#!/usr/bin/env python3
"""Read-only build-pool configuration and current placement checks; no resources created."""
import argparse
import json
import subprocess

KEY = 'saas.platform/pool'
VALUE = 'build'


def tolerates_build(t):
    return t.get('effect', '') in ('', 'NoSchedule') and (
        (t.get('operator') == 'Exists' and t.get('key', '') in ('', KEY)) or
        (t.get('operator', 'Equal') == 'Equal' and t.get('key') == KEY and t.get('value') == VALUE))


def build_placement(spec):
    terms = spec.get('affinity', {}).get('nodeAffinity', {}).get(
        'requiredDuringSchedulingIgnoredDuringExecution', {}).get('nodeSelectorTerms', [])
    return bool(terms) and all(any(
        e.get('key') == KEY and e.get('operator') == 'In' and e.get('values') == [VALUE]
        for e in term.get('matchExpressions', [])) for term in terms) and any(
            t.get('key') == KEY and t.get('operator', 'Equal') == 'Equal'
            and t.get('value') == VALUE and t.get('effect') == 'NoSchedule'
            for t in spec.get('tolerations', [])) and not spec.get('shareProcessNamespace', False)


def audit(nodes, pods, jobs, pools=None, expectation=None):
    errors = []
    build_nodes = set()
    for node in nodes:
        name = node['metadata']['name']
        labelled = node['metadata'].get('labels', {}).get(KEY) == VALUE
        tainted = any(t.get('key') == KEY and t.get('value') == VALUE and t.get('effect') == 'NoSchedule'
                      for t in node.get('spec', {}).get('taints', []))
        if labelled != tainted:
            errors.append('Node %s must have both the build label and NoSchedule taint.' % name)
        if labelled:
            build_nodes.add(name)
    active_jobs = 0
    for job in jobs:
        if job.get('status', {}).get('succeeded') or any(
                c.get('type') == 'Failed' and c.get('status') == 'True'
                for c in job.get('status', {}).get('conditions', [])):
            continue
        active_jobs += 1
        if not build_placement(job['spec']['template']['spec']):
            errors.append('Active build Job %s lacks mandatory pool isolation.' % job['metadata']['name'])
    running = 0
    infrastructure = []
    for pod in pods:
        if pod.get('status', {}).get('phase') in ('Succeeded', 'Failed'):
            continue
        metadata, spec = pod['metadata'], pod['spec']
        namespace, name = metadata['namespace'], metadata['name']
        node = spec.get('nodeName')
        if namespace == 'odoo-builds':
            if not build_placement(spec):
                errors.append('Build pod %s lacks mandatory pool isolation.' % name)
            if node and node not in build_nodes:
                errors.append('Build pod %s is assigned to a non-build node.' % name)
            if pod.get('status', {}).get('phase') == 'Running' and node in build_nodes:
                running += 1
        else:
            daemon = namespace in ('kube-system', 'monitoring') and any(
                owner.get('kind') == 'DaemonSet' and owner.get('controller')
                for owner in metadata.get('ownerReferences', []))
            if not daemon and (node in build_nodes or any(tolerates_build(t) for t in spec.get('tolerations', []))):
                errors.append('Non-build pod %s/%s can access the build pool.' % (namespace, name))
            if daemon and node in build_nodes:
                infrastructure.append(namespace + '/' + name)
    if pools is not None:
        candidates = [p for p in pools if p.get('name') == 'build']
        if len(candidates) != 1:
            errors.append('DOKS must have exactly one pool named build.')
        else:
            pool = candidates[0]
            if pool.get('auto_scale') is not True or pool.get('min_nodes') != 0 or pool.get('max_nodes', 0) < 1:
                errors.append('DOKS build pool must autoscale with min_nodes=0 and max_nodes>=1.')
            if pool.get('labels', {}).get(KEY) != VALUE or not any(
                    t.get('key') == KEY and t.get('value') == VALUE and t.get('effect') == 'NoSchedule'
                    for t in pool.get('taints', [])):
                errors.append('DOKS pool template must persist the build label and taint, including at zero nodes.')
            if expectation == 'zero' and pool.get('nodes'):
                errors.append('DOKS build pool still contains nodes.')
        if not any(p.get('name') != 'build' and p.get('nodes') and (
                not p.get('auto_scale') or p.get('min_nodes', 0) >= 1) for p in pools):
            errors.append('Keep at least one non-build pool node available for platform services.')
    if expectation == 'running' and running == 0:
        errors.append('No Running build pod was observed on the build pool.')
    if expectation == 'zero' and (build_nodes or active_jobs):
        errors.append('Build nodes or active build Jobs still exist; zero is not verified.')
    return errors, {'build_nodes': len(build_nodes), 'running_build_pods': running,
                    'active_build_jobs': active_jobs, 'infrastructure_daemonsets': infrastructure}


def read(args):
    return json.loads(subprocess.run(args, check=True, capture_output=True, text=True).stdout)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--context', required=True, help='Explicit administrator Kubernetes context')
    parser.add_argument('--kubeconfig')
    parser.add_argument('--doks-cluster', help='Explicit DOKS ID/name; enables provider template checks at zero nodes')
    parser.add_argument('--doctl-context', help='Optional doctl account context')
    state = parser.add_mutually_exclusive_group()
    state.add_argument('--expect-zero', action='store_true')
    state.add_argument('--expect-running', action='store_true')
    args = parser.parse_args(argv)
    base = ['kubectl', '--context', args.context]
    if args.kubeconfig:
        base += ['--kubeconfig', args.kubeconfig]
    try:
        nodes = read([*base, 'get', 'nodes', '-o', 'json'])['items']
        pods = read([*base, 'get', 'pods', '--all-namespaces', '-o', 'json'])['items']
        jobs = read([*base, 'get', 'jobs', '-n', 'odoo-builds', '-o', 'json'])['items']
        pools = None
        if args.doks_cluster:
            provider = ['doctl'] + (['--context', args.doctl_context] if args.doctl_context else [])
            pools = read([*provider, 'kubernetes', 'cluster', 'node-pool', 'list', args.doks_cluster, '--output', 'json'])
        errors, evidence = audit(nodes, pods, jobs, pools,
                                 'zero' if args.expect_zero else 'running' if args.expect_running else None)
    except (subprocess.CalledProcessError, OSError, ValueError, KeyError, TypeError) as exc:
        print('FAIL: could not read cluster/provider state: ' + str(exc))
        return 1
    print(json.dumps(evidence, sort_keys=True))
    for error in errors:
        print('FAIL: ' + error)
    if pools is None:
        print('Provider pool template/autoscaling not checked; supply --doks-cluster for DOKS acceptance.')
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
