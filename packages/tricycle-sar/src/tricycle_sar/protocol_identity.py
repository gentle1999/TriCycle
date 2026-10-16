"""Separate model/result semantics from post-result storage and delivery identity.

Selectors are explicit and fail closed when a mixed-file boundary changes. Unknown
files remain scientific dependencies. This is a reviewed dependency partition,
not an automatic proof of arbitrary program equivalence.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
from collections.abc import Callable
from pathlib import Path

from tricycle_core.training_protocol import protocol_file_source

SCHEMA = 'tricycle.sar.scoped-identity/v1'


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def scientific_source(target: str, content: bytes) -> bytes:
    """Retain computations and environment setup; exclude only reviewed output tails."""
    if target == 'common/sar_worker_common.sh':
        text = content.decode()
        match = re.fullmatch(r'(.*)\npreserve_outputs\(\) \{\n.*\n\}\ntrap preserve_outputs EXIT\n', text, re.DOTALL)
        if match is None:
            raise ValueError('Worker output-hook boundary changed; review the identity selector')
        return match[1].encode()
    if target == 'common/sar_outputs.py':
        tree = ast.parse(content)
        found = False
        body = []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in {'checkpoint_files', 'export_diagnostics'}:
                continue
            if isinstance(node, ast.FunctionDef) and node.name == 'collect':
                boundaries = [i for i, statement in enumerate(node.body)
                              if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)
                              and isinstance(statement.value.func, ast.Name) and statement.value.func.id == 'write'
                              and statement.value.args and isinstance(statement.value.args[0], ast.BinOp)
                              and isinstance(statement.value.args[0].right, ast.Constant)
                              and statement.value.args[0].right.value == 'status.csv']
                if len(boundaries) != 1:
                    raise ValueError('Result serialization boundary changed; review the identity selector')
                boundary = boundaries[0]
                # The remaining tail must begin with the known artifact-retention branch.
                tail = node.body[boundary + 1:]
                if tail and isinstance(tail[0], ast.Expr) and isinstance(tail[0].value, ast.Call):
                    call = tail[0].value
                    if not isinstance(call.func, ast.Name) or call.func.id != 'export_diagnostics':
                        raise ValueError('Unknown post-result action')
                    tail = tail[1:]
                if not tail or not isinstance(tail[0], ast.If) or 'retain_checkpoint' not in ast.unparse(tail[0].test):
                    raise ValueError('Artifact retention boundary changed')
                node.body = node.body[:boundary + 1]
                found = True
            body.append(node)
        if not found:
            raise ValueError('Missing result collection function')
        tree.body = body
        return ast.dump(tree, include_attributes=False).encode()
    return content


def identity(method: dict, source: Callable[[dict], bytes]) -> dict:
    components = []
    for entry in sorted(method['files'], key=lambda e: e['path']):
        if entry['external']:
            scientific = entry['sha256']
        else:
            content = source(entry)
            if hashlib.sha256(content).hexdigest() != entry['sha256']:
                raise ValueError('Identity source differs from delivery binding')
            scientific = hashlib.sha256(scientific_source(entry['path'], content)).hexdigest()
        components.append({'path': entry['path'], 'scientific_sha256': scientific,
                           'delivery_sha256': entry['sha256'], 'external': entry['external']})
    # Scheduling location is not a numerical setting. Image, machine/accelerator,
    # environment mode and all other execution parameters remain bound.
    context = {k: v for k, v in method.items() if k not in {
        'files', 'fingerprint', 'identity', 'executor', 'environment', 'run',
        'bundle_name', 'entrypoint', 'generated_inputs', 'config_template', 'config_name'}}
    science = [{k: v for k, v in c.items() if k != 'delivery_sha256'} for c in components]
    return {'schema': SCHEMA, 'scientific_sha256': digest({'schema': SCHEMA, 'context': context, 'components': science}),
            'delivery_sha256': method['fingerprint'], 'context': context, 'components': components,
            'selector_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def current_identity(manifest: Path, method: dict) -> dict:
    return identity(method, lambda entry: protocol_file_source(manifest, entry).read_bytes())


def compare(left: dict, right: dict) -> dict:
    """Report comparability separately from exact delivery reproducibility."""
    if left['schema'] != SCHEMA or right['schema'] != SCHEMA or left['selector_sha256'] != right['selector_sha256']:
        raise ValueError('Identity policies differ; explicit review is required')
    return {'same_scientific_method': left['scientific_sha256'] == right['scientific_sha256'],
            'same_delivery': left['delivery_sha256'] == right['delivery_sha256']}
