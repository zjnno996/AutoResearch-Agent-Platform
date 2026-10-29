"""Run held method witnesses and the real experiment within one time allocation."""
from __future__ import annotations

import shlex
import shutil
import time
from pathlib import Path
from typing import Any

from researchclaw.orchestration.artifacts import read_json
from researchclaw.orchestration.compact_state import file_hash, fingerprint


HELD_MANIFEST = 'stage-09/method_probes/manifest.json'
DISCLAIMER = ('Analytic/metamorphic probes can falsify specified mechanisms. Passing probes and '
              'observed calls do not prove a theorem, complete method equivalence or a scientific hypothesis.')


def execute_science(root: Path, work: Path, plan: dict[str, Any], python: str,
                    arguments: list[str], archive: Path, runtime_seconds: float):
    """Return tool outcome, actual command, method evidence and an optional gap.

    The held specifications are read, never regenerated from implementation.
    Oracle execution and the full experiment share this attempt's allocation.
    """
    from researchclaw.pipeline.claw_engine.tools.executor import ToolExecutor
    from researchclaw.orchestration.method_probes import runner_arguments, read_method_receipt
    from researchclaw.orchestration.compact_generation import source_files

    command = shlex.join([python, 'main.py', *arguments]) + ' > outputs/stdout.log 2> outputs/stderr.log'
    executor = ToolExecutor(work, bash_timeout=runtime_seconds, python_path=python,
                            journal_path=archive / 'tools.jsonl')
    if not plan.get('method_probes'):
        result = executor.execute_structured('bash', {'command': command})
        return result, command, {'status': 'not_applicable', 'passed': False,
                                'note': plan.get('method_probe_note', 'Legacy plan has no held method probes.'),
                                'limitations': DISCLAIMER, 'receipt_hashes': {}}, ''

    manifest = root / HELD_MANIFEST
    # Read before executing generated code. A missing oracle is an evidence
    # failure, never an invitation to let implementation generate its own test.
    if not manifest.is_file() or manifest.is_symlink():
        raise ValueError('Held method-probe manifest is missing or unsafe')
    pinned = file_hash(manifest)
    evidence: dict[str, Any] = {'status': 'failed', 'passed': False, 'manifest_path': HELD_MANIFEST,
                                'manifest_sha256': pinned, 'limitations': DISCLAIMER, 'receipt_hashes': {}}
    started = time.monotonic()
    pinned_sources = {path.relative_to(work).as_posix(): file_hash(path) for path in source_files(work)}
    pilot_receipt = archive / 'method-pilot.json'
    pilot = shlex.join([python, *runner_arguments(manifest, work, pilot_receipt, 'pilot',
                                                 expected_manifest_sha256=pinned)])
    pilot += ' > ' + shlex.quote(str(archive / 'method-pilot.stdout.log'))
    pilot += ' 2> ' + shlex.quote(str(archive / 'method-pilot.stderr.log'))
    # Startup/import cost is part of this shared allocation. A short fixed
    # deadline can expire before a complete scientific verdict, then force
    # uninformed code rewrites. Retry only an incomplete timeout, never a failed assert.
    pilot_limit = min(runtime_seconds, max(30.0, min(120.0, runtime_seconds*.25)))
    evidence['pilot_attempts'] = []
    for attempt in range(2):
        remaining = runtime_seconds-(time.monotonic()-started)
        if remaining <= 0:
            if not evidence['pilot_attempts']:
                raise ValueError('Shared experiment runtime exhausted before the held method pilot')
            break
        executor.bash_timeout = min(pilot_limit, remaining)
        outcome = executor.execute_structured('bash', {'command': pilot})
        evidence['pilot_attempts'].append({'attempt': attempt+1, 'timeout_sec': executor.bash_timeout,
            'timed_out': outcome.timed_out, 'exit_code': outcome.exit_code})
        if not outcome.timed_out or attempt == 1:
            break
        if file_hash(manifest) != pinned:
            return outcome, pilot, evidence, 'Held method oracle changed during timed-out pilot execution'
        current_sources = {path.relative_to(work).as_posix(): file_hash(path) for path in source_files(work)}
        if current_sources != pinned_sources:
            return outcome, pilot, evidence, 'Implementation source changed during timed-out pilot execution'
        for name in ('method-pilot.json', 'method-pilot.stdout.log', 'method-pilot.stderr.log'):
            path = archive/name
            if path.is_file():
                shutil.copy2(path, archive/('timed-out-'+name))
        pilot_receipt.unlink(missing_ok=True)
    if outcome.timed_out:
        return outcome, pilot, evidence, ('Held method pilot timed out after bounded unchanged attempts; '
            'no completed scientific witness verdict. Shared runtime allocation was not reset.')
    if pilot_receipt.is_file():
        evidence['receipt_hashes'][pilot_receipt.relative_to(root).as_posix()] = file_hash(pilot_receipt)
    try:
        evidence['pilot'] = read_method_receipt(manifest, work, pilot_receipt, 'pilot')
        if outcome.is_error:
            raise ValueError('Method pilot subprocess failed despite its receipt')
    except (OSError, ValueError, TypeError) as exc:
        return outcome, pilot, evidence, 'Held method pilot failed: ' + str(exc)
    if file_hash(manifest) != pinned:
        return outcome, pilot, evidence, 'Held method oracle changed during pilot execution'
    remaining = runtime_seconds - (time.monotonic() - started)
    if remaining <= 0:
        return outcome, pilot, evidence, 'Method pilot exhausted the shared experiment runtime allocation'

    observation = archive / 'method-observation.json'
    command = shlex.join([python, *runner_arguments(manifest, work, observation, 'observe', arguments=arguments,
                                                   expected_manifest_sha256=pinned)])
    command += ' > outputs/stdout.log 2> outputs/stderr.log'
    executor.bash_timeout = remaining
    outcome = executor.execute_structured('bash', {'command': command})
    if observation.is_file():
        evidence['receipt_hashes'][observation.relative_to(root).as_posix()] = file_hash(observation)
    try:
        evidence['observe'] = read_method_receipt(manifest, work, observation, 'observe')
        if outcome.is_error:
            raise ValueError('Observed full experiment subprocess failed despite its receipt')
        if file_hash(manifest) != pinned:
            raise ValueError('Held method oracle changed during full experiment execution')
    except (OSError, ValueError, TypeError) as exc:
        return outcome, command, evidence, 'Full-run method linkage failed: ' + str(exc)
    evidence.update(status='passed', passed=True, duration_sec=round(time.monotonic() - started, 3))
    return outcome, command, evidence, ''


def audit_method_execution(root: Path, work: Path, plan: dict[str, Any],
                           execution_receipt: dict[str, Any], *,
                           evidence_origin_root: Path | None = None) -> dict[str, Any]:
    """Recheck held artifacts, executed bindings and receipt hashes for review/export."""
    from researchclaw.orchestration.method_probes import read_method_receipt
    if not plan.get('method_probes'):
        return {'passed': True, 'status': 'not_applicable', 'method_validated': False,
                'limitations': DISCLAIMER}
    evidence = execution_receipt.get('method_probes', {})
    manifest = root / HELD_MANIFEST
    errors = []
    if evidence.get('status') != 'passed' or evidence.get('passed') is not True:
        errors.append('No successful held method witnesses and full-run linkage')
    if not manifest.is_file() or manifest.is_symlink() or evidence.get('manifest_sha256') != file_hash(manifest):
        errors.append('Held method specification changed or is missing')
    method_identity = fingerprint({key: plan.get(key) for key in ('method_steps', 'method_probes', 'method_probe_note')})
    if read_json(manifest).get('plan_sha256') != method_identity:
        errors.append('Held method specification differs from the approved executable plan')
    paths = evidence.get('receipt_hashes', {})
    if not isinstance(paths, dict) or len(paths) != 2:
        errors.append('Method evidence needs both held-pilot and full-run receipts')
        paths = {}
    modes = set()
    for relative, digest in paths.items():
        part = Path(relative)
        path = root / part
        if (part.is_absolute() or '..' in part.parts or not path.resolve().is_relative_to(root.resolve())
                or path.is_symlink() or not path.is_file() or file_hash(path) != digest):
            errors.append('Method execution receipt changed or is missing: ' + str(relative))
            continue
        mode = 'pilot' if path.name == 'method-pilot.json' else 'observe' if path.name == 'method-observation.json' else ''
        if not mode or mode in modes:
            errors.append('Invalid or repeated method receipt mode')
            continue
        modes.add(mode)
        try:
            read_method_receipt(manifest, work, path, mode, evidence_origin_root=evidence_origin_root)
        except (OSError, ValueError, TypeError) as exc:
            errors.append(str(exc))
    if modes != {'pilot', 'observe'}:
        errors.append('Both independent pilot and full-run observation are required')
    return {'passed': not errors, 'status': 'passed' if not errors else 'failed',
            'errors': errors, 'limitations': DISCLAIMER}
