"""QuantaAlpha custom workspace."""

import subprocess
from pathlib import Path

import yaml
from rdagent.scenarios.qlib.experiment.workspace import QlibFBWorkspace as _RdagentQlibFBWorkspace
from rdagent.log import rdagent_logger as logger
from quantaalpha.utils.qlib_provider import select_provider_for_config

_CUSTOM_TEMPLATE_DIR = Path(__file__).resolve().parent / "factor_template"


def _patch_qlib_config_provider(config_path: Path) -> None:
    if not config_path.exists():
        return
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    provider = select_provider_for_config(config, config_path.name)
    qlib_init = config.setdefault("qlib_init", {})
    old_provider = qlib_init.get("provider_uri")
    qlib_init["provider_uri"] = str(provider)
    config_path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8")

    if str(old_provider) != str(provider):
        logger.warning(
            f"Using Qlib provider for {config_path.name}: {provider} "
            f"(configured provider was {old_provider})"
        )


class QlibFBWorkspace(_RdagentQlibFBWorkspace):
    """
    Override rdagent QlibFBWorkspace: inject project factor_template/ YAML over defaults;
    init empty git repo in workspace to avoid qlib recorder git help output.
    """

    def __init__(self, template_folder_path: Path, *args, **kwargs) -> None:
        super().__init__(template_folder_path, *args, **kwargs)
        if _CUSTOM_TEMPLATE_DIR.exists():
            self.inject_code_from_folder(_CUSTOM_TEMPLATE_DIR)
            logger.info(f"Overrode rdagent default config with project template: {_CUSTOM_TEMPLATE_DIR}")

    def before_execute(self) -> None:
        """Init empty git repo in workspace to suppress qlib recorder git warnings."""
        super().before_execute()
        for config_name in ("conf_baseline.yaml", "conf_combined_factors.yaml"):
            _patch_qlib_config_provider(self.workspace_path / config_name)

        git_dir = self.workspace_path / ".git"
        if not git_dir.exists():
            try:
                subprocess.run(
                    ["git", "init"],
                    cwd=str(self.workspace_path),
                    capture_output=True,
                    timeout=5,
                )
            except Exception:
                pass
