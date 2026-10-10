"""config.py 테스트 — 모델 선택 기록 로드·검증, 모델을 고정하지 않는지 확인."""

import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import config as cfg

ROOT = Path(__file__).resolve().parent.parent


def _write(tmp_path, data):
    p = tmp_path / "harness.config.json"
    p.write_text(json.dumps(data))
    return p


@pytest.fixture
def valid_config():
    return {
        "selection": {"source": "https://artificialanalysis.ai/models", "selected_at": "2026-10-10", "rationale": ""},
        "roles": {
            "worker": {"cli": "claude", "model": "claude-sonnet-5-5", "effort": "max"},
            "reviewers": [
                {"name": "claude", "cli": "claude", "model": "claude-opus-5-5", "effort": "max"},
                {"name": "codex", "cli": "codex", "model": "", "effort": "max"},
            ],
        },
        "parallel": {"max_worktrees": 3},
        "timeouts_sec": {"step": 3600, "review": 1800},
    }


class TestLoadConfig:
    def test_loads_valid_file(self, tmp_path, valid_config):
        loaded = cfg.load_config(_write(tmp_path, valid_config))
        assert loaded["roles"]["worker"]["model"] == "claude-sonnet-5-5"

    def test_missing_role_raises(self, tmp_path, valid_config):
        del valid_config["roles"]["worker"]
        with pytest.raises(cfg.ConfigError, match="worker"):
            cfg.load_config(_write(tmp_path, valid_config))

    def test_empty_reviewers_raises(self, tmp_path, valid_config):
        valid_config["roles"]["reviewers"] = []
        with pytest.raises(cfg.ConfigError, match="reviewers"):
            cfg.load_config(_write(tmp_path, valid_config))

    def test_unknown_cli_raises(self, tmp_path, valid_config):
        valid_config["roles"]["reviewers"][1]["cli"] = "gemini"
        with pytest.raises(cfg.ConfigError, match="cli"):
            cfg.load_config(_write(tmp_path, valid_config))

    def test_duplicate_reviewer_name_raises(self, tmp_path, valid_config):
        valid_config["roles"]["reviewers"][1]["name"] = "claude"
        with pytest.raises(cfg.ConfigError, match="name"):
            cfg.load_config(_write(tmp_path, valid_config))

    def test_missing_timeout_raises(self, tmp_path, valid_config):
        del valid_config["timeouts_sec"]["step"]
        with pytest.raises(cfg.ConfigError, match="step"):
            cfg.load_config(_write(tmp_path, valid_config))

    def test_orchestrator_role_not_required(self, tmp_path, valid_config):
        assert "orchestrator" not in valid_config["roles"]
        cfg.load_config(_write(tmp_path, valid_config))

    def test_repo_config_is_valid(self):
        cfg.load_config()


class TestCliArgs:
    def test_claude_args(self):
        role = {"cli": "claude", "model": "claude-opus-5-5", "effort": "max"}
        assert cfg.claude_args(role) == ["--model", "claude-opus-5-5", "--effort", "max"]

    def test_claude_args_empty_model_omitted(self):
        role = {"cli": "claude", "model": "", "effort": "high"}
        assert cfg.claude_args(role) == ["--effort", "high"]

    def test_codex_args(self):
        role = {"cli": "codex", "model": "gpt-x", "effort": "max"}
        assert cfg.codex_args(role) == ["-m", "gpt-x", "-c", 'model_reasoning_effort="max"']

    def test_codex_args_empty_model_uses_codex_default(self):
        role = {"cli": "codex", "model": "", "effort": ""}
        assert cfg.codex_args(role) == []


class TestSelectionAge:
    def test_empty_selection_returns_none(self, valid_config):
        valid_config["selection"]["selected_at"] = ""
        assert cfg.selection_age_days(valid_config, date(2026, 10, 17)) is None

    def test_missing_selection_returns_none(self, valid_config):
        del valid_config["selection"]
        assert cfg.selection_age_days(valid_config, date(2026, 10, 17)) is None

    def test_returns_days_since_selection(self, valid_config):
        assert cfg.selection_age_days(valid_config, date(2026, 10, 17)) == 7


def _frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    block = text.split("---")[1]
    return dict(
        (k.strip(), v.strip())
        for k, v in (line.split(":", 1) for line in block.strip().splitlines() if ":" in line)
    )


class TestModelsNotPinned:
    """모델은 오케스트레이터(사용자가 처음 명령한 세션)가 실행 시 artificialanalysis.ai를 보고 고른다."""

    def test_settings_does_not_pin_orchestrator_model(self):
        settings = json.loads((ROOT / ".claude" / "settings.json").read_text())
        assert "model" not in settings
        assert "effortLevel" not in settings

    @pytest.mark.parametrize("agent", ["implementer.md", "reviewer.md"])
    def test_agent_does_not_pin_model(self, agent):
        assert "model" not in _frontmatter(ROOT / ".claude" / "agents" / agent)
