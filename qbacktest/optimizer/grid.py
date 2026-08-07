"""Grid + random search over a parameter space."""

from __future__ import annotations

import itertools
import random
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import pandas as pd


@dataclass
class ParamSpace:
    """Cartesian product over named parameters.

    Each value can be a list (discrete) or a tuple (continuous range, used in
    random sampling).
    """
    grid: dict[str, list[Any]] = field(default_factory=dict)

    def add(self, name: str, values: list[Any]) -> ParamSpace:
        self.grid[name] = list(values)
        return self

    def __iter__(self):
        keys = list(self.grid.keys())
        for combo in itertools.product(*[self.grid[k] for k in keys]):
            yield dict(zip(keys, combo))

    def sample(self, n: int, seed: int | None = None) -> list[dict[str, Any]]:
        rng = random.Random(seed)
        keys = list(self.grid.keys())
        out: list[dict[str, Any]] = []
        for _ in range(n):
            row = {k: rng.choice(self.grid[k]) for k in keys}
            out.append(row)
        return out

    def __len__(self) -> int:
        n = 1
        for v in self.grid.values():
            n *= len(v)
        return n


def grid_search(
    run_fn: Callable[[dict[str, Any]], dict[str, float]],
    space: ParamSpace,
    *,
    parallel: int = 1,
    progress: bool = True,
) -> pd.DataFrame:
    """Exhaustive search over `space`.

    `run_fn(params)` runs a backtest with those params and returns a metrics dict.
    Returns a DataFrame with one row per parameter combination, sorted by Sharpe desc.
    """
    combos = list(space)
    rows: list[dict[str, Any]] = []
    if parallel <= 1:
        for i, p in enumerate(combos):
            metrics = run_fn(p)
            rows.append({**p, **metrics})
            if progress:
                print(f"[{i+1}/{len(combos)}] {p} → sharpe={metrics.get('sharpe', 0):.2f}")
    else:
        with ProcessPoolExecutor(max_workers=parallel) as ex:
            futures = {ex.submit(run_fn, p): p for p in combos}
            for i, fut in enumerate(as_completed(futures)):
                p = futures[fut]
                metrics = fut.result()
                rows.append({**p, **metrics})
                if progress:
                    print(f"[{i+1}/{len(combos)}] {p} → sharpe={metrics.get('sharpe', 0):.2f}")
    df = pd.DataFrame(rows)
    if "sharpe" in df.columns:
        df = df.sort_values("sharpe", ascending=False).reset_index(drop=True)
    return df
