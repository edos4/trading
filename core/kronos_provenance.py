"""Verify checkpoint identity and declared model-selection information bounds."""
from __future__ import annotations

from datetime import date
import hashlib
import json
from pathlib import Path


def checkpoint_provenance(root: Path, as_of: str | None, *, required: bool = False) -> dict:
    manifest=root/'selection_manifest.json'
    missing={'identity_verified':False,'out_of_sample_eligible':False,
             'reason':'No checkpoint-linked selection manifest; historical holdout status unknown'}
    if not manifest.exists():
        if required:
            raise ValueError(missing['reason'])
        return missing
    raw=manifest.read_bytes(); value=json.loads(raw)
    if value.get('schema_version')!=1 or not value.get('corpus_sha256'):
        raise ValueError('Invalid checkpoint selection manifest')
    eligible=date.fromisoformat(value['eligible_from'])
    selection_end=date.fromisoformat(value['selection_data_end'])
    if eligible<=selection_end:
        raise ValueError('Checkpoint eligibility must follow all selection data')
    components=value.get('components',{})
    if set(components)!={'predictor','tokenizer'}:
        raise ValueError('Selection manifest must bind predictor and tokenizer')
    for component,files in components.items():
        directory=root/component/'best_model'
        actual={str(p.relative_to(directory)) for p in directory.rglob('*') if p.is_file()}
        if not files or set(files)!=actual:
            raise ValueError(f'Checkpoint manifest does not cover every {component} file')
        for name,expected in files.items():
            path=directory/name
            if not path.resolve().is_relative_to(directory.resolve()):
                raise ValueError('Invalid checkpoint manifest path')
            digest=hashlib.sha256()
            with path.open('rb') as stream:
                for chunk in iter(lambda:stream.read(1024*1024),b''):
                    digest.update(chunk)
            if digest.hexdigest()!=expected:
                raise ValueError(f'Checkpoint identity differs: {component}/{name}')
    if as_of and date.fromisoformat(as_of)<eligible:
        raise ValueError(f'Kronos model-selection overlap: {as_of} precedes eligible_from={eligible}')
    return {'identity_verified':True,'out_of_sample_eligible':bool(as_of),
            'eligible_from':eligible.isoformat(),'selection_data_end':selection_end.isoformat(),
            'corpus_sha256':value['corpus_sha256'],
            'manifest_sha256':hashlib.sha256(raw).hexdigest(),
            'scope':'file identity and operator-declared selection boundary; not independent training certification'}
