"""Board registry. Adapters are selected by the `adapter` key in data/boards.yaml."""

from __future__ import annotations

from . import ats, builtin, climatebase, consider, edgar, fundingpress, generalist, getro, landingjobs, portfolio, substack, teamworkonline, waas
from .base import BoardResult

ADAPTERS = {
    "getro": getro.fetch,
    "ats": ats.fetch,
    "portfolio": portfolio.fetch,
    "waas": waas.fetch,
    "generalist": generalist.fetch,
    "edgar": edgar.fetch,
    "landingjobs": landingjobs.fetch,
    "climatebase": climatebase.fetch,
    "consider": consider.fetch,
    "builtin": builtin.fetch,
    "teamworkonline": teamworkonline.fetch,
    "substack": substack.fetch,
    "fundingpress": fundingpress.fetch,
}


def run_board(name: str, cfg: dict) -> BoardResult:
    """Dispatch one board. Unknown or login-walled boards report, never raise."""
    tier = cfg.get("tier", "")
    if tier == "skip":
        return BoardResult(
            board=name,
            blocked=True,
            error=cfg.get("reason", "login-walled — handle in browser"),
        )
    fn = ADAPTERS.get(cfg.get("adapter", ""))
    if not fn:
        return BoardResult(board=name, error=f"no adapter '{cfg.get('adapter')}'")
    return fn(name, cfg)
