"""Bounded read-only inspection of numeric arrays and image artifacts."""
from pathlib import Path
from typing import Any
import zipfile


def array_checks(path: Path, check: dict[str, Any], errors: list[str]) -> dict[str, Any]:
    import numpy as np
    if path.suffix.lower() == '.npz':
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if (len(members) > 256 or sum(item.file_size for item in members) > 128 * 1024 * 1024
                    or any(item.file_size > 64 * 1024 * 1024 for item in members)):
                raise ValueError('Array archive exceeds bounded member/uncompressed-size limits; unverified')
        data = np.load(path, allow_pickle=False)
        names = data.files
        aliases = {}
    else:
        data = np.load(path, allow_pickle=False, mmap_mode='r')
        names, aliases = ['array'], {path.stem: 'array'}
    try:
        def resolve(name: str) -> str:
            return aliases.get(name, name)
        for name in check.get('required_arrays', []):
            if resolve(name) not in names:
                errors.append(f'Required numeric array is missing: {name}')
        predicates = {}
        for key in ('finite_arrays', 'nonempty_arrays'):
            value = check.get(key, [])
            predicates[key] = set(names if value is True or value == 'all' else (resolve(name) for name in value))
        selected = set().union(*predicates.values())
        metadata = {}
        for name in sorted(selected):
            if name not in names:
                errors.append(f'Checked array is missing: {name}')
                continue
            array = data[name] if path.suffix.lower() == '.npz' else data
            if not isinstance(array, np.ndarray):
                raise ValueError(f'Archive member {name} is not a numeric array')
            info = {'shape': list(array.shape), 'dtype': str(array.dtype), 'size': int(array.size)}
            if name in predicates['nonempty_arrays'] and array.size == 0:
                errors.append(f'Array {name} is empty')
            if name in predicates['finite_arrays']:
                if array.dtype.kind not in 'biufc':
                    errors.append(f'Array {name} is not a plain numeric array')
                    info['all_finite'] = False
                else:
                    values = array.ravel(order='K')
                    finite = all(bool(np.isfinite(values[start:start+1_000_000]).all())
                                 for start in range(0, values.size, 1_000_000))
                    info['all_finite'] = finite
                    if not finite:
                        errors.append(f'Array {name} contains non-finite measurements')
            metadata[name] = info
        return {'array_names': names, 'arrays': metadata}
    finally:
        if path.suffix.lower() == '.npz':
            data.close()


def image_checks(path: Path, check: dict[str, Any], errors: list[str]) -> dict[str, Any]:
    from PIL import Image
    with Image.open(path) as image:
        observed = {'format': image.format, 'width': image.width, 'height': image.height}
        image.verify()
    expected = check.get('image_format', 'PNG' if path.suffix.lower() == '.png' else None)
    if expected and observed['format'] != expected.upper():
        errors.append(f'Image format is {observed["format"]}; requires {expected.upper()}')
    return {'image': observed}
