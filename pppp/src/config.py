"""Settings of the request pipeline, read from config/config.yaml."""

import dataclasses
import pathlib

import yaml

# This file is at <repository>/pppp/src/config.py.
REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPOSITORY_ROOT / "config" / "config.yaml"


@dataclasses.dataclass(frozen=True)
class PipelineSettings:
    """Where the pipeline finds its models, and how many times it retries a translation.

    gate_a_model_directory: directory of the trained Gate A classifier;
    generator_config_path: the generator's config.json;
    max_retries: translations retried after the first one.
    """

    gate_a_model_directory: pathlib.Path
    generator_config_path: pathlib.Path
    max_retries: int


def resolve_path(path_text):
    """Return `path_text` as an absolute path; a relative path is taken from the repository root."""
    path = pathlib.Path(path_text)
    if path.is_absolute():
        return path
    return REPOSITORY_ROOT / path


def load_settings(config_path=DEFAULT_CONFIG_PATH):
    """Read the YAML file at `config_path` and return its PipelineSettings.

    Raises KeyError when a setting is missing from the file.
    """
    config_text = pathlib.Path(config_path).read_text(encoding="utf-8")
    settings_by_section = yaml.safe_load(config_text)
    return PipelineSettings(
        gate_a_model_directory=resolve_path(settings_by_section["gate_a"]["model_directory"]),
        generator_config_path=resolve_path(settings_by_section["generator"]["config"]),
        max_retries=settings_by_section["max_retries"],
    )
