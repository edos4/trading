"""Arrow: named, integrity-checked one- and three-session direction inference."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import uuid

import pandas as pd

from core.kronos_direction import DirectionModel, _implementation_hashes, predict_direction
from core.kronos_weekly import PHT, write_json
from core.market import last_closed_session_date, ph_history_symbol
from learn.kronos_weekly_dataset import expected_session, file_hash, is_session

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARROW = ROOT / 'runs' / 'arrow'
MODEL_NAME = 'Arrow'


def _load_bundle(directory: Path) -> tuple[dict, dict[int, DirectionModel], str]:
    directory = Path(directory).resolve(strict=True)
    digest = file_hash(directory / 'selection.json')
    if digest != (directory / 'selection.sha256').read_text().strip():
        raise ValueError('Arrow selection manifest hash differs')
    selection = json.loads((directory / 'selection.json').read_text())
    if selection['source_files'] != _implementation_hashes():
        raise ValueError('Arrow feature/training implementation differs from its locked selection')
    if 'flat_policy' in selection:
        from core.arrow_flat import source_hashes, validate_policy
        validate_policy(selection['flat_policy'])
        if selection.get('revision_source_files') != source_hashes():
            raise ValueError('Arrow Flat calibration implementation differs from its selection')
        if (file_hash(directory / 'flat-validation.json') != selection['calibration_report_sha256']
                or not date.fromisoformat(selection['history_verification_cutoff'])
                <= date.fromisoformat(selection['feedback_through'])
                < date.fromisoformat(selection['final_test_start'])
                < date.fromisoformat(selection['final_test_end'])):
            raise ValueError('Arrow Flat calibration provenance or forward dates differ')
    if set(selection['models']) != {'direction-1d.npz', 'direction-3d.npz'}:
        raise ValueError('Arrow requires both one- and three-session models')
    models = {}
    for horizon in (1, 3):
        path = directory / f'direction-{horizon}d.npz'
        if file_hash(path) != selection['models'][path.name]:
            raise ValueError(f'Arrow {horizon}-day checkpoint hash differs')
        model = DirectionModel.load(path)
        if (model.horizon != horizon or model.lookback != selection['lookback']
                or asdict(model.quality) != selection['quality']):
            raise ValueError('Arrow checkpoint configuration differs from its selection')
        models[horizon] = model
    if 'flat_policy' in selection:
        from core.arrow_flat import wrap_model
        models = {h: wrap_model(model, selection['flat_policy']) for h, model in models.items()}
    return selection, models, digest


def register_arrow(directory: Path, alias: Path = DEFAULT_ARROW) -> dict:
    """Name existing checkpoints without rewriting weights or locked selection."""
    directory = Path(directory).resolve(strict=True)
    selection, _, digest = _load_bundle(directory)
    alias = Path(alias).absolute()
    if alias != directory and alias.exists() and not alias.is_symlink():
        raise ValueError('Arrow alias cannot replace an existing directory or file')
    card_path = directory / 'arrow.json'
    if card_path.exists():
        card = json.loads(card_path.read_text())
        if (card.get('name') != MODEL_NAME or card.get('selection_sha256') != digest
                or card.get('version') != digest[:12]):
            raise ValueError('Existing Arrow name card belongs to another selection')
    else:
        card = {'name': MODEL_NAME, 'version': digest[:12], 'selection_sha256': digest,
                'named_at': datetime.now(PHT).isoformat(timespec='seconds'),
                'models': {str(h): {'name': f'Arrow {h}D', 'checkpoint': f'direction-{h}d.npz',
                                    'sha256': selection['models'][f'direction-{h}d.npz']} for h in (1, 3)}}
        write_json(card_path, card)
    if alias == directory:
        return card
    alias.parent.mkdir(parents=True, exist_ok=True)
    temporary = alias.with_name(f'.{alias.name}-{uuid.uuid4().hex}.tmp')
    try:
        temporary.symlink_to(directory, target_is_directory=True)
        temporary.replace(alias)
    finally:
        temporary.unlink(missing_ok=True)
    return card


@dataclass
class Arrow:
    directory: Path
    selection: dict
    models: dict[int, DirectionModel]
    card: dict

    @classmethod
    def load(cls, directory: Path = DEFAULT_ARROW) -> 'Arrow':
        directory = Path(directory).resolve(strict=True)
        selection, models, digest = _load_bundle(directory)
        card = json.loads((directory / 'arrow.json').read_text())
        if (card.get('name') != MODEL_NAME or card.get('selection_sha256') != digest
                or card.get('version') != digest[:12]):
            raise ValueError('Arrow name card differs from the locked models')
        return cls(directory, selection, models, card)

    def info(self) -> dict:
        final_path = self.directory / 'final-evaluation.json'
        status = 'final_test_pending'
        if final_path.exists():
            final = json.loads(final_path.read_text())
            if final['selection_sha256'] != self.card['selection_sha256']:
                raise ValueError('Arrow final report belongs to another selection')
            status = 'final_test_passed' if final['passed_gates'] else 'final_test_failed'
        result = {'name': MODEL_NAME, 'version': self.card['version'], 'directory': str(self.directory),
                'horizons_sessions': [1, 3], 'lookback_bars': self.selection['lookback'],
                'excluded_symbols': self.selection['excluded_symbols'], 'validation_status': status,
                'reserved_final_period': [self.selection['final_test_start'], self.selection['final_test_end']],
                'automated_trading_enabled': False}
        if 'flat_policy' in self.selection:
            result.update(decision_policy=self.selection['flat_policy']['method'],
                          parent_version=self.selection['parent_selection_sha256'][:12],
                          feedback_through=self.selection['feedback_through'])
        return result

    def predict(self, frame: pd.DataFrame, symbol: str, market: str, horizon: int = 1,
                *, as_of: date | None = None) -> dict:
        if market not in ('us', 'ph') or horizon not in self.models:
            raise ValueError('Use market us/ph and horizon 1/3')
        canonical = ph_history_symbol(symbol) if market == 'ph' else symbol.strip().upper()
        if not canonical or canonical in self.selection['excluded_symbols']:
            raise ValueError('Symbol is empty or excluded from Arrow')
        if not isinstance(frame.index, pd.DatetimeIndex) or frame.index.isna().any():
            raise ValueError('Arrow requires dated OHLCV bars')
        closed = last_closed_session_date(market)
        cutoff = as_of or closed
        if cutoff > closed:
            raise ValueError(f'Requested date is beyond the latest completed {market} session ({closed})')
        work = frame.loc[[stamp.date() <= cutoff for stamp in frame.index]].sort_index(kind='stable')
        if work.empty:
            raise ValueError('No completed history is available for this date')
        anchor = work.index[-1].date()
        expected = cutoff if is_session(cutoff, market) else expected_session(cutoff + timedelta(days=1), market)
        if anchor != expected:
            raise ValueError(f'History is stale: latest bar is {anchor}, expected {expected}')
        first_target = anchor + timedelta(days=1)
        while not is_session(first_target, market):
            first_target += timedelta(days=1)
        if first_target < date.fromisoformat(self.selection['final_test_start']):
            raise ValueError('Requested forecast overlaps Arrow training/selection history')
        result = predict_direction(self.models[horizon], work, market)
        return {'model': MODEL_NAME, 'version': self.card['version'], 'symbol': canonical,
                'market': market, 'as_of': anchor.isoformat(), 'horizon_sessions': horizon,
                'validation_status': self.info()['validation_status'], **result}
