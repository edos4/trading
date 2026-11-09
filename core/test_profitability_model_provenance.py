"""Missing or replaced model provenance cannot certify a holdout."""
from __future__ import annotations

import hashlib
import json
import pytest
from core.kronos_provenance import checkpoint_provenance


def manifest(root):
    components={}
    for name in ['predictor','tokenizer']:
        directory=root/name/'best_model'; directory.mkdir(parents=True)
        (directory/'weights.bin').write_bytes(name.encode())
        components[name]={'weights.bin':hashlib.sha256(name.encode()).hexdigest()}
    value={'schema_version':1,'eligible_from':'2026-01-01','selection_data_end':'2025-12-31',
           'corpus_sha256':{'dataset':'0'*64},'components':components}
    (root/'selection_manifest.json').write_text(json.dumps(value))


def test_missing_metadata_is_explicit_and_strict_mode_refuses_it(tmp_path):
    assert not checkpoint_provenance(tmp_path,'2026-01-01')['out_of_sample_eligible']
    with pytest.raises(ValueError,match='manifest'):
        checkpoint_provenance(tmp_path,'2026-01-01',required=True)


def test_verified_identity_allows_pre_eligibility_dates_and_rejects_replaced_weights(tmp_path):
    manifest(tmp_path)
    early = checkpoint_provenance(tmp_path,'2025-11-03')
    assert early['identity_verified'] and not early['out_of_sample_eligible']
    later = checkpoint_provenance(tmp_path,'2026-01-01')
    assert later['identity_verified'] and later['out_of_sample_eligible']
    (tmp_path/'predictor/best_model/weights.bin').write_bytes(b'replacement')
    with pytest.raises(ValueError,match='identity differs'):
        checkpoint_provenance(tmp_path,'2026-01-01')
