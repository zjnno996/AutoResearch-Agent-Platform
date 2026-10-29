"""Vendor selected, pinned upstream skills without installing or running them.

Input archives are inspected source snapshots, not executable installers. The
existing frozen candidates keep the previous library through a shared snapshot.
"""
from pathlib import Path, PurePosixPath
import hashlib
import json
import os
import shutil
import tarfile

PROJECT = Path(__file__).resolve().parents[1]
SKILLS = PROJECT / 'backend/agent/researchclaw/skills'
BENCHMARKS = Path(os.environ.get('EMNETS_BENCHMARKS_DIR', str(PROJECT / '.local/benchmarks')))
ARCHIVES = Path(os.environ.get('EMNETS_SKILL_ARCHIVES_DIR', str(BENCHMARKS / 'upstream/skills')))
SOURCES = {
    'anthropic': ('anthropics--skills', 'Apache-2.0', [
        'frontend-design', 'mcp-builder', 'skill-creator', 'webapp-testing',
        'web-artifacts-builder', 'theme-factory', 'canvas-design', 'algorithmic-art']),
    'superpowers': ('obra--superpowers', 'MIT', [
        'systematic-debugging', 'verification-before-completion', 'test-driven-development',
        'writing-plans', 'receiving-code-review', 'requesting-code-review', 'writing-skills']),
    'trailofbits': ('trailofbits--skills', 'CC-BY-SA-4.0', [
        'dimensional-analysis', 'property-based-testing', 'modern-python', 'spec-to-code-compliance',
        'differential-review', 'mutation-testing', 'post-patch-validation', 'audit-context-building',
        'writing-lean-proofs', 'coverage-analysis']),
}


def preserve_frozen_library():
    library = SKILLS / 'library'
    old_lock = (SKILLS / 'sources.lock.json').read_bytes()
    identity = hashlib.sha256(old_lock).hexdigest()
    snapshot = (BENCHMARKS / 'skill_snapshots') / identity / 'library'
    if not snapshot.exists():
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(library, snapshot)
        (snapshot.parent / 'sources.lock.json').write_bytes(old_lock)
    for candidate in (BENCHMARKS / 'candidates').glob('*/researchclaw/skills/library'):
        if candidate.is_symlink() and candidate.resolve() == library.resolve():
            candidate.unlink()
            candidate.symlink_to(snapshot, target_is_directory=True)


def vendor():
    preserve_frozen_library()
    library = SKILLS / 'library'
    lock = json.loads((SKILLS / 'sources.lock.json').read_text())
    installed = []
    for provider, (name, license_name, selected) in SOURCES.items():
        metadata = json.loads((ARCHIVES / (name + '.json')).read_text())
        with tarfile.open(metadata['archive']) as tar:
            members = tar.getmembers()
            prefix = members[0].name.split('/')[0] + '/'
            paths = {member.name[len(prefix):]: member for member in members if member.isfile()}
            source_prefixes = {}
            for skill in selected:
                matches = [path.removesuffix('/SKILL.md') + '/' for path in metadata['skills']
                           if path.endswith('/' + skill + '/SKILL.md') and '/tests/' not in path]
                if len(matches) != 1:
                    raise ValueError('Skill directory must be unambiguous: ' + skill)
                source_prefixes[matches[0]] = skill
            size = 0
            for relative, member in paths.items():
                chosen = next(((folder, skill) for folder, skill in source_prefixes.items()
                               if relative.startswith(folder)), None)
                if not chosen:
                    continue
                folder, skill = chosen
                suffix = PurePosixPath(relative[len(folder):])
                if '..' in suffix.parts or suffix.is_absolute() or member.size > 4 * 1024 * 1024:
                    raise ValueError('Unsafe or oversized skill resource')
                size += member.size
                if size > 64 * 1024 * 1024:
                    raise ValueError('Selected source exceeds expanded byte budget')
                target = library / provider / skill / suffix
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(tar.extractfile(member).read())
            if 'LICENSE' in paths:
                (library / provider / 'LICENSE').write_bytes(tar.extractfile(paths['LICENSE']).read())
            for folder, skill in source_prefixes.items():
                if provider == 'anthropic':
                    license_path = library / provider / skill / 'LICENSE.txt'
                    if not license_path.is_file() or 'Apache License' not in license_path.read_text():
                        raise ValueError('Selected Anthropic skill must have Apache licensing')
                installed.append(provider + '/' + skill)
        entry = {'repository': 'https://github.com/' + metadata['repo'], 'revision': metadata['revision'],
                 'directory': provider, 'license': license_name, 'skill_count': len(selected),
                 'selection': selected, 'retrieved_at': '2026-10-28'}
        lock['sources'] = [source for source in lock['sources'] if source.get('directory') != provider] + [entry]
    lock['retrieved_at'] = '2026-10-28'
    for provider in SOURCES:
        for path in sorted((library / provider).rglob('*')):
            if path.is_file():
                lock['files'][path.relative_to(library).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    (SKILLS / 'sources.lock.json').write_text(json.dumps(lock, indent=2, ensure_ascii=False) + '\n')
    print(json.dumps({'added_skills': len(installed), 'installed': installed,
                      'sources': len(lock['sources']), 'pinned_resources': len(lock['files'])}))


if __name__ == '__main__':
    vendor()
