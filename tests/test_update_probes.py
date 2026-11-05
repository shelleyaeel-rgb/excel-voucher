from pathlib import Path

import pytest

import main
from xiaoyu import config


def test_probe_redirects_data_and_restores_it_after_failure(tmp_path, monkeypatch):
    isolated = tmp_path / 'isolated'
    monkeypatch.setenv('XIAOYU_UPDATE_TEST_DATA', str(isolated))
    monkeypatch.setattr(config, '_TEST_DATA_DIR', None)

    def probe(report):
        assert config.data_dir() == isolated
        (config.data_dir() / 'probe-only.json').write_text('{}')
        raise RuntimeError('probe failed')

    with pytest.raises(RuntimeError, match='probe failed'):
        main._isolated_probe(probe, str(tmp_path / 'report.json'))
    assert config._TEST_DATA_DIR is None
    assert (isolated / 'probe-only.json').exists()


def test_default_probe_uses_disposable_data(tmp_path, monkeypatch):
    monkeypatch.delenv('XIAOYU_UPDATE_TEST_DATA', raising=False)
    seen = []

    def probe(report):
        seen.append(config.data_dir())
        assert config.data_dir().is_dir()
        return 0

    assert main._isolated_probe(probe, str(tmp_path / 'report.json')) == 0
    assert not seen[0].exists()
    assert config._TEST_DATA_DIR is None
