"""Rich terminal display for the full intraday trading pipeline.

Provides a live-updating layout that shows progress across all 5 pipeline phases:
screening, multi-agent analysis, execution, monitoring, and reporting.
"""

import threading
import time
from datetime import datetime
from typing import List, Optional

from rich import box
from rich.align import Align
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

# force_terminal=True keeps Rich in cursor-control mode even when stdout is
# not a real TTY (some IDE terminals misreport). Without this, Live renders
# in "non-terminal" fallback mode where each refresh appends a new frame
# instead of overwriting the previous one — the bug that produces stacked
# header banners.
console = Console(force_terminal=True)

# Phase definitions
PHASES = [
    "Screening",
    "Analysis",
    "Execution",
    "Monitoring",
    "Reporting",
]


class PipelineDisplay:
    """Live terminal display for the 5-phase pipeline."""

    def __init__(self):
        self.start_time: float = 0
        self.live: Optional[Live] = None
        self._lock = threading.Lock()

        # Phase state: "pending", "in_progress", "done", "skipped"
        self.phase_status = {i: "pending" for i in range(5)}
        self.phase_detail = {i: "" for i in range(5)}

        # Stock queue for Phase 2
        self.stocks: List[dict] = []
        self.stock_status: dict = {}  # ticker -> "pending"/"in_progress"/"done"/"failed"

        # Sequential mode: agent status for the single in-flight stock
        self.current_ticker: Optional[str] = None
        self.agent_status: dict = {}  # agent_name -> "pending"/"in_progress"/"completed"

        # Parallel mode: per-ticker live agent + chunk count
        self.parallel_mode: bool = False
        self.ticker_states: dict = {}  # ticker -> {"agent": str, "chunk_count": int}

        # Activity log (most recent messages)
        self.activity_lines: List[str] = []
        self.max_activity_lines = 12

        # Live content panel — shows what agents are saying/doing right now
        self.live_content: List[str] = []
        self.max_content_lines = 20

        # Results
        self.results: List[str] = []

        # Stats
        self.llm_calls = 0
        self.tool_calls = 0
        self.tokens_in = 0
        self.tokens_out = 0

        # Monitoring state
        self.monitoring_tickers: List[str] = []
        self.monitoring_capital: float = 0
        self.monitoring_open: int = 0

    def start(self):
        """Start the live display.

        ``screen=True`` swaps to the terminal's alt-screen buffer so the
        dashboard renders in a fixed region. Combined with
        ``Console(force_terminal=True)``, this prevents the
        "frames stacking on top of each other" bug where each refresh paints
        a new banner without erasing the previous one. ``redirect_stdout``
        and ``redirect_stderr`` keep stray writes (worker-thread logging,
        library warnings) from corrupting the cursor.
        """
        self.start_time = time.time()
        self.live = Live(
            self._build_layout(),
            refresh_per_second=4,
            console=console,
            screen=True,
            redirect_stdout=True,
            redirect_stderr=True,
        )
        self.live.start()

    def stop(self):
        """Stop the live display."""
        if self.live:
            self.live.stop()
            self.live = None

    def refresh(self):
        """Force a display refresh.

        Thread-safe: parallel workers call display methods concurrently, and
        rich's Live object is not safe to update from multiple threads at once.
        """
        with self._lock:
            if self.live:
                self.live.update(self._build_layout())

    # --- Phase control ---

    def set_phase(self, phase_num: int, status: str, detail: str = ""):
        """Update phase status. phase_num is 0-indexed."""
        self.phase_status[phase_num] = status
        if detail:
            self.phase_detail[phase_num] = detail
        self.refresh()

    # --- Stock queue ---

    def set_stocks(self, stocks: List[dict]):
        """Set the stock queue for Phase 2."""
        self.stocks = stocks
        self.stock_status = {s["ticker"]: "pending" for s in stocks}
        self.refresh()

    def update_stock_status(self, ticker: str, status: str):
        """Update a stock's status in the queue."""
        self.stock_status[ticker] = status
        if status == "in_progress":
            self.current_ticker = ticker
        self.refresh()

    # --- Agent progress (within a single stock) ---

    def reset_agents(self):
        """Reset agent status for a new stock analysis."""
        self.agent_status = {
            "Market Analyst": "pending",
            "Retail Sentiment": "pending",
            "Institutional Sentiment": "pending",
            "Contrarian Sentiment": "pending",
            "News Analyst": "pending",
            "Fundamentals Analyst": "pending",
            "Bull Researcher": "pending",
            "Bear Researcher": "pending",
            "Research Manager": "pending",
            "Trader": "pending",
            "Aggressive Analyst": "pending",
            "Conservative Analyst": "pending",
            "Neutral Analyst": "pending",
            "Portfolio Manager": "pending",
        }
        # Clear live content for new stock
        self.live_content = []

    def update_agent(self, agent_name: str, status: str):
        """Update an agent's status."""
        if agent_name in self.agent_status:
            self.agent_status[agent_name] = status
            self.refresh()

    # --- Parallel mode (multiple stocks analyzing concurrently) ---

    def set_parallel_mode(self, ticker_states: dict):
        """Switch the agents panel to per-ticker progress rows.

        ticker_states is keyed by ticker, each value a dict with 'agent' and
        'chunk_count'. The same dict is mutated by worker threads as they stream
        chunks; we just hold a reference and re-render on refresh().
        """
        self.parallel_mode = True
        self.ticker_states = ticker_states
        self.refresh()

    def update_parallel_progress(self, ticker_states: dict):
        """Refresh the parallel-mode agents panel after a worker updates its row."""
        self.ticker_states = ticker_states
        self.refresh()

    # --- Activity log ---

    def add_activity(self, text: str):
        """Add a line to the activity log."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.activity_lines.append(f"[dim]{timestamp}[/dim] {text}")
        if len(self.activity_lines) > self.max_activity_lines:
            self.activity_lines = self.activity_lines[-self.max_activity_lines:]
        self.refresh()

    # --- Live content (debate text, tool calls, report snippets) ---

    def add_content(self, text: str):
        """Add a line to the live content panel (what agents are doing/saying)."""
        self.live_content.append(text)
        if len(self.live_content) > self.max_content_lines:
            self.live_content = self.live_content[-self.max_content_lines:]
        self.refresh()

    def set_content_section(self, title: str, body: str):
        """Replace live content with a titled section (for debate rounds, etc.)."""
        lines = [f"[bold cyan]{title}[/bold cyan]"]
        # Truncate long body to fit display
        for line in body.split("\n")[:12]:
            if len(line) > 120:
                line = line[:117] + "..."
            lines.append(line)
        if body.count("\n") > 12:
            lines.append(f"[dim]... ({body.count(chr(10)) - 12} more lines)[/dim]")
        self.live_content = lines
        self.refresh()

    # --- Results ---

    def add_result(self, ticker: str, summary: str):
        """Add a completed result."""
        self.results.append(f"[bold]{ticker}[/bold]: {summary}")
        self.refresh()

    # --- Stats ---

    def update_stats(self, llm_calls: int, tool_calls: int, tokens_in: int, tokens_out: int):
        """Update footer stats."""
        self.llm_calls = llm_calls
        self.tool_calls = tool_calls
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out
        self.refresh()

    # --- Monitoring ---

    def set_monitoring(self, tickers: List[str], capital: float, open_positions: int):
        """Update monitoring status for Phase 4."""
        self.monitoring_tickers = tickers
        self.monitoring_capital = capital
        self.monitoring_open = open_positions
        self.refresh()

    # --- Layout building ---

    def _build_layout(self) -> Layout:
        """Build the full terminal layout."""
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="main"),
            Layout(name="footer", size=3),
        )

        # Main: top row (phases + stocks + agents) and bottom row (content + results)
        layout["main"].split_column(
            Layout(name="upper", ratio=2),
            Layout(name="lower", ratio=3),
        )
        layout["upper"].split_row(
            Layout(name="phases", ratio=3),
            Layout(name="stocks", ratio=1),
            Layout(name="agents", ratio=2),
        )
        layout["lower"].split_row(
            Layout(name="content", ratio=3),
            Layout(name="results", ratio=2),
        )

        # Header
        elapsed = time.time() - self.start_time if self.start_time else 0
        elapsed_str = f"{int(elapsed // 60):02d}:{int(elapsed % 60):02d}"
        date_str = datetime.now().strftime("%Y-%m-%d")

        layout["header"].update(
            Panel(
                f"[bold green]TradingAgents Pipeline[/bold green] - {date_str}  |  "
                f"Elapsed: {elapsed_str}",
                border_style="green",
            )
        )

        # Phases panel
        layout["phases"].update(self._build_phases_panel())

        # Stock queue panel
        layout["stocks"].update(self._build_stocks_panel())

        # Agents panel (current stock's agent progress)
        layout["agents"].update(self._build_agents_panel())

        # Live content panel (debate/tool calls/report text)
        layout["content"].update(self._build_content_panel())

        # Results panel
        layout["results"].update(self._build_results_panel())

        # Footer
        layout["footer"].update(self._build_footer())

        return layout

    def _build_phases_panel(self) -> Panel:
        """Build the phase progress panel."""
        table = Table(
            show_header=False, box=None, padding=(0, 1), expand=True
        )
        table.add_column("Icon", width=3)
        table.add_column("Phase", ratio=1)
        table.add_column("Status", width=14)

        for i, name in enumerate(PHASES):
            status = self.phase_status[i]
            detail = self.phase_detail[i]

            if status == "done":
                icon = "[green]●[/green]"
                status_text = f"[green]done[/green]"
            elif status == "in_progress":
                icon = "[cyan]◉[/cyan]"
                status_text = f"[cyan]{detail or 'running'}[/cyan]"
            elif status == "skipped":
                icon = "[dim]○[/dim]"
                status_text = "[dim]skipped[/dim]"
            else:
                icon = "[dim]○[/dim]"
                status_text = "[dim]pending[/dim]"

            table.add_row(icon, f"Phase {i+1}: {name}", status_text)

        return Panel(table, title="Pipeline Progress", border_style="cyan", padding=(0, 1))

    def _build_stocks_panel(self) -> Panel:
        """Build the stock queue panel."""
        if not self.stocks:
            return Panel("[dim]Waiting for screening...[/dim]", title="Stocks", border_style="blue")

        table = Table(show_header=False, box=None, padding=(0, 0), expand=True)
        table.add_column("N", width=3)
        table.add_column("Ticker", ratio=1)
        table.add_column("St", width=2)

        for i, stock in enumerate(self.stocks, 1):
            ticker = stock["ticker"]
            status = self.stock_status.get(ticker, "pending")

            if status == "done":
                icon = "[green]✓[/green]"
            elif status == "in_progress":
                icon = "[cyan]⟳[/cyan]"
            elif status == "failed":
                icon = "[red]✗[/red]"
            else:
                icon = "[dim]·[/dim]"

            style = "bold" if status == "in_progress" else ""
            table.add_row(f"{i}.", f"[{style}]{ticker}[/{style}]" if style else ticker, icon)

        return Panel(table, title="Stocks", border_style="blue", padding=(0, 1))

    def _build_agents_panel(self) -> Panel:
        """Build the agent progress panel.

        Sequential mode: all 14 agent rows for the single in-flight ticker.
        Parallel mode: one row per ticker showing currently-active agent + chunk count.
        """
        if self.parallel_mode and self.ticker_states:
            return self._build_parallel_agents_panel()

        if not self.agent_status:
            return Panel("[dim]Waiting for analysis...[/dim]", title="Agents", border_style="magenta")

        table = Table(show_header=False, box=None, padding=(0, 0), expand=True)
        table.add_column("Agent", ratio=1)
        table.add_column("St", width=8)

        for agent, status in self.agent_status.items():
            if status == "completed":
                st = "[green]done[/green]"
            elif status == "in_progress":
                st = "[cyan]active[/cyan]"
            else:
                st = "[dim]...[/dim]"
            table.add_row(agent, st)

        title = f"Agents ({self.current_ticker})" if self.current_ticker else "Agents"
        return Panel(table, title=title, border_style="magenta", padding=(0, 1))

    def _build_parallel_agents_panel(self) -> Panel:
        """Per-ticker progress rows for parallel mode."""
        table = Table(show_header=True, box=box.SIMPLE_HEAD, padding=(0, 1), expand=True)
        table.add_column("Ticker", ratio=1)
        table.add_column("Active Agent", ratio=2)
        table.add_column("Chunks", width=7, justify="right")

        for ticker, st in self.ticker_states.items():
            stock_status = self.stock_status.get(ticker, "pending")
            if stock_status == "done":
                agent_cell = "[green]✓ complete[/green]"
            elif stock_status == "failed":
                agent_cell = "[red]✗ failed[/red]"
            else:
                agent_cell = f"[cyan]{st.get('agent', '...')}[/cyan]"
            table.add_row(ticker, agent_cell, str(st.get("chunk_count", 0)))

        return Panel(table, title="Per-Ticker Progress", border_style="magenta", padding=(0, 1))

    def _build_content_panel(self) -> Panel:
        """Build the live content panel showing debate/agent output."""
        if self.live_content:
            content = "\n".join(self.live_content)
        elif self.activity_lines:
            content = "\n".join(self.activity_lines)
        else:
            content = "[dim]Waiting for agent output...[/dim]"

        title = "Live Output"
        if self.current_ticker:
            title = f"Live Output ({self.current_ticker})"

        return Panel(content, title=title, border_style="green", padding=(0, 1))

    def _build_results_panel(self) -> Panel:
        """Build the results panel."""
        if not self.results:
            return Panel("[dim]No results yet...[/dim]", title="Results", border_style="yellow", padding=(0, 1))

        content = "\n".join(self.results)
        return Panel(content, title="Results", border_style="yellow", padding=(0, 1))

    def _build_footer(self) -> Panel:
        """Build the footer with stats."""
        elapsed = time.time() - self.start_time if self.start_time else 0
        elapsed_str = f"{int(elapsed // 60):02d}:{int(elapsed % 60):02d}"

        def fmt_tokens(n):
            return f"{n/1000:.1f}k" if n >= 1000 else str(n)

        parts = [
            f"Elapsed: {elapsed_str}",
            f"LLM: {self.llm_calls}",
            f"Tools: {self.tool_calls}",
            f"Tokens: {fmt_tokens(self.tokens_in)}↑ {fmt_tokens(self.tokens_out)}↓",
        ]

        if self.monitoring_capital > 0:
            parts.append(f"Capital: ₹{self.monitoring_capital:.0f}")
            parts.append(f"Open: {self.monitoring_open}")

        return Panel(
            " | ".join(parts),
            border_style="grey50",
        )
