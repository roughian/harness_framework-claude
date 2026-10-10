"""
harness.config.json 로더 — worker/reviewers의 모델 선택 기록·effort와 timeout.

모델은 고정하지 않는다. 오케스트레이터(사용자가 처음 명령한 세션)가 실행 전에
artificialanalysis.ai/models를 보고 골라 기록한다. 비어 있으면 CLI 기본 모델을 쓴다.
"""

import json
from datetime import date
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "harness.config.json"

SINGLE_ROLES = ("worker",)
SUPPORTED_CLIS = ("claude", "codex")
ROLE_KEYS = ("cli", "model", "effort")
TIMEOUT_KEYS = ("step", "review")


class ConfigError(Exception):
    """harness.config.json이 없거나 형식이 잘못됨."""


def _check_role(label: str, role: dict):
    missing = [k for k in ROLE_KEYS if k not in role]
    if missing:
        raise ConfigError(f"{label}: 필수 키 누락 {missing}")
    if role["cli"] not in SUPPORTED_CLIS:
        raise ConfigError(f"{label}: 지원하지 않는 cli '{role['cli']}' (허용: {SUPPORTED_CLIS})")


def _validate(data: dict):
    roles = data.get("roles", {})
    for name in SINGLE_ROLES:
        if name not in roles:
            raise ConfigError(f"roles.{name} 누락")
        _check_role(f"roles.{name}", roles[name])

    reviewers = roles.get("reviewers") or []
    if not reviewers:
        raise ConfigError("roles.reviewers가 비어 있음")
    names = [r.get("name") for r in reviewers]
    if None in names or len(set(names)) != len(names):
        raise ConfigError(f"roles.reviewers의 name은 필수이며 중복 불가: {names}")
    for r in reviewers:
        _check_role(f"roles.reviewers[{r['name']}]", r)

    timeouts = data.get("timeouts_sec", {})
    for key in TIMEOUT_KEYS:
        if not isinstance(timeouts.get(key), int):
            raise ConfigError(f"timeouts_sec.{key} 누락 또는 정수 아님")


def load_config(path: Optional[Path] = None) -> dict:
    p = Path(path) if path else CONFIG_FILE
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError as err:
        raise ConfigError(f"{p} 없음") from err
    except json.JSONDecodeError as err:
        raise ConfigError(f"{p} JSON 파싱 실패: {err}") from err
    _validate(data)
    return data


def selection_age_days(config: dict, today: date) -> Optional[int]:
    """마지막 모델 선택 후 지난 일수. 선택 기록이 없으면 None."""
    selected_at = config.get("selection", {}).get("selected_at")
    if not selected_at:
        return None
    return (today - date.fromisoformat(selected_at)).days


def claude_args(role: dict) -> list:
    args = []
    if role.get("model"):
        args += ["--model", role["model"]]
    if role.get("effort"):
        args += ["--effort", role["effort"]]
    return args


def codex_args(role: dict) -> list:
    """model이 비어 있으면 ~/.codex/config.toml 기본 모델을 쓴다."""
    args = []
    if role.get("model"):
        args += ["-m", role["model"]]
    if role.get("effort"):
        args += ["-c", f'model_reasoning_effort="{role["effort"]}"']
    return args
