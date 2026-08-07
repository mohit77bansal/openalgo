"""Command-line interface for qbacktest.

Usage:
    qb seed --year 2024              Ingest NSE bhavcopy for given year
    qb backtest --strategy nifty_straddle --year 2024
    qb auth upstox                   Print OAuth URL + complete flow
    qb data availability NIFTY       Show local data availability
    qb optimize --strategy ...       Run walk-forward optimization
    qb api                           Start the FastAPI gateway
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.table import Table

from qbacktest import __version__

console = Console()
log = logging.getLogger(__name__)


@click.group(help=f"qbacktest v{__version__} — Indian markets backtesting CLI")
@click.option("--data-dir", default="./data", type=click.Path(), help="Path to data root")
@click.pass_context
def cli(ctx: click.Context, data_dir: str) -> None:
    ctx.ensure_object(dict)
    ctx.obj["data_dir"] = Path(data_dir).resolve()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


# ---------------------------------------------------------------------------
# Seed (ingest data)
# ---------------------------------------------------------------------------


@cli.command(help="Ingest NSE bhavcopy for a year (or date range).")
@click.option("--year", type=int, help="Calendar year — ingests Jan 1 to Dec 31")
@click.option("--start", type=click.DateTime(["%Y-%m-%d"]))
@click.option("--end", type=click.DateTime(["%Y-%m-%d"]))
@click.option("--segment", type=click.Choice(["EQ", "FO", "BOTH"]), default="BOTH")
@click.pass_context
def seed(
    ctx: click.Context,
    year: Optional[int],
    start: Optional[click.DateTime],
    end: Optional[click.DateTime],
    segment: str,
) -> None:
    from qbacktest.data.store import DataStore
    from qbacktest.data.sources.bhavcopy import BhavcopySource

    if year:
        start_d = date(year, 1, 1)
        end_d = date(year, 12, 31)
    elif start and end:
        start_d = start.date() if hasattr(start, "date") else start
        end_d = end.date() if hasattr(end, "date") else end
    else:
        click.echo("Specify --year or --start/--end")
        sys.exit(1)

    segments = ("EQ", "FO") if segment == "BOTH" else (segment,)
    store = DataStore(root=ctx.obj["data_dir"])
    src = BhavcopySource(store)

    console.print(f"[bold]Seeding {start_d} → {end_d}[/bold] segments={segments}")
    counts = src.ingest_range(start_d, end_d, segments=segments)
    console.print(f"[green]Done[/green] EQ rows: {counts['EQ']}, FO rows: {counts['FO']}")


# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------


@cli.command(help="Run a reference strategy backtest.")
@click.option("--strategy", default="nifty_straddle",
              type=click.Choice(["nifty_straddle", "iron_condor", "intraday_breakout"]))
@click.option("--year", type=int, default=2024)
@click.option("--start", type=click.DateTime(["%Y-%m-%d"]))
@click.option("--end", type=click.DateTime(["%Y-%m-%d"]))
@click.option("--capital", default=1_000_000.0)
@click.option("--cost", default="zerodha", type=click.Choice(["zerodha", "upstox", "fyers", "zero"]))
@click.option("--underlying", default="NIFTY")
@click.pass_context
def backtest(
    ctx: click.Context,
    strategy: str,
    year: int,
    start: Optional[click.DateTime],
    end: Optional[click.DateTime],
    capital: float,
    cost: str,
    underlying: str,
) -> None:
    from qbacktest.core.types import Exchange, Instrument, InstrumentType
    from qbacktest.costs.costs import DEFAULT_COST_MODELS
    from qbacktest.data.store import DataStore
    from qbacktest.data.historical import HistoricalSource
    from qbacktest.engine.engine import BacktestEngine
    from qbacktest.strategies import (
        IntradayORBreakout,
        NiftyIronCondor,
        NiftyShortStraddle,
    )

    classes = {
        "nifty_straddle": NiftyShortStraddle,
        "iron_condor": NiftyIronCondor,
        "intraday_breakout": IntradayORBreakout,
    }
    cls = classes[strategy]

    if start and end:
        start_d = start.date() if hasattr(start, "date") else start
        end_d = end.date() if hasattr(end, "date") else end
    else:
        start_d = date(year, 1, 1)
        end_d = date(year, 12, 31)

    store = DataStore(root=ctx.obj["data_dir"])
    src = HistoricalSource(store)
    idx = Instrument(symbol=underlying.upper(), exchange=Exchange.NSE, instrument_type=InstrumentType.INDEX, lot_size=1)
    src.subscribe(idx, start=start_d, end=end_d, timeframe="1d")

    engine = BacktestEngine(
        strategy=cls(),
        data=src,
        starting_capital=capital,
        cost_model=DEFAULT_COST_MODELS[cost],
    )
    console.print(f"[bold]Running {cls.__name__}[/bold] from {start_d} to {end_d}")
    result = engine.run()
    console.print(result.summary())


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@cli.command(help="Authenticate with a broker. AngelOne is fully headless. Upstox supports --headless via Playwright + TOTP.")
@click.argument("broker", type=click.Choice(["angelone", "upstox", "fyers"]))
@click.option("--headless/--browser", default=False,
              help="(Upstox only) Use Playwright + TOTP to drive the OAuth flow without manual browser interaction.")
@click.option("--debug-screenshots", default=None, type=click.Path(),
              help="(Upstox headless only) Save step-by-step screenshots to this directory for debugging.")
@click.option("--show-browser", is_flag=True,
              help="(Upstox headless only) Run Chromium with a visible window for debugging.")
@click.pass_context
def auth(ctx: click.Context, broker: str, headless: bool, debug_screenshots: str | None, show_browser: bool) -> None:
    from qbacktest.gateway.settings import load_settings
    from qbacktest.brokers.token_store import TokenStore
    settings = load_settings()
    settings.app_data_dir.mkdir(parents=True, exist_ok=True)
    store = TokenStore(path=settings.app_data_dir / "broker_tokens.db", passphrase=settings.app_secret)
    if broker == "angelone":
        if not (settings.angelone_client_code and settings.angelone_api_key and settings.angelone_totp_secret):
            console.print("[red]AngelOne creds missing in .env[/red]")
            console.print("[dim]Set ANGELONE_CLIENT_CODE, ANGELONE_PASSWORD, ANGELONE_API_KEY, ANGELONE_TOTP_SECRET[/dim]")
            sys.exit(1)
        from qbacktest.brokers.angelone import AngelOneAdapter
        adapter = AngelOneAdapter(
            client_code=settings.angelone_client_code,
            password=settings.angelone_password,
            api_key=settings.angelone_api_key,
            totp_secret=settings.angelone_totp_secret,
            token_store=store,
        )
        console.print("[bold]Performing headless TOTP login (no browser)...[/bold]")
        data = asyncio.run(adapter.connect())
        console.print(f"[green]Authenticated[/green] as {data.get('clientcode', settings.angelone_client_code)}")
        return
    if broker == "upstox":
        if not settings.upstox_client_id:
            console.print("[red]UPSTOX_CLIENT_ID not set in env[/red]")
            sys.exit(1)
        from qbacktest.brokers.upstox import UpstoxAdapter
        adapter = UpstoxAdapter(
            client_id=settings.upstox_client_id,
            client_secret=settings.upstox_client_secret,
            redirect_uri=settings.upstox_redirect_uri,
            token_store=store,
        )
        if headless:
            from qbacktest.brokers.upstox_headless import UpstoxHeadlessAuth
            if not (settings.upstox_mobile and settings.upstox_pin and settings.upstox_totp_secret):
                console.print("[red]Headless Upstox needs UPSTOX_MOBILE, UPSTOX_PIN, UPSTOX_TOTP_SECRET in .env[/red]")
                console.print("[dim]TOTP-based 2FA must be enabled on the Upstox account (NOT SMS OTP).[/dim]")
                sys.exit(1)
            console.print("[bold]Headless Upstox login via Playwright + TOTP...[/bold]")
            console.print("[dim]Note: Playwright + Chromium must be installed. Run `make install-broker` if missing.[/dim]")
            helper = UpstoxHeadlessAuth(
                adapter=adapter,
                mobile=settings.upstox_mobile,
                pin=settings.upstox_pin,
                totp_secret=settings.upstox_totp_secret,
                headless=not show_browser,
                debug_screenshots_dir=debug_screenshots,
            )
            try:
                data = asyncio.run(helper.login())
                console.print(f"[green]Authenticated[/green]: {data.get('user_id', 'OK')}")
            except Exception as e:
                console.print(f"[red]Headless login failed[/red]: {e}")
                if debug_screenshots:
                    console.print(f"[dim]Screenshots in {debug_screenshots}/[/dim]")
                sys.exit(2)
            return
    else:
        if not settings.fyers_client_id:
            console.print("[red]FYERS_CLIENT_ID not set in env[/red]")
            sys.exit(1)
        from qbacktest.brokers.fyers import FyersAdapter
        adapter = FyersAdapter(
            client_id=settings.fyers_client_id,
            secret_key=settings.fyers_secret_key,
            redirect_uri=settings.fyers_redirect_uri,
            token_store=store,
        )
    console.print(f"\n[bold]Visit this URL in your browser[/bold]:\n")
    console.print(adapter.authorize_url())
    console.print("\nAfter login, the broker will redirect to your configured redirect URI.")
    console.print("Paste the `code` (Upstox) or `auth_code` (Fyers) from the URL here:")
    code = click.prompt("Code", type=str)
    data = asyncio.run(adapter.exchange_code_for_token(code.strip()))
    console.print(f"[green]Authenticated[/green]: {data.get('user_id', 'OK')}")


# ---------------------------------------------------------------------------
# Data availability
# ---------------------------------------------------------------------------


@cli.command(name="data", help="Data inspection commands")
def data() -> None:
    pass


@cli.command(help="Print availability for a symbol.")
@click.argument("symbol")
@click.option("--timeframe", default="1d")
@click.pass_context
def availability(ctx: click.Context, symbol: str, timeframe: str) -> None:
    from qbacktest.core.types import Exchange, Instrument, InstrumentType
    from qbacktest.data.availability import DataAvailability
    from qbacktest.data.store import DataStore
    store = DataStore(root=ctx.obj["data_dir"])
    avail = DataAvailability(store)
    inst = Instrument(symbol=symbol.upper(), exchange=Exchange.NSE, instrument_type=InstrumentType.INDEX, lot_size=1)
    rep = avail.report(inst, timeframe)
    table = Table(title=f"Availability: {inst.id} {timeframe}")
    table.add_column("Field"); table.add_column("Value")
    table.add_row("Earliest", rep.earliest.isoformat() if rep.earliest else "—")
    table.add_row("Latest", rep.latest.isoformat() if rep.latest else "—")
    table.add_row("Days", str(rep.n_days))
    console.print(table)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@cli.command(name="scan-arb", help="Scan a chain snapshot for arbitrage opportunities.")
@click.option("--snapshot", type=click.Path(exists=True, dir_okay=False), required=True,
              help="Path to a JSON-serialized ChainSnapshot")
@click.option("--include-far-otm", is_flag=True,
              help="Also scan for far-OTM premium-collection opportunities (REAL TAIL RISK)")
def scan_arb(snapshot: str, include_far_otm: bool) -> None:
    import json
    from datetime import date as _date, datetime as _dt
    from qbacktest.core.chain import ChainQuote, ChainSnapshot
    from qbacktest.core.types import OptionType
    from qbacktest.strategies.arbitrage import scan_all

    with open(snapshot) as f:
        data = json.load(f)
    quotes = tuple(
        ChainQuote(
            strike=float(q["strike"]),
            option_type=OptionType(q["option_type"]),
            last_price=float(q["last_price"]),
            bid=q.get("bid"),
            ask=q.get("ask"),
            volume=int(q.get("volume", 0)),
            open_interest=int(q.get("open_interest", 0)),
            iv=q.get("iv"),
        )
        for q in data["quotes"]
    )
    snap = ChainSnapshot(
        underlying=data["underlying"],
        expiry=_date.fromisoformat(data["expiry"]),
        spot=float(data["spot"]),
        ts=_dt.fromisoformat(data["ts"]),
        risk_free_rate=float(data.get("risk_free_rate", 0.07)),
        quotes=quotes,
        lot_size=int(data.get("lot_size", 75)),
        futures_price=data.get("futures_price"),
    )
    opps = scan_all(snap, include_far_otm=include_far_otm)

    if not opps:
        console.print("[yellow]No arbitrage opportunities found in this snapshot.[/yellow]")
        return

    table = Table(title=f"Arbitrage scan: {snap.underlying} {snap.expiry}")
    table.add_column("Kind"); table.add_column("Edge/lot", justify="right")
    table.add_column("After cost", justify="right"); table.add_column("Capital", justify="right")
    table.add_column("Notes")
    for o in opps[:20]:
        table.add_row(
            o.kind.value,
            f"₹{o.edge_per_lot_inr:,.0f}",
            f"₹{o.edge_after_costs_inr:,.0f}",
            f"₹{o.capital_required_inr:,.0f}",
            o.notes[:60] + ("…" if len(o.notes) > 60 else ""),
        )
    console.print(table)
    console.print(f"\n[bold]{len(opps)}[/bold] total opportunities. "
                  f"Top edge after costs: ₹{opps[0].edge_after_costs_inr:,.0f}")


@cli.command(name="scan-arb-live", help="Run the live arbitrage scanner with mock or live broker data.")
@click.option("--source", default="mock", type=click.Choice(["mock", "angelone", "upstox", "fyers"]),
              help="Where to pull the chain from. 'mock' synthesizes data offline; 'angelone' uses headless TOTP.")
@click.option("--underlying", default="NIFTY")
@click.option("--interval", default=1.0, type=float, help="Seconds between scans")
@click.option("--duration", default=30, type=int, help="Run for N seconds (0 = forever)")
@click.option("--include-far-otm", is_flag=True)
@click.option("--min-edge", default=0.0, type=float, help="Min net edge per lot (INR) to display")
@click.option("--top", default=5, type=int, help="Show top N opportunities each tick")
@click.option("--seed", default=None, type=int, help="Seed for mock RNG (reproducible runs)")
@click.option("--mispricing-prob", default=0.30, type=float,
              help="Probability of injecting a mispricing per tick (mock only)")
def scan_arb_live(source: str, underlying: str, interval: float, duration: int,
                  include_far_otm: bool, min_edge: float, top: int,
                  seed: int | None, mispricing_prob: float) -> None:
    import asyncio
    from datetime import datetime
    from qbacktest.core.calendar import IST
    from qbacktest.live import ChainScannerWorker
    from qbacktest.data.chain_source import MockChainProducer

    if source == "mock":
        producer = MockChainProducer(
            underlying=underlying,
            interval_seconds=interval,
            seed=seed,
            mispricing_prob=mispricing_prob,
        )
    elif source == "angelone":
        from datetime import date as _date
        from qbacktest.brokers.angelone import AngelOneAdapter
        from qbacktest.brokers.token_store import TokenStore
        from qbacktest.gateway.settings import load_settings
        from qbacktest.data.chain_builder import ChainBuilder
        from qbacktest.data.chain_source import LiveBrokerChainSource
        from qbacktest.core.types import Exchange, Instrument, InstrumentType
        from qbacktest.core.calendar import DEFAULT_CALENDAR
        s = load_settings()
        if not (s.angelone_client_code and s.angelone_api_key and s.angelone_totp_secret):
            console.print("[red]AngelOne creds missing in .env[/red]")
            sys.exit(1)
        store = TokenStore(s.app_data_dir / "broker_tokens.db", s.app_secret)
        adapter = AngelOneAdapter(
            client_code=s.angelone_client_code,
            password=s.angelone_password,
            api_key=s.angelone_api_key,
            totp_secret=s.angelone_totp_secret,
            token_store=store,
        )
        if not adapter.is_authenticated():
            console.print("[dim]Authenticating via TOTP...[/dim]")
            asyncio.run(adapter.connect())

        # Pick strikes ATM ± 10 around current spot. We need symbol master to map
        # strikes → angelone tokens. For brevity we ask the user to pre-warm the
        # master separately; full automation is a follow-up.
        console.print("[yellow]Note:[/yellow] live AngelOne chain ingest requires pre-fetched symbol master with broker tokens. "
                      "For first run, prefer --source mock to verify the pipeline; then re-run with --source angelone after "
                      "running `qb fetch-symbol-master --broker angelone`.")

        expiry = DEFAULT_CALENDAR.next_weekly_expiry(underlying.upper(), _date.today())
        if expiry is None:
            console.print(f"[red]No weekly expiry available for {underlying}[/red]")
            sys.exit(1)
        # Strikes — use a coarse ±10 step, will be filtered by symbol master availability later
        atm_step = 50 if underlying.upper() == "NIFTY" else 100
        # We don't know spot without a symbol master; guess from a recent NIFTY level.
        guess_spot = 25000 if underlying.upper() == "NIFTY" else 50000
        strikes = [float(guess_spot + (i - 10) * atm_step) for i in range(21)]
        spot_inst = Instrument(symbol=underlying.upper(), exchange=Exchange.NSE, instrument_type=InstrumentType.INDEX, lot_size=1)
        builder = ChainBuilder(
            broker=adapter,
            underlying=underlying.upper(),
            expiry=expiry,
            strikes=strikes,
            spot_instrument=spot_inst,
        )
        producer = LiveBrokerChainSource(builder=builder, interval_seconds=interval)
    else:
        console.print(f"[red]Source '{source}' not yet wired for live chain ingest — use 'angelone' or 'mock'[/red]")
        sys.exit(1)

    worker = ChainScannerWorker(
        source=producer,
        include_far_otm=include_far_otm,
        min_edge_after_costs_inr=min_edge,
    )

    start_ts = datetime.now(IST)
    console.print(f"[bold]Live arbitrage scanner[/bold] — {underlying} via {source} "
                  f"(interval {interval}s, duration {duration}s)\n")

    async def render(snap, opps):
        elapsed = (datetime.now(IST) - start_ts).total_seconds()
        console.rule(f"[dim]t+{elapsed:>5.1f}s | spot {snap.spot:.2f} | "
                     f"{len(opps)} opps[/dim]")
        if not opps:
            console.print("[dim]  (no opportunities this tick)[/dim]")
            return
        for o in opps[:top]:
            color = "green" if o.edge_after_costs_inr > 0 else "yellow"
            console.print(
                f"  [{color}]{o.kind.value:<22}[/{color}] "
                f"edge ₹{o.edge_per_lot_inr:>7.0f}/lot  "
                f"net ₹{o.edge_after_costs_inr:>7.0f}  "
                f"cap ₹{o.capital_required_inr:>7,.0f}  "
                f"[dim]{o.notes[:60]}[/dim]"
            )

    worker.subscribe(render)

    async def runner():
        max_iters = max(1, int(duration / interval)) if duration > 0 else None
        await worker.run(max_iterations=max_iters)

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        worker.stop()

    stats = worker.stats
    console.print()
    console.print(f"[bold]Done.[/bold] "
                  f"Scans: {stats['scans_completed']}  "
                  f"Opportunities emitted: {stats['opportunities_emitted']}")


@cli.command(help="Start the FastAPI gateway.")
@click.option("--host", default="127.0.0.1")
@click.option("--port", default=8000)
@click.option("--reload", is_flag=True)
def api(host: str, port: int, reload: bool) -> None:
    import uvicorn
    uvicorn.run("qbacktest.gateway.app:app", host=host, port=port, reload=reload)


# Register NSE-direct arb scanner (lives in a separate module to avoid pulling httpx
# into the main import path unnecessarily).
from qbacktest import cli_nse_arb as _cli_nse_arb  # noqa: E402
_cli_nse_arb.register(cli)


@cli.command(name="news", help="Fetch news from RSS/BSE/GDELT and rank stocks likely volatile tomorrow.")
@click.option("--hours", default=24, type=int, help="Look-back window in hours")
@click.option("--top", default=10, type=int, help="Top N volatile-tomorrow tickers to show")
@click.option("--twitter", is_flag=True, help="Include Twitter (requires TWITTER_BEARER_TOKEN)")
@click.option("--show-headlines", default=10, type=int, help="Print N most-recent raw headlines")
@click.option("--json-out", default=None, type=click.Path(), help="Write signals as JSON to this path")
def news_cli(hours: int, top: int, twitter: bool, show_headlines: int, json_out: str | None) -> None:
    import asyncio as _asyncio
    import json as _json
    from qbacktest.signals.pipeline import run_pipeline

    console.print(f"[bold cyan]Fetching news from RSS + BSE + GDELT (last {hours}h)...[/bold cyan]")
    items, signals = _asyncio.run(run_pipeline(look_back_hours=hours, include_twitter=twitter, top_n=top))
    console.print(f"[dim]{len(items)} unique items, {len(signals)} ranked-volatile candidates[/dim]\n")

    if show_headlines > 0:
        from rich.table import Table as _Table
        t = _Table(title=f"Latest {min(show_headlines, len(items))} headlines")
        t.add_column("Time"); t.add_column("Source"); t.add_column("Title")
        for it in items[:show_headlines]:
            t.add_row(it.published_at.strftime("%H:%M %d-%b"), it.source, it.short)
        console.print(t)
        console.print()

    if not signals:
        console.print("[yellow]No volatility signals matched. Try --hours 48 or check feed connectivity.[/yellow]")
        return

    from rich.table import Table as _T
    table = _T(title=f"Top {len(signals)} likely-volatile tomorrow")
    table.add_column("#", justify="right")
    table.add_column("Ticker"); table.add_column("Sentiment"); table.add_column("Event")
    table.add_column("Move%", justify="right"); table.add_column("Dir")
    table.add_column("Conf", justify="right"); table.add_column("State")
    table.add_column("Suggested trade")
    for i, s in enumerate(signals, start=1):
        table.add_row(
            str(i),
            s.ticker,
            s.sentiment.value,
            s.event_kind.value,
            f"{s.expected_move_pct:.1f}%",
            s.expected_direction,
            f"{s.confidence:.2f}",
            s.reaction_state.value,
            s.trade_suggestion,
        )
    console.print(table)
    console.print()
    console.print("[bold]Top headline per ticker:[/bold]")
    for s in signals[:5]:
        console.print(f"  [cyan]{s.ticker}[/cyan]  {s.primary_news.short}")
        console.print(f"    [dim]{s.primary_news.url}[/dim]")
    console.print()
    console.print("[dim]Reminder: heuristic scorer. Verify each headline before trading. Not financial advice.[/dim]")

    if json_out:
        from pathlib import Path as _P
        _P(json_out).write_text(_json.dumps([s.to_json() for s in signals], indent=2))
        console.print(f"\n[green]Wrote signals JSON →[/green] {json_out}")


@cli.command(name="research", help="Pull arxiv finance papers, extract strategies, score Indian feasibility.")
@click.option("--days-back", default=60, type=int)
@click.option("--max-papers", default=40, type=int)
@click.option("--no-llm", is_flag=True, help="Skip LLM extraction (lightweight spec only)")
@click.option("--top", default=15, type=int)
def research_cli(days_back: int, max_papers: int, no_llm: bool, top: int) -> None:
    import asyncio as _asyncio
    from rich.table import Table as _T
    from qbacktest.research.pipeline import run_research_pipeline

    console.print(f"[bold cyan]Pulling arxiv q-fin papers (last {days_back} days)...[/bold cyan]")
    papers, candidates = _asyncio.run(run_research_pipeline(
        days_back=days_back, max_papers=max_papers, use_llm=not no_llm,
    ))
    console.print(f"[dim]{len(papers)} papers, {len(candidates)} extracted strategies[/dim]\n")

    table = _T(title=f"Top {min(top, len(candidates))} candidate strategies (ranked by Indian-feasibility)")
    table.add_column("#", justify="right")
    table.add_column("Strategy")
    table.add_column("Asset")
    table.add_column("Market")
    table.add_column("Sharpe")
    table.add_column("Feas.")
    table.add_column("Blockers")
    for i, c in enumerate(candidates[:top], start=1):
        table.add_row(
            str(i),
            c.spec.name[:42],
            c.spec.asset_class.value,
            c.spec.market_type.value,
            f"{c.spec.claimed_sharpe:.2f}" if c.spec.claimed_sharpe else "-",
            f"{c.feasibility.overall_score:.2f}",
            "; ".join(c.feasibility.blockers)[:32] or "-",
        )
    console.print(table)

    console.print()
    console.print("[bold]Top 3 in detail:[/bold]")
    for i, c in enumerate(candidates[:3], start=1):
        console.print(f"\n[cyan]#{i} {c.spec.name}[/cyan]   "
                      f"[dim]({c.spec.asset_class.value}, {c.spec.market_type.value})[/dim]")
        console.print(f"  Paper: {c.paper.title[:90]}")
        console.print(f"  arxiv: {c.paper.arxiv_id}  ({c.paper.pdf_url})")
        console.print(f"  Signal: {c.spec.signal_summary[:200]}")
        console.print(f"  Entry: {c.spec.entry_rule[:120]}")
        console.print(f"  Exit:  {c.spec.exit_rule[:120]}")
        console.print(f"  Hold:  {c.spec.holding_period}")
        if c.spec.claimed_sharpe:
            console.print(f"  Claimed Sharpe: {c.spec.claimed_sharpe:.2f}")
        console.print(f"  [bold]Indian feasibility[/bold]: overall={c.feasibility.overall_score:.2f}  "
                      f"data={c.feasibility.data_score:.2f}  cap={c.feasibility.capacity_score:.2f}  "
                      f"cost={c.feasibility.cost_score:.2f}  regs={c.feasibility.regulatory_score:.2f}")
        if c.feasibility.blockers:
            console.print(f"  [yellow]Blockers:[/yellow] {', '.join(c.feasibility.blockers)}")
        if c.feasibility.notes:
            console.print(f"  [dim]{c.feasibility.notes}[/dim]")


@cli.command(name="scan-scalping",
             help="Run intraday scalping signal engine (ORB + GapFade + Reversion) on mock or live bars.")
@click.option("--source", default="mock", type=click.Choice(["mock", "angelone"]),
              help="mock = synthetic bars (offline); angelone = live ticks (requires auth).")
@click.option("--underlying", default="NIFTY")
@click.option("--interval", default=1.0, type=float, help="Seconds between bars (mock only)")
@click.option("--bars", default=400, type=int, help="Max bars to process before stopping (~1 trading day at 1m)")
@click.option("--min-edge", default=0.0, type=float, help="Min expectancy INR/lot to surface a signal")
@click.option("--regime/--no-regime", default=True, help="Apply RegimeFilter (block premium-sell on event days)")
@click.option("--seed", default=None, type=int, help="Mock RNG seed")
def scan_scalping(source: str, underlying: str, interval: float, bars: int,
                  min_edge: float, regime: bool, seed: int | None) -> None:
    import asyncio as _asyncio
    from qbacktest.signals.scalping import (
        GapFadeDetector, LastHourReversionDetector, MockIntradayTicker,
        ORBDetector, ScalpingEngine,
    )

    if source == "mock":
        ticker = MockIntradayTicker(underlying=underlying, interval_seconds=interval)
        if seed is not None:
            import random as _r
            ticker._rng = _r.Random(seed)
    else:
        console.print("[red]Live AngelOne bar source for scalping not wired in v0 — requires symbol-master pre-fetch[/red]")
        sys.exit(1)

    engine = ScalpingEngine(
        source=ticker,
        detectors=(
            ORBDetector(underlying=underlying),
            GapFadeDetector(underlying=underlying),
            LastHourReversionDetector(underlying=underlying),
        ),
        apply_regime=regime,
        min_expectancy_inr=min_edge,
    )
    console.print(f"[bold]Scalping signal engine[/bold] — {underlying}/{source}  "
                  f"(interval {interval}s, max {bars} bars, regime={regime})\n")

    async def render(sig):
        arrow = "↑" if sig.direction.value == "LONG" else "↓"
        color = "green" if sig.expectancy_inr_per_lot > 0 else "yellow"
        console.print(f"[{color}]{sig.kind.value:<22}[/{color}] {arrow} {sig.ticker:<12} "
                      f"entry={sig.entry:.2f}  SL={sig.stop:.2f}  TGT={sig.target:.2f}  "
                      f"R:R={sig.reward_to_risk:.2f}  E=₹{sig.expectancy_inr_per_lot:,.0f}/lot")
        console.print(f"  [dim]{sig.rationale}  ·  exit by {sig.exit_by_time.strftime('%H:%M')}"
                      f"{('  ·  regime: ' + sig.regime_note) if sig.regime_note else ''}[/dim]")

    engine.subscribe(render)
    try:
        _asyncio.run(engine.run(max_bars=bars))
    except KeyboardInterrupt:
        engine.stop()
    stats = engine.stats
    console.print()
    console.print(f"[bold]Done.[/bold] bars={stats['bars_processed']}  "
                  f"signals={stats['signals_emitted']}  skipped(regime/edge)={stats['signals_skipped']}")


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
