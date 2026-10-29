import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from quantaalpha.backtest.factor_loader import FactorLoader
from quantaalpha.validation.paper_pool import (
    _build_validation_label,
    build_paper_factor_pool,
)


class PaperPoolRunTest(unittest.TestCase):
    def test_builds_immutable_selected_library_from_validation_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dates = pd.date_range("2021-01-04", periods=12, freq="B")
            instruments = [f"S{index:02d}" for index in range(6)]
            index = pd.MultiIndex.from_product(
                [dates, instruments],
                names=["datetime", "instrument"],
            )
            time = np.repeat(np.arange(len(dates), dtype=float), len(instruments))
            stock = np.tile(np.arange(len(instruments), dtype=float), len(dates))
            panel = pd.DataFrame(
                {"$close": 100.0 + time + stock * (time + 1.0) ** 2 / 20.0},
                index=index,
            )
            factor_data = root / "daily_pv.h5"
            panel.to_hdf(factor_data, key="data")
            label = _build_validation_label(
                factor_data,
                "2021-01-04",
                "2021-01-15",
            )

            factor_paths = {}
            for factor_id, multiplier in (("alpha", 1.0), ("duplicate", 2.0)):
                path = root / f"{factor_id}.h5"
                (label * multiplier).rename(factor_id).to_hdf(path, key="data")
                factor_paths[factor_id] = path

            library = {
                "metadata": {},
                "factors": {
                    factor_id: {
                        "factor_id": factor_id,
                        "factor_name": factor_id,
                        "factor_expression": "$close",
                        "cache_location": {
                            "result_h5_path": str(path),
                        },
                        "quality_gate": {
                            "evaluated": True,
                            "passed": True,
                            "consistency_passed": True,
                        },
                    }
                    for factor_id, path in factor_paths.items()
                },
            }
            library_path = root / "library.json"
            library_path.write_text(json.dumps(library), encoding="utf-8")
            config = {
                "protocol": {"id": "test-paper"},
                "paper_protocol": {
                    "label": "Ref($close, -2) / Ref($close, -1) - 1",
                    "effective_segments": {
                        "valid": ["2021-01-04", "2021-01-15"]
                    },
                },
                "replication_assumptions": {
                    "factor_pool": {
                        "correlation_method": "mean_daily_cross_sectional_spearman",
                        "max_abs_correlation": 0.7,
                        "max_factors": 150,
                        "max_fraction": 1.0,
                        "min_cross_section_size": 5,
                        "min_valid_days": 5,
                    }
                },
                "runtime": {
                    "factor_data": {"path": str(factor_data)}
                },
            }
            config_path = root / "protocol.yaml"
            config_path.write_text(
                yaml.safe_dump(config),
                encoding="utf-8",
            )
            output = root / "pool"

            result = build_paper_factor_pool(
                config_path,
                library_path,
                output,
            )

            self.assertEqual(result["selection"]["selectedCount"], 1)
            self.assertEqual(
                result["selection"]["selectedFactors"],
                ["alpha"],
            )
            selected = json.loads(
                (output / "selected_factor_library.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(set(selected["factors"]), {"alpha"})
            self.assertTrue((output / "manifest.json").is_file())
            loader = FactorLoader(
                {
                    "factor_source": {
                        "type": "custom",
                        "custom": {
                            "json_files": [
                                str(output / "selected_factor_library.json")
                            ],
                            "decision_filter": "all",
                            "quality_filter": None,
                        },
                    }
                }
            )
            qlib_factors, custom_factors = loader.load_factors()
            self.assertEqual(qlib_factors, {})
            self.assertEqual(
                {factor["factor_name"] for factor in custom_factors},
                {"alpha"},
            )
            with self.assertRaises(FileExistsError):
                build_paper_factor_pool(
                    config_path,
                    library_path,
                    output,
                )


if __name__ == "__main__":
    unittest.main()
