"""CLI helper: fetch live NSE chain → run all arb scanners → print manual-trade tickets.

Standalone module so the main CLI doesn't need to import httpx unconditionally.

Usage:
    qb scan-arb-nse                               # NIFTY nearest expiry
    qb scan-arb-nse --underlying BANKNIFTY
    qb scan-arb-nse --underlying NIFTY --top 10
"""

from __future__ import annotations

import sys
from datetime import date

from rich.console import Console
from rich.table import Table

from qbacktest.core.calendar import DEFAULT_CALENDAR, IST
from qbacktest.data.sources.nse_chain import NSEChainFetcher
from qbacktest.strategies.arbitrage import scan_all
from qbacktest.strategies.arbitrage.types import OpportunityKind


console = Console()


# Friendly names + manual-trade hints per opportunity kind
_KIND_HINTS = {
    OpportunityKind.BOX_SPREAD: (
        "Box spread (synthetic loan). 4 legs, locked at expiry. "
        "Held to expiry: profit = (K2 - K1) - net debit."
    ),
    OpportunityKind.PCP_CALL_RICH: (
        "Put-call parity: call is rich. SELL call + BUY put + BUY future. "
        "Held to expiry: profit = mispricing × lot, deterministic."
    ),
    OpportunityKind.PCP_PUT_RICH: (
        "Put-call parity: put is rich. BUY call + SELL put + SELL future. "
        "Held to expiry: profit = mispricing × lot, deterministic."
    ),
    OpportunityKind.CONVERSION: (
        "Conversion: synthetic stock cheap vs future. BUY synthetic + SELL future."
    ),
    OpportunityKind.REVERSAL: (
        "Reversal: synthetic stock rich vs future. SELL synthetic + BUY future."
    ),
    OpportunityKind.ADJACENT_STRIKE_CALL: (
        "Adjacent-strike call inversion. BUY lower strike + SELL higher strike. "
        "Net credit upfront; payoff ≥ 0 at expiry. Free money — but disappears in seconds."
    ),
    OpportunityKind.ADJACENT_STRIKE_PUT: (
        "Adjacent-strike put inversion. BUY higher strike + SELL lower strike. "
        "Net credit upfront; payoff ≥ 0 at expiry."
    ),
    OpportunityKind.FAR_OTM_PREMIUM: (
        "Far-OTM credit spread (HEDGED). Real tail risk — size carefully."
    ),
}


def run(underlying: str, top: int, include_far_otm: bool, min_edge: float, fetch_only: bool) -> int:
    console.print(f"[bold cyan]Fetching live {underlying} option chain from NSE...[/bold cyan]")
    try:
        snap = NSEChainFetcher().fetch(underlying)
    except Exception as e:
        console.print(f"[red]Fetch failed:[/red] {e}")
        console.print(
            "[dim]Common causes: NSE blocks non-Indian IPs, blocks during off-market warm-ups, "
            "or rate-limits scrapers. Retry once; if it persists, run during NSE market hours "
            "(09:15–15:30 IST).[/dim]"
        )
        return 2

    is_market_open = DEFAULT_CALENDAR.is_in_session(snap.ts)
    market_state = "[green]MARKET OPEN[/green]" if is_market_open else "[yellow]MARKET CLOSED[/yellow]"

    console.print()
    console.rule(f"[bold]{snap.underlying}  spot={snap.spot:,.2f}  expiry={snap.expiry}  {market_state}[/bold]")
    console.print(f"[dim]Snapshot at {snap.ts.strftime('%Y-%m-%d %H:%M:%S IST')}  |  "
                  f"{len(snap.quotes)} quotes  |  lot size {snap.lot_size}  |  "
                  f"r={snap.risk_free_rate:.2%}[/dim]")
    console.print()

    if fetch_only:
        # Print a quick chain snapshot summary and stop
        _print_chain_summary(snap)
        return 0

    opps = scan_all(snap, include_far_otm=include_far_otm)
    opps = [o for o in opps if o.edge_after_costs_inr >= min_edge]
    if not opps:
        console.print(f"[yellow]No arbitrage opportunities at this snapshot[/yellow] "
                      f"(threshold ₹{min_edge:.0f} net per lot).")
        if not is_market_open:
            console.print("[dim]Market is closed → bid/ask are stale. Retry at 09:20 IST tomorrow.[/dim]")
        return 0

    console.print(f"[bold green]{len(opps)} arbitrage opportunities found[/bold green]"
                  f" (threshold ₹{min_edge:.0f} net per lot)")
    console.print()

    # Top-N table
    table = Table(title=f"Top {min(top, len(opps))} opportunities", show_lines=False)
    table.add_column("#", justify="right")
    table.add_column("Kind")
    table.add_column("Edge / lot", justify="right")
    table.add_column("After cost", justify="right")
    table.add_column("Capital", justify="right")
    table.add_column("Notes")
    for i, o in enumerate(opps[:top], start=1):
        table.add_row(
            str(i),
            o.kind.value,
            f"₹{o.edge_per_lot_inr:>6,.0f}",
            f"₹{o.edge_after_costs_inr:>6,.0f}",
            f"₹{o.capital_required_inr:>7,.0f}",
            o.notes[:64] + ("…" if len(o.notes) > 64 else ""),
        )
    console.print(table)

    # Detailed manual-trade tickets for the top 3
    console.print()
    console.rule("[bold]Manual trade tickets (top 3)[/bold]")
    for i, o in enumerate(opps[:3], start=1):
        _print_manual_ticket(i, o, is_market_open=is_market_open)

    # Summary
    console.print()
    if is_market_open:
        console.print("[bold green]Tradeable now.[/bold green] Place these orders manually in your broker terminal.")
    else:
        console.print("[bold yellow]Market closed — these are stale prices.[/bold yellow] "
                      "These mispricings will mostly NOT survive market open. Re-run during market hours.")
    console.print("[dim]Disclaimer: arbitrage ≠ riskless when execution slippage exceeds estimate. "
                  "Place limit orders simultaneously; cancel if any leg doesn't fill within 30 seconds.[/dim]")
    return 0


def _print_chain_summary(snap):
    table = Table(title=f"{snap.underlying} chain — ATM ±5 strikes")
    table.add_column("Strike", justify="right")
    table.add_column("CE bid", justify="right")
    table.add_column("CE ask", justify="right")
    table.add_column("CE OI", justify="right")
    table.add_column("PE bid", justify="right")
    table.add_column("PE ask", justify="right")
    table.add_column("PE OI", justify="right")
    atm = snap.atm_strike(step=50 if snap.underlying == "NIFTY" else 100)
    strikes = sorted(snap.strikes)
    nearby = [s for s in strikes if abs(s - atm) <= 250]
    for k in nearby:
        c = snap.call(k); p = snap.put(k)
        cb = f"{c.bid:.2f}" if c and c.bid else "-"
        ca = f"{c.ask:.2f}" if c and c.ask else "-"
        co = f"{c.open_interest:,}" if c else "-"
        pb = f"{p.bid:.2f}" if p and p.bid else "-"
        pa = f"{p.ask:.2f}" if p and p.ask else "-"
        po = f"{p.open_interest:,}" if p else "-"
        marker = " [ATM]" if k == atm else ""
        table.add_row(f"{int(k)}{marker}", cb, ca, co, pb, pa, po)
    console.print(table)


def _print_manual_ticket(idx, opp, *, is_market_open: bool) -> None:
    color = "green" if opp.edge_after_costs_inr > 0 else "yellow"
    console.print(f"\n[bold {color}]#{idx} {opp.kind.value}[/bold {color}]  "
                  f"edge ₹{opp.edge_per_lot_inr:,.0f}/lot  net ₹{opp.edge_after_costs_inr:,.0f}/lot  "
                  f"capital ₹{opp.capital_required_inr:,.0f}")
    hint = _KIND_HINTS.get(opp.kind, "")
    if hint:
        console.print(f"   [dim]{hint}[/dim]")
    console.print(f"   {opp.notes}")
    console.print(f"   [bold]Legs:[/bold]")
    for leg in opp.legs:
        sign = "[red]-[/red]" if leg.side.value == "SELL" else "[green]+[/green]"
        console.print(f"     {sign} {leg.side.value:<4} {leg.quantity:>4} × {leg.instrument_id}  @ ₹{leg.price:.2f}   "
                      f"[dim]({leg.leg_type})[/dim]")
    if not is_market_open:
        console.print("   [yellow]⚠ Market closed — these prices are stale.[/yellow]")
    console.print(f"   [dim]Max risk on slippage estimate: ₹{opp.max_risk_inr:,.0f}/lot[/dim]")


# Click entry point — registered from the main CLI
def register(cli_group):
    import click

    @cli_group.command(name="scan-arb-nse",
                       help="Fetch live NIFTY/BANKNIFTY/FINNIFTY/MIDCPNIFTY chain from NSE and scan for arb.")
    @click.option("--underlying", default="NIFTY",
                  type=click.Choice(["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"]))
    @click.option("--top", default=5, type=int, help="Show top N opportunities")
    @click.option("--include-far-otm", is_flag=True, help="Include far-OTM credit spreads (real tail risk)")
    @click.option("--min-edge", default=50.0, type=float, help="Minimum net edge per lot (INR) to display")
    @click.option("--fetch-only", is_flag=True, help="Print chain summary only, skip scanners")
    def scan_arb_nse(underlying, top, include_far_otm, min_edge, fetch_only):
        sys.exit(run(underlying, top, include_far_otm, min_edge, fetch_only))
    return scan_arb_nse
