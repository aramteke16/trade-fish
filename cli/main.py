from typing import Optional
import datetime
import typer
from pathlib import Path
from functools import wraps
from rich.console import Console
from dotenv import load_dotenv

# Load environment variables
load_dotenv()
load_dotenv(".env.enterprise", override=False)
from rich.panel import Panel
from rich.spinner import Spinner
from rich.live import Live
from rich.columns import Columns
from rich.markdown import Markdown
from rich.layout import Layout
from rich.text import Text
from rich.table import Table
from collections import deque
import time
from rich.tree import Tree
from rich import box
from rich.align import Align
from rich.rule import Rule

from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG  # noqa: F401  (fallback only)


def _runtime_config():
    """DB-backed runtime config; falls back to the static DEFAULT_CONFIG dict
    if the DB is unreachable so unit tests / first-import code never break."""
    try:
        from tradingagents.web.config_service import load_config
        return load_config()
    except Exception:
        return dict(DEFAULT_CONFIG)
from cli.models import AnalystType
from cli.utils import *
from cli.announcements import fetch_announcements, display_announcements
from cli.stats_handler import StatsCallbackHandler

console = Console()

app = typer.Typer(
    name="TradingAgents",
    help="TradingAgents CLI: Multi-Agents LLM Financial Trading Framework",
    add_completion=True,  # Enable shell completion
)


# Create a deque to store recent messages with a maximum length
class MessageBuffer:
    # Fixed teams that always run (not user-selectable)
    FIXED_AGENTS = {
        "Research Team": ["Bull Researcher", "Bear Researcher", "Research Manager"],
        "Trading Team": ["Trader"],
        "Risk Management": ["Aggressive Analyst", "Neutral Analyst", "Conservative Analyst"],
        "Portfolio Management": ["Portfolio Manager"],
    }

    # Analyst name mapping
    ANALYST_MAPPING = {
        "market": "Market Analyst",
        "social": "Social Analyst",
        "news": "News Analyst",
        "fundamentals": "Fundamentals Analyst",
    }

    # Report section mapping: section -> (analyst_key for filtering, finalizing_agent)
    # analyst_key: which analyst selection controls this section (None = always included)
    # finalizing_agent: which agent must be "completed" for this report to count as done
    REPORT_SECTIONS = {
        "market_report": ("market", "Market Analyst"),
        "sentiment_report": ("social", "Social Analyst"),
        "news_report": ("news", "News Analyst"),
        "fundamentals_report": ("fundamentals", "Fundamentals Analyst"),
        "investment_plan": (None, "Research Manager"),
        "trader_investment_plan": (None, "Trader"),
        "final_trade_decision": (None, "Portfolio Manager"),
    }

    def __init__(self, max_length=100):
        self.messages = deque(maxlen=max_length)
        self.tool_calls = deque(maxlen=max_length)
        self.current_report = None
        self.final_report = None  # Store the complete final report
        self.agent_status = {}
        self.current_agent = None
        self.report_sections = {}
        self.selected_analysts = []
        self._processed_message_ids = set()

    def init_for_analysis(self, selected_analysts):
        """Initialize agent status and report sections based on selected analysts.

        Args:
            selected_analysts: List of analyst type strings (e.g., ["market", "news"])
        """
        self.selected_analysts = [a.lower() for a in selected_analysts]

        # Build agent_status dynamically
        self.agent_status = {}

        # Add selected analysts
        for analyst_key in self.selected_analysts:
            if analyst_key in self.ANALYST_MAPPING:
                self.agent_status[self.ANALYST_MAPPING[analyst_key]] = "pending"

        # Add fixed teams
        for team_agents in self.FIXED_AGENTS.values():
            for agent in team_agents:
                self.agent_status[agent] = "pending"

        # Build report_sections dynamically
        self.report_sections = {}
        for section, (analyst_key, _) in self.REPORT_SECTIONS.items():
            if analyst_key is None or analyst_key in self.selected_analysts:
                self.report_sections[section] = None

        # Reset other state
        self.current_report = None
        self.final_report = None
        self.current_agent = None
        self.messages.clear()
        self.tool_calls.clear()
        self._processed_message_ids.clear()

    def get_completed_reports_count(self):
        """Count reports that are finalized (their finalizing agent is completed).

        A report is considered complete when:
        1. The report section has content (not None), AND
        2. The agent responsible for finalizing that report has status "completed"

        This prevents interim updates (like debate rounds) from counting as completed.
        """
        count = 0
        for section in self.report_sections:
            if section not in self.REPORT_SECTIONS:
                continue
            _, finalizing_agent = self.REPORT_SECTIONS[section]
            # Report is complete if it has content AND its finalizing agent is done
            has_content = self.report_sections.get(section) is not None
            agent_done = self.agent_status.get(finalizing_agent) == "completed"
            if has_content and agent_done:
                count += 1
        return count

    def add_message(self, message_type, content):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.messages.append((timestamp, message_type, content))

    def add_tool_call(self, tool_name, args):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.tool_calls.append((timestamp, tool_name, args))

    def update_agent_status(self, agent, status):
        if agent in self.agent_status:
            self.agent_status[agent] = status
            self.current_agent = agent

    def update_report_section(self, section_name, content):
        if section_name in self.report_sections:
            self.report_sections[section_name] = content
            self._update_current_report()

    def _update_current_report(self):
        # For the panel display, only show the most recently updated section
        latest_section = None
        latest_content = None

        # Find the most recently updated section
        for section, content in self.report_sections.items():
            if content is not None:
                latest_section = section
                latest_content = content
               
        if latest_section and latest_content:
            # Format the current section for display
            section_titles = {
                "market_report": "Market Analysis",
                "sentiment_report": "Social Sentiment",
                "news_report": "News Analysis",
                "fundamentals_report": "Fundamentals Analysis",
                "investment_plan": "Research Team Decision",
                "trader_investment_plan": "Trading Team Plan",
                "final_trade_decision": "Portfolio Management Decision",
            }
            self.current_report = (
                f"### {section_titles[latest_section]}\n{latest_content}"
            )

        # Update the final complete report
        self._update_final_report()

    def _update_final_report(self):
        report_parts = []

        # Analyst Team Reports - use .get() to handle missing sections
        analyst_sections = ["market_report", "sentiment_report", "news_report", "fundamentals_report"]
        if any(self.report_sections.get(section) for section in analyst_sections):
            report_parts.append("## Analyst Team Reports")
            if self.report_sections.get("market_report"):
                report_parts.append(
                    f"### Market Analysis\n{self.report_sections['market_report']}"
                )
            if self.report_sections.get("sentiment_report"):
                report_parts.append(
                    f"### Social Sentiment\n{self.report_sections['sentiment_report']}"
                )
            if self.report_sections.get("news_report"):
                report_parts.append(
                    f"### News Analysis\n{self.report_sections['news_report']}"
                )
            if self.report_sections.get("fundamentals_report"):
                report_parts.append(
                    f"### Fundamentals Analysis\n{self.report_sections['fundamentals_report']}"
                )

        # Research Team Reports
        if self.report_sections.get("investment_plan"):
            report_parts.append("## Research Team Decision")
            report_parts.append(f"{self.report_sections['investment_plan']}")

        # Trading Team Reports
        if self.report_sections.get("trader_investment_plan"):
            report_parts.append("## Trading Team Plan")
            report_parts.append(f"{self.report_sections['trader_investment_plan']}")

        # Portfolio Management Decision
        if self.report_sections.get("final_trade_decision"):
            report_parts.append("## Portfolio Management Decision")
            report_parts.append(f"{self.report_sections['final_trade_decision']}")

        self.final_report = "\n\n".join(report_parts) if report_parts else None


message_buffer = MessageBuffer()


def create_layout():
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="main"),
        Layout(name="footer", size=3),
    )
    layout["main"].split_column(
        Layout(name="upper", ratio=3), Layout(name="analysis", ratio=5)
    )
    layout["upper"].split_row(
        Layout(name="progress", ratio=2), Layout(name="messages", ratio=3)
    )
    return layout


def format_tokens(n):
    """Format token count for display."""
    if n >= 1000:
        return f"{n/1000:.1f}k"
    return str(n)


def update_display(layout, spinner_text=None, stats_handler=None, start_time=None):
    # Header with welcome message
    layout["header"].update(
        Panel(
            "[bold green]Welcome to TradingAgents CLI[/bold green]\n"
            "[dim]© [Tauric Research](https://github.com/TauricResearch)[/dim]",
            title="Welcome to TradingAgents",
            border_style="green",
            padding=(1, 2),
            expand=True,
        )
    )

    # Progress panel showing agent status
    progress_table = Table(
        show_header=True,
        header_style="bold magenta",
        show_footer=False,
        box=box.SIMPLE_HEAD,  # Use simple header with horizontal lines
        title=None,  # Remove the redundant Progress title
        padding=(0, 2),  # Add horizontal padding
        expand=True,  # Make table expand to fill available space
    )
    progress_table.add_column("Team", style="cyan", justify="center", width=20)
    progress_table.add_column("Agent", style="green", justify="center", width=20)
    progress_table.add_column("Status", style="yellow", justify="center", width=20)

    # Group agents by team - filter to only include agents in agent_status
    all_teams = {
        "Analyst Team": [
            "Market Analyst",
            "Social Analyst",
            "News Analyst",
            "Fundamentals Analyst",
        ],
        "Research Team": ["Bull Researcher", "Bear Researcher", "Research Manager"],
        "Trading Team": ["Trader"],
        "Risk Management": ["Aggressive Analyst", "Neutral Analyst", "Conservative Analyst"],
        "Portfolio Management": ["Portfolio Manager"],
    }

    # Filter teams to only include agents that are in agent_status
    teams = {}
    for team, agents in all_teams.items():
        active_agents = [a for a in agents if a in message_buffer.agent_status]
        if active_agents:
            teams[team] = active_agents

    for team, agents in teams.items():
        # Add first agent with team name
        first_agent = agents[0]
        status = message_buffer.agent_status.get(first_agent, "pending")
        if status == "in_progress":
            spinner = Spinner(
                "dots", text="[blue]in_progress[/blue]", style="bold cyan"
            )
            status_cell = spinner
        else:
            status_color = {
                "pending": "yellow",
                "completed": "green",
                "error": "red",
            }.get(status, "white")
            status_cell = f"[{status_color}]{status}[/{status_color}]"
        progress_table.add_row(team, first_agent, status_cell)

        # Add remaining agents in team
        for agent in agents[1:]:
            status = message_buffer.agent_status.get(agent, "pending")
            if status == "in_progress":
                spinner = Spinner(
                    "dots", text="[blue]in_progress[/blue]", style="bold cyan"
                )
                status_cell = spinner
            else:
                status_color = {
                    "pending": "yellow",
                    "completed": "green",
                    "error": "red",
                }.get(status, "white")
                status_cell = f"[{status_color}]{status}[/{status_color}]"
            progress_table.add_row("", agent, status_cell)

        # Add horizontal line after each team
        progress_table.add_row("─" * 20, "─" * 20, "─" * 20, style="dim")

    layout["progress"].update(
        Panel(progress_table, title="Progress", border_style="cyan", padding=(1, 2))
    )

    # Messages panel showing recent messages and tool calls
    messages_table = Table(
        show_header=True,
        header_style="bold magenta",
        show_footer=False,
        expand=True,  # Make table expand to fill available space
        box=box.MINIMAL,  # Use minimal box style for a lighter look
        show_lines=True,  # Keep horizontal lines
        padding=(0, 1),  # Add some padding between columns
    )
    messages_table.add_column("Time", style="cyan", width=8, justify="center")
    messages_table.add_column("Type", style="green", width=10, justify="center")
    messages_table.add_column(
        "Content", style="white", no_wrap=False, ratio=1
    )  # Make content column expand

    # Combine tool calls and messages
    all_messages = []

    # Add tool calls
    for timestamp, tool_name, args in message_buffer.tool_calls:
        formatted_args = format_tool_args(args)
        all_messages.append((timestamp, "Tool", f"{tool_name}: {formatted_args}"))

    # Add regular messages
    for timestamp, msg_type, content in message_buffer.messages:
        content_str = str(content) if content else ""
        if len(content_str) > 200:
            content_str = content_str[:197] + "..."
        all_messages.append((timestamp, msg_type, content_str))

    # Sort by timestamp descending (newest first)
    all_messages.sort(key=lambda x: x[0], reverse=True)

    # Calculate how many messages we can show based on available space
    max_messages = 12

    # Get the first N messages (newest ones)
    recent_messages = all_messages[:max_messages]

    # Add messages to table (already in newest-first order)
    for timestamp, msg_type, content in recent_messages:
        # Format content with word wrapping
        wrapped_content = Text(content, overflow="fold")
        messages_table.add_row(timestamp, msg_type, wrapped_content)

    layout["messages"].update(
        Panel(
            messages_table,
            title="Messages & Tools",
            border_style="blue",
            padding=(1, 2),
        )
    )

    # Analysis panel showing current report
    if message_buffer.current_report:
        layout["analysis"].update(
            Panel(
                Markdown(message_buffer.current_report),
                title="Current Report",
                border_style="green",
                padding=(1, 2),
            )
        )
    else:
        layout["analysis"].update(
            Panel(
                "[italic]Waiting for analysis report...[/italic]",
                title="Current Report",
                border_style="green",
                padding=(1, 2),
            )
        )

    # Footer with statistics
    # Agent progress - derived from agent_status dict
    agents_completed = sum(
        1 for status in message_buffer.agent_status.values() if status == "completed"
    )
    agents_total = len(message_buffer.agent_status)

    # Report progress - based on agent completion (not just content existence)
    reports_completed = message_buffer.get_completed_reports_count()
    reports_total = len(message_buffer.report_sections)

    # Build stats parts
    stats_parts = [f"Agents: {agents_completed}/{agents_total}"]

    # LLM and tool stats from callback handler
    if stats_handler:
        stats = stats_handler.get_stats()
        stats_parts.append(f"LLM: {stats['llm_calls']}")
        stats_parts.append(f"Tools: {stats['tool_calls']}")

        # Token display with graceful fallback
        if stats["tokens_in"] > 0 or stats["tokens_out"] > 0:
            tokens_str = f"Tokens: {format_tokens(stats['tokens_in'])}\u2191 {format_tokens(stats['tokens_out'])}\u2193"
        else:
            tokens_str = "Tokens: --"
        stats_parts.append(tokens_str)

    stats_parts.append(f"Reports: {reports_completed}/{reports_total}")

    # Elapsed time
    if start_time:
        elapsed = time.time() - start_time
        elapsed_str = f"\u23f1 {int(elapsed // 60):02d}:{int(elapsed % 60):02d}"
        stats_parts.append(elapsed_str)

    stats_table = Table(show_header=False, box=None, padding=(0, 2), expand=True)
    stats_table.add_column("Stats", justify="center")
    stats_table.add_row(" | ".join(stats_parts))

    layout["footer"].update(Panel(stats_table, border_style="grey50"))


def get_user_selections():
    """Get all user selections before starting the analysis display."""
    # Display ASCII art welcome message
    with open(Path(__file__).parent / "static" / "welcome.txt", "r", encoding="utf-8") as f:
        welcome_ascii = f.read()

    # Create welcome box content
    welcome_content = f"{welcome_ascii}\n"
    welcome_content += "[bold green]TradingAgents: Multi-Agents LLM Financial Trading Framework - CLI[/bold green]\n\n"
    welcome_content += "[bold]Workflow Steps:[/bold]\n"
    welcome_content += "I. Analyst Team → II. Research Team → III. Trader → IV. Risk Management → V. Portfolio Management\n\n"
    welcome_content += (
        "[dim]Built by [Tauric Research](https://github.com/TauricResearch)[/dim]"
    )

    # Create and center the welcome box
    welcome_box = Panel(
        welcome_content,
        border_style="green",
        padding=(1, 2),
        title="Welcome to TradingAgents",
        subtitle="Multi-Agents LLM Financial Trading Framework",
    )
    console.print(Align.center(welcome_box))
    console.print()
    console.print()  # Add vertical space before announcements

    # Fetch and display announcements (silent on failure)
    announcements = fetch_announcements()
    display_announcements(console, announcements)

    # Create a boxed questionnaire for each step
    def create_question_box(title, prompt, default=None):
        box_content = f"[bold]{title}[/bold]\n"
        box_content += f"[dim]{prompt}[/dim]"
        if default:
            box_content += f"\n[dim]Default: {default}[/dim]"
        return Panel(box_content, border_style="blue", padding=(1, 2))

    # Step 1: Ticker symbol
    console.print(
        create_question_box(
            "Step 1: Ticker Symbol",
            "Enter the exact ticker symbol to analyze, including exchange suffix when needed (examples: SPY, CNC.TO, 7203.T, 0700.HK)",
            "SPY",
        )
    )
    selected_ticker = get_ticker()

    # Step 2: Analysis date
    default_date = datetime.datetime.now().strftime("%Y-%m-%d")
    console.print(
        create_question_box(
            "Step 2: Analysis Date",
            "Enter the analysis date (YYYY-MM-DD)",
            default_date,
        )
    )
    analysis_date = get_analysis_date()

    # Step 3: Output language
    console.print(
        create_question_box(
            "Step 3: Output Language",
            "Select the language for analyst reports and final decision"
        )
    )
    output_language = ask_output_language()

    # Step 4: Select analysts
    console.print(
        create_question_box(
            "Step 4: Analysts Team", "Select your LLM analyst agents for the analysis"
        )
    )
    selected_analysts = select_analysts()
    console.print(
        f"[green]Selected analysts:[/green] {', '.join(analyst.value for analyst in selected_analysts)}"
    )

    # Step 5: Research depth
    console.print(
        create_question_box(
            "Step 5: Research Depth", "Select your research depth level"
        )
    )
    selected_research_depth = select_research_depth()

    # Step 6: LLM Provider
    console.print(
        create_question_box(
            "Step 6: LLM Provider", "Select your LLM provider"
        )
    )
    selected_llm_provider, backend_url = select_llm_provider()

    # Step 7: Thinking agents
    console.print(
        create_question_box(
            "Step 7: Thinking Agents", "Select your thinking agents for analysis"
        )
    )
    selected_shallow_thinker = select_shallow_thinking_agent(selected_llm_provider)
    selected_deep_thinker = select_deep_thinking_agent(selected_llm_provider)

    # Step 8: Provider-specific thinking configuration
    thinking_level = None
    reasoning_effort = None
    anthropic_effort = None

    provider_lower = selected_llm_provider.lower()
    if provider_lower == "google":
        console.print(
            create_question_box(
                "Step 8: Thinking Mode",
                "Configure Gemini thinking mode"
            )
        )
        thinking_level = ask_gemini_thinking_config()
    elif provider_lower == "openai":
        console.print(
            create_question_box(
                "Step 8: Reasoning Effort",
                "Configure OpenAI reasoning effort level"
            )
        )
        reasoning_effort = ask_openai_reasoning_effort()
    elif provider_lower == "anthropic":
        console.print(
            create_question_box(
                "Step 8: Effort Level",
                "Configure Claude effort level"
            )
        )
        anthropic_effort = ask_anthropic_effort()

    return {
        "ticker": selected_ticker,
        "analysis_date": analysis_date,
        "analysts": selected_analysts,
        "research_depth": selected_research_depth,
        "llm_provider": selected_llm_provider.lower(),
        "backend_url": backend_url,
        "shallow_thinker": selected_shallow_thinker,
        "deep_thinker": selected_deep_thinker,
        "google_thinking_level": thinking_level,
        "openai_reasoning_effort": reasoning_effort,
        "anthropic_effort": anthropic_effort,
        "output_language": output_language,
    }


def get_ticker():
    """Get ticker symbol from user input."""
    return typer.prompt("", default="SPY")


def get_analysis_date():
    """Get the analysis date from user input."""
    while True:
        date_str = typer.prompt(
            "", default=datetime.datetime.now().strftime("%Y-%m-%d")
        )
        try:
            # Validate date format and ensure it's not in the future
            analysis_date = datetime.datetime.strptime(date_str, "%Y-%m-%d")
            if analysis_date.date() > datetime.datetime.now().date():
                console.print("[red]Error: Analysis date cannot be in the future[/red]")
                continue
            return date_str
        except ValueError:
            console.print(
                "[red]Error: Invalid date format. Please use YYYY-MM-DD[/red]"
            )


def save_report_to_disk(final_state, ticker: str, save_path: Path):
    """Save complete analysis report to disk with organized subfolders."""
    save_path.mkdir(parents=True, exist_ok=True)
    sections = []

    # 1. Analysts
    analysts_dir = save_path / "1_analysts"
    analyst_parts = []
    if final_state.get("market_report"):
        analysts_dir.mkdir(exist_ok=True)
        (analysts_dir / "market.md").write_text(final_state["market_report"], encoding="utf-8")
        analyst_parts.append(("Market Analyst", final_state["market_report"]))
    if final_state.get("sentiment_report"):
        analysts_dir.mkdir(exist_ok=True)
        (analysts_dir / "sentiment.md").write_text(final_state["sentiment_report"], encoding="utf-8")
        analyst_parts.append(("Social Analyst", final_state["sentiment_report"]))
    if final_state.get("news_report"):
        analysts_dir.mkdir(exist_ok=True)
        (analysts_dir / "news.md").write_text(final_state["news_report"], encoding="utf-8")
        analyst_parts.append(("News Analyst", final_state["news_report"]))
    if final_state.get("fundamentals_report"):
        analysts_dir.mkdir(exist_ok=True)
        (analysts_dir / "fundamentals.md").write_text(final_state["fundamentals_report"], encoding="utf-8")
        analyst_parts.append(("Fundamentals Analyst", final_state["fundamentals_report"]))
    if analyst_parts:
        content = "\n\n".join(f"### {name}\n{text}" for name, text in analyst_parts)
        sections.append(f"## I. Analyst Team Reports\n\n{content}")

    # 2. Research
    if final_state.get("investment_debate_state"):
        research_dir = save_path / "2_research"
        debate = final_state["investment_debate_state"]
        research_parts = []
        if debate.get("bull_history"):
            research_dir.mkdir(exist_ok=True)
            (research_dir / "bull.md").write_text(debate["bull_history"], encoding="utf-8")
            research_parts.append(("Bull Researcher", debate["bull_history"]))
        if debate.get("bear_history"):
            research_dir.mkdir(exist_ok=True)
            (research_dir / "bear.md").write_text(debate["bear_history"], encoding="utf-8")
            research_parts.append(("Bear Researcher", debate["bear_history"]))
        if debate.get("judge_decision"):
            research_dir.mkdir(exist_ok=True)
            (research_dir / "manager.md").write_text(debate["judge_decision"], encoding="utf-8")
            research_parts.append(("Research Manager", debate["judge_decision"]))
        if research_parts:
            content = "\n\n".join(f"### {name}\n{text}" for name, text in research_parts)
            sections.append(f"## II. Research Team Decision\n\n{content}")

    # 3. Trading
    if final_state.get("trader_investment_plan"):
        trading_dir = save_path / "3_trading"
        trading_dir.mkdir(exist_ok=True)
        (trading_dir / "trader.md").write_text(final_state["trader_investment_plan"], encoding="utf-8")
        sections.append(f"## III. Trading Team Plan\n\n### Trader\n{final_state['trader_investment_plan']}")

    # 4. Risk Management
    if final_state.get("risk_debate_state"):
        risk_dir = save_path / "4_risk"
        risk = final_state["risk_debate_state"]
        risk_parts = []
        if risk.get("aggressive_history"):
            risk_dir.mkdir(exist_ok=True)
            (risk_dir / "aggressive.md").write_text(risk["aggressive_history"], encoding="utf-8")
            risk_parts.append(("Aggressive Analyst", risk["aggressive_history"]))
        if risk.get("conservative_history"):
            risk_dir.mkdir(exist_ok=True)
            (risk_dir / "conservative.md").write_text(risk["conservative_history"], encoding="utf-8")
            risk_parts.append(("Conservative Analyst", risk["conservative_history"]))
        if risk.get("neutral_history"):
            risk_dir.mkdir(exist_ok=True)
            (risk_dir / "neutral.md").write_text(risk["neutral_history"], encoding="utf-8")
            risk_parts.append(("Neutral Analyst", risk["neutral_history"]))
        if risk_parts:
            content = "\n\n".join(f"### {name}\n{text}" for name, text in risk_parts)
            sections.append(f"## IV. Risk Management Team Decision\n\n{content}")

        # 5. Portfolio Manager
        if risk.get("judge_decision"):
            portfolio_dir = save_path / "5_portfolio"
            portfolio_dir.mkdir(exist_ok=True)
            (portfolio_dir / "decision.md").write_text(risk["judge_decision"], encoding="utf-8")
            sections.append(f"## V. Portfolio Manager Decision\n\n### Portfolio Manager\n{risk['judge_decision']}")

    # Write consolidated report
    header = f"# Trading Analysis Report: {ticker}\n\nGenerated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    (save_path / "complete_report.md").write_text(header + "\n\n".join(sections), encoding="utf-8")
    return save_path / "complete_report.md"


def display_complete_report(final_state):
    """Display the complete analysis report sequentially (avoids truncation)."""
    console.print()
    console.print(Rule("Complete Analysis Report", style="bold green"))

    # I. Analyst Team Reports
    analysts = []
    if final_state.get("market_report"):
        analysts.append(("Market Analyst", final_state["market_report"]))
    if final_state.get("sentiment_report"):
        analysts.append(("Social Analyst", final_state["sentiment_report"]))
    if final_state.get("news_report"):
        analysts.append(("News Analyst", final_state["news_report"]))
    if final_state.get("fundamentals_report"):
        analysts.append(("Fundamentals Analyst", final_state["fundamentals_report"]))
    if analysts:
        console.print(Panel("[bold]I. Analyst Team Reports[/bold]", border_style="cyan"))
        for title, content in analysts:
            console.print(Panel(Markdown(content), title=title, border_style="blue", padding=(1, 2)))

    # II. Research Team Reports
    if final_state.get("investment_debate_state"):
        debate = final_state["investment_debate_state"]
        research = []
        if debate.get("bull_history"):
            research.append(("Bull Researcher", debate["bull_history"]))
        if debate.get("bear_history"):
            research.append(("Bear Researcher", debate["bear_history"]))
        if debate.get("judge_decision"):
            research.append(("Research Manager", debate["judge_decision"]))
        if research:
            console.print(Panel("[bold]II. Research Team Decision[/bold]", border_style="magenta"))
            for title, content in research:
                console.print(Panel(Markdown(content), title=title, border_style="blue", padding=(1, 2)))

    # III. Trading Team
    if final_state.get("trader_investment_plan"):
        console.print(Panel("[bold]III. Trading Team Plan[/bold]", border_style="yellow"))
        console.print(Panel(Markdown(final_state["trader_investment_plan"]), title="Trader", border_style="blue", padding=(1, 2)))

    # IV. Risk Management Team
    if final_state.get("risk_debate_state"):
        risk = final_state["risk_debate_state"]
        risk_reports = []
        if risk.get("aggressive_history"):
            risk_reports.append(("Aggressive Analyst", risk["aggressive_history"]))
        if risk.get("conservative_history"):
            risk_reports.append(("Conservative Analyst", risk["conservative_history"]))
        if risk.get("neutral_history"):
            risk_reports.append(("Neutral Analyst", risk["neutral_history"]))
        if risk_reports:
            console.print(Panel("[bold]IV. Risk Management Team Decision[/bold]", border_style="red"))
            for title, content in risk_reports:
                console.print(Panel(Markdown(content), title=title, border_style="blue", padding=(1, 2)))

        # V. Portfolio Manager Decision
        if risk.get("judge_decision"):
            console.print(Panel("[bold]V. Portfolio Manager Decision[/bold]", border_style="green"))
            console.print(Panel(Markdown(risk["judge_decision"]), title="Portfolio Manager", border_style="blue", padding=(1, 2)))


def update_research_team_status(status):
    """Update status for research team members (not Trader)."""
    research_team = ["Bull Researcher", "Bear Researcher", "Research Manager"]
    for agent in research_team:
        message_buffer.update_agent_status(agent, status)


# Ordered list of analysts for status transitions
ANALYST_ORDER = ["market", "social", "news", "fundamentals"]
ANALYST_AGENT_NAMES = {
    "market": "Market Analyst",
    "social": "Social Analyst",
    "news": "News Analyst",
    "fundamentals": "Fundamentals Analyst",
}
ANALYST_REPORT_MAP = {
    "market": "market_report",
    "social": "sentiment_report",
    "news": "news_report",
    "fundamentals": "fundamentals_report",
}


def update_analyst_statuses(message_buffer, chunk):
    """Update analyst statuses based on accumulated report state.

    Logic:
    - Store new report content from the current chunk if present
    - Check accumulated report_sections (not just current chunk) for status
    - Analysts with reports = completed
    - First analyst without report = in_progress
    - Remaining analysts without reports = pending
    - When all analysts done, set Bull Researcher to in_progress
    """
    selected = message_buffer.selected_analysts
    found_active = False

    for analyst_key in ANALYST_ORDER:
        if analyst_key not in selected:
            continue

        agent_name = ANALYST_AGENT_NAMES[analyst_key]
        report_key = ANALYST_REPORT_MAP[analyst_key]

        # Capture new report content from current chunk
        if chunk.get(report_key):
            message_buffer.update_report_section(report_key, chunk[report_key])

        # Determine status from accumulated sections, not just current chunk
        has_report = bool(message_buffer.report_sections.get(report_key))

        if has_report:
            message_buffer.update_agent_status(agent_name, "completed")
        elif not found_active:
            message_buffer.update_agent_status(agent_name, "in_progress")
            found_active = True
        else:
            message_buffer.update_agent_status(agent_name, "pending")

    # When all analysts complete, transition research team to in_progress
    if not found_active and selected:
        if message_buffer.agent_status.get("Bull Researcher") == "pending":
            message_buffer.update_agent_status("Bull Researcher", "in_progress")

def extract_content_string(content):
    """Extract string content from various message formats.
    Returns None if no meaningful text content is found.
    """
    import ast

    def is_empty(val):
        """Check if value is empty using Python's truthiness."""
        if val is None or val == '':
            return True
        if isinstance(val, str):
            s = val.strip()
            if not s:
                return True
            try:
                return not bool(ast.literal_eval(s))
            except (ValueError, SyntaxError):
                return False  # Can't parse = real text
        return not bool(val)

    if is_empty(content):
        return None

    if isinstance(content, str):
        return content.strip()

    if isinstance(content, dict):
        text = content.get('text', '')
        return text.strip() if not is_empty(text) else None

    if isinstance(content, list):
        text_parts = [
            item.get('text', '').strip() if isinstance(item, dict) and item.get('type') == 'text'
            else (item.strip() if isinstance(item, str) else '')
            for item in content
        ]
        result = ' '.join(t for t in text_parts if t and not is_empty(t))
        return result if result else None

    return str(content).strip() if not is_empty(content) else None


def classify_message_type(message) -> tuple[str, str | None]:
    """Classify LangChain message into display type and extract content.

    Returns:
        (type, content) - type is one of: User, Agent, Data, Control
                        - content is extracted string or None
    """
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    content = extract_content_string(getattr(message, 'content', None))

    if isinstance(message, HumanMessage):
        if content and content.strip() == "Continue":
            return ("Control", content)
        return ("User", content)

    if isinstance(message, ToolMessage):
        return ("Data", content)

    if isinstance(message, AIMessage):
        return ("Agent", content)

    # Fallback for unknown types
    return ("System", content)


def format_tool_args(args, max_length=80) -> str:
    """Format tool arguments for terminal display."""
    result = str(args)
    if len(result) > max_length:
        return result[:max_length - 3] + "..."
    return result

def run_analysis(checkpoint: bool = False):
    # First get all user selections
    selections = get_user_selections()

    # Create config with selected research depth — DB-backed defaults +
    # per-run user overrides (which we don't persist).
    config = _runtime_config()
    config["max_debate_rounds"] = selections["research_depth"]
    config["max_risk_discuss_rounds"] = selections["research_depth"]
    config["quick_think_llm"] = selections["shallow_thinker"]
    config["deep_think_llm"] = selections["deep_thinker"]
    config["backend_url"] = selections["backend_url"]
    config["llm_provider"] = selections["llm_provider"].lower()
    # Provider-specific thinking configuration
    config["google_thinking_level"] = selections.get("google_thinking_level")
    config["openai_reasoning_effort"] = selections.get("openai_reasoning_effort")
    config["anthropic_effort"] = selections.get("anthropic_effort")
    config["output_language"] = selections.get("output_language", "English")
    config["checkpoint_enabled"] = checkpoint

    # Create stats callback handler for tracking LLM/tool calls
    stats_handler = StatsCallbackHandler()

    # Normalize analyst selection to predefined order (selection is a 'set', order is fixed)
    selected_set = {analyst.value for analyst in selections["analysts"]}
    selected_analyst_keys = [a for a in ANALYST_ORDER if a in selected_set]

    # Initialize the graph with callbacks bound to LLMs
    graph = TradingAgentsGraph(
        selected_analyst_keys,
        config=config,
        debug=True,
        callbacks=[stats_handler],
    )

    # Initialize message buffer with selected analysts
    message_buffer.init_for_analysis(selected_analyst_keys)

    # Track start time for elapsed display
    start_time = time.time()

    # Create result directory
    results_dir = Path(config["results_dir"]) / selections["ticker"] / selections["analysis_date"]
    results_dir.mkdir(parents=True, exist_ok=True)
    report_dir = results_dir / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    log_file = results_dir / "message_tool.log"
    log_file.touch(exist_ok=True)

    def save_message_decorator(obj, func_name):
        func = getattr(obj, func_name)
        @wraps(func)
        def wrapper(*args, **kwargs):
            func(*args, **kwargs)
            timestamp, message_type, content = obj.messages[-1]
            content = content.replace("\n", " ")  # Replace newlines with spaces
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"{timestamp} [{message_type}] {content}\n")
        return wrapper
    
    def save_tool_call_decorator(obj, func_name):
        func = getattr(obj, func_name)
        @wraps(func)
        def wrapper(*args, **kwargs):
            func(*args, **kwargs)
            timestamp, tool_name, args = obj.tool_calls[-1]
            args_str = ", ".join(f"{k}={v}" for k, v in args.items())
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"{timestamp} [Tool Call] {tool_name}({args_str})\n")
        return wrapper

    def save_report_section_decorator(obj, func_name):
        func = getattr(obj, func_name)
        @wraps(func)
        def wrapper(section_name, content):
            func(section_name, content)
            if section_name in obj.report_sections and obj.report_sections[section_name] is not None:
                content = obj.report_sections[section_name]
                if content:
                    file_name = f"{section_name}.md"
                    text = "\n".join(str(item) for item in content) if isinstance(content, list) else content
                    with open(report_dir / file_name, "w", encoding="utf-8") as f:
                        f.write(text)
        return wrapper

    message_buffer.add_message = save_message_decorator(message_buffer, "add_message")
    message_buffer.add_tool_call = save_tool_call_decorator(message_buffer, "add_tool_call")
    message_buffer.update_report_section = save_report_section_decorator(message_buffer, "update_report_section")

    # Now start the display layout
    layout = create_layout()

    with Live(layout, refresh_per_second=4) as live:
        # Initial display
        update_display(layout, stats_handler=stats_handler, start_time=start_time)

        # Add initial messages
        message_buffer.add_message("System", f"Selected ticker: {selections['ticker']}")
        message_buffer.add_message(
            "System", f"Analysis date: {selections['analysis_date']}"
        )
        message_buffer.add_message(
            "System",
            f"Selected analysts: {', '.join(analyst.value for analyst in selections['analysts'])}",
        )
        update_display(layout, stats_handler=stats_handler, start_time=start_time)

        # Update agent status to in_progress for the first analyst
        first_analyst = f"{selections['analysts'][0].value.capitalize()} Analyst"
        message_buffer.update_agent_status(first_analyst, "in_progress")
        update_display(layout, stats_handler=stats_handler, start_time=start_time)

        # Create spinner text
        spinner_text = (
            f"Analyzing {selections['ticker']} on {selections['analysis_date']}..."
        )
        update_display(layout, spinner_text, stats_handler=stats_handler, start_time=start_time)

        # Initialize state and get graph args with callbacks
        init_agent_state = graph.propagator.create_initial_state(
            selections["ticker"], selections["analysis_date"]
        )
        # Pass callbacks to graph config for tool execution tracking
        # (LLM tracking is handled separately via LLM constructor)
        args = graph.propagator.get_graph_args(callbacks=[stats_handler])

        # Stream the analysis
        trace = []
        for chunk in graph.graph.stream(init_agent_state, **args):
            # Process all messages in chunk, deduplicating by message ID
            for message in chunk.get("messages", []):
                msg_id = getattr(message, "id", None)
                if msg_id is not None:
                    if msg_id in message_buffer._processed_message_ids:
                        continue
                    message_buffer._processed_message_ids.add(msg_id)

                msg_type, content = classify_message_type(message)
                if content and content.strip():
                    message_buffer.add_message(msg_type, content)

                if hasattr(message, "tool_calls") and message.tool_calls:
                    for tool_call in message.tool_calls:
                        if isinstance(tool_call, dict):
                            message_buffer.add_tool_call(tool_call["name"], tool_call["args"])
                        else:
                            message_buffer.add_tool_call(tool_call.name, tool_call.args)

            # Update analyst statuses based on report state (runs on every chunk)
            update_analyst_statuses(message_buffer, chunk)

            # Research Team - Handle Investment Debate State
            if chunk.get("investment_debate_state"):
                debate_state = chunk["investment_debate_state"]
                bull_hist = debate_state.get("bull_history", "").strip()
                bear_hist = debate_state.get("bear_history", "").strip()
                judge = debate_state.get("judge_decision", "").strip()

                # Only update status when there's actual content
                if bull_hist or bear_hist:
                    update_research_team_status("in_progress")
                if bull_hist:
                    message_buffer.update_report_section(
                        "investment_plan", f"### Bull Researcher Analysis\n{bull_hist}"
                    )
                if bear_hist:
                    message_buffer.update_report_section(
                        "investment_plan", f"### Bear Researcher Analysis\n{bear_hist}"
                    )
                if judge:
                    message_buffer.update_report_section(
                        "investment_plan", f"### Research Manager Decision\n{judge}"
                    )
                    update_research_team_status("completed")
                    message_buffer.update_agent_status("Trader", "in_progress")

            # Trading Team
            if chunk.get("trader_investment_plan"):
                message_buffer.update_report_section(
                    "trader_investment_plan", chunk["trader_investment_plan"]
                )
                if message_buffer.agent_status.get("Trader") != "completed":
                    message_buffer.update_agent_status("Trader", "completed")
                    message_buffer.update_agent_status("Aggressive Analyst", "in_progress")

            # Risk Management Team - Handle Risk Debate State
            if chunk.get("risk_debate_state"):
                risk_state = chunk["risk_debate_state"]
                agg_hist = risk_state.get("aggressive_history", "").strip()
                con_hist = risk_state.get("conservative_history", "").strip()
                neu_hist = risk_state.get("neutral_history", "").strip()
                judge = risk_state.get("judge_decision", "").strip()

                if agg_hist:
                    if message_buffer.agent_status.get("Aggressive Analyst") != "completed":
                        message_buffer.update_agent_status("Aggressive Analyst", "in_progress")
                    message_buffer.update_report_section(
                        "final_trade_decision", f"### Aggressive Analyst Analysis\n{agg_hist}"
                    )
                if con_hist:
                    if message_buffer.agent_status.get("Conservative Analyst") != "completed":
                        message_buffer.update_agent_status("Conservative Analyst", "in_progress")
                    message_buffer.update_report_section(
                        "final_trade_decision", f"### Conservative Analyst Analysis\n{con_hist}"
                    )
                if neu_hist:
                    if message_buffer.agent_status.get("Neutral Analyst") != "completed":
                        message_buffer.update_agent_status("Neutral Analyst", "in_progress")
                    message_buffer.update_report_section(
                        "final_trade_decision", f"### Neutral Analyst Analysis\n{neu_hist}"
                    )
                if judge:
                    if message_buffer.agent_status.get("Portfolio Manager") != "completed":
                        message_buffer.update_agent_status("Portfolio Manager", "in_progress")
                        message_buffer.update_report_section(
                            "final_trade_decision", f"### Portfolio Manager Decision\n{judge}"
                        )
                        message_buffer.update_agent_status("Aggressive Analyst", "completed")
                        message_buffer.update_agent_status("Conservative Analyst", "completed")
                        message_buffer.update_agent_status("Neutral Analyst", "completed")
                        message_buffer.update_agent_status("Portfolio Manager", "completed")

            # Update the display
            update_display(layout, stats_handler=stats_handler, start_time=start_time)

            trace.append(chunk)

        # Get final state and decision
        final_state = trace[-1]
        decision = graph.process_signal(final_state["final_trade_decision"])

        # Update all agent statuses to completed
        for agent in message_buffer.agent_status:
            message_buffer.update_agent_status(agent, "completed")

        message_buffer.add_message(
            "System", f"Completed analysis for {selections['analysis_date']}"
        )

        # Update final report sections
        for section in message_buffer.report_sections.keys():
            if section in final_state:
                message_buffer.update_report_section(section, final_state[section])

        update_display(layout, stats_handler=stats_handler, start_time=start_time)

    # Post-analysis prompts (outside Live context for clean interaction)
    console.print("\n[bold cyan]Analysis Complete![/bold cyan]\n")

    # Prompt to save report
    save_choice = typer.prompt("Save report?", default="Y").strip().upper()
    if save_choice in ("Y", "YES", ""):
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        default_path = Path.cwd() / "reports" / f"{selections['ticker']}_{timestamp}"
        save_path_str = typer.prompt(
            "Save path (press Enter for default)",
            default=str(default_path)
        ).strip()
        save_path = Path(save_path_str)
        try:
            report_file = save_report_to_disk(final_state, selections["ticker"], save_path)
            console.print(f"\n[green]✓ Report saved to:[/green] {save_path.resolve()}")
            console.print(f"  [dim]Complete report:[/dim] {report_file.name}")
        except Exception as e:
            console.print(f"[red]Error saving report: {e}[/red]")

    # Prompt to display full report
    display_choice = typer.prompt("\nDisplay full report on screen?", default="Y").strip().upper()
    if display_choice in ("Y", "YES", ""):
        display_complete_report(final_state)


@app.command()
def analyze(
    checkpoint: bool = typer.Option(
        False,
        "--checkpoint",
        help="Enable checkpoint/resume: save state after each node so a crashed run can resume.",
    ),
    clear_checkpoints: bool = typer.Option(
        False,
        "--clear-checkpoints",
        help="Delete all saved checkpoints before running (force fresh start).",
    ),
):
    if clear_checkpoints:
        from tradingagents.graph.checkpointer import clear_all_checkpoints
        n = clear_all_checkpoints(_runtime_config()["data_cache_dir"])
        console.print(f"[yellow]Cleared {n} checkpoint(s).[/yellow]")
    run_analysis(checkpoint=checkpoint)


@app.command()
def pipeline(
    top_n: int = typer.Option(5, "--top-n", help="Number of stocks to analyze"),
    analyze_only: bool = typer.Option(False, "--analyze-only", help="Skip execution/monitoring"),
    skip_market_check: bool = typer.Option(False, "--skip-market-check", help="Skip market hours check"),
    poll_interval: int = typer.Option(600, "--poll-interval", help="Price poll interval in seconds (default: 600 = 10 min)"),
    date: Optional[str] = typer.Option(
        None, "--date",
        help="Analysis date YYYY-MM-DD for dry-run on a historical day (default: today IST). "
             "Auto-implies --analyze-only and --skip-market-check.",
    ),
):
    """Run the full intraday pipeline with live progress display.

    Phase 2 always analyzes stocks in parallel (one thread per stock).
    """
    from cli.pipeline_display import PipelineDisplay
    from cli.stats_handler import StatsCallbackHandler
    from tradingagents.screener import NSE_MIDCAP_SMALLCAP_UNIVERSE, ScreenFilters, Ranker
    from tradingagents.execution.paper_trader import PaperTrader
    from tradingagents.pipeline.plan_extractor import extract_trade_plan
    from tradingagents.pipeline.market_monitor import MarketMonitor
    from tradingagents.dataflows.indian_market import is_market_open, is_execution_window, IST
    from tradingagents.web.database import (
        init_db, insert_trade_plan, insert_debate, insert_agent_report, insert_daily_metrics,
    )

    # Validate date format and apply dry-run implications.
    if date:
        try:
            datetime.datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            console.print(f"[red]Invalid --date {date!r}: must be YYYY-MM-DD[/red]")
            return
        skip_market_check = True
        analyze_only = True

    init_db()

    if not skip_market_check and not is_market_open():
        console.print("[yellow]Market closed today (holiday or weekend). Exiting.[/yellow]")
        return

    display = PipelineDisplay()
    display.start()

    try:
        _run_pipeline_with_display(display, top_n, analyze_only, poll_interval, date)
    except KeyboardInterrupt:
        display.add_activity("[red]Pipeline interrupted by user[/red]")
    except Exception as e:
        display.add_activity(f"[red]Pipeline error: {e}[/red]")
        raise
    finally:
        display.stop()

    # Print final summary outside Live context
    console.print()
    console.print(Rule("Pipeline Complete", style="bold green"))
    if display.results:
        for r in display.results:
            console.print(f"  {r}")
    console.print()


def _run_pipeline_with_display(
    display,
    top_n: int,
    analyze_only: bool,
    poll_interval: int,
    date: Optional[str] = None,
):
    """Execute all pipeline phases with live display updates.

    ``date`` lets the caller pin analysis to a historical YYYY-MM-DD for
    dry-run replays. When None, defaults to today's date.
    """
    from cli.stats_handler import StatsCallbackHandler
    from tradingagents.screener import NSE_MIDCAP_SMALLCAP_UNIVERSE, ScreenFilters, Ranker
    from tradingagents.execution.paper_trader import PaperTrader
    from tradingagents.pipeline.plan_extractor import extract_trade_plan
    from tradingagents.pipeline.market_monitor import MarketMonitor
    from tradingagents.dataflows.indian_market import is_execution_window, IST
    from tradingagents.web.database import (
        init_db, insert_trade_plan, insert_debate, insert_agent_report, insert_daily_metrics,
        get_latest_capital,
    )

    date = date or datetime.datetime.now().strftime("%Y-%m-%d")
    stats_handler = StatsCallbackHandler()

    # ─── Phase 1: Screening ─────────────────────────────────────────────────
    display.set_phase(0, "in_progress", "screening")
    display.add_activity("Screening 80 stocks from NSE universe...")

    universe = NSE_MIDCAP_SMALLCAP_UNIVERSE[:80]
    filters = ScreenFilters()
    results = filters.screen(universe)
    passed = filters.get_passed(results)

    display.add_activity(f"Passed filters: {len(passed)}/{len(universe)} stocks")

    ranker = Ranker()
    ranked = ranker.rank(passed, top_n=top_n)

    if not ranked:
        display.set_phase(0, "done", f"0 stocks")
        display.add_activity("[red]No stocks passed screening. Exiting.[/red]")
        return

    display.set_phase(0, "done", f"{len(ranked)} stocks")
    display.set_stocks(ranked)
    display.add_activity(f"Top {len(ranked)} stocks selected for analysis")

    # ─── Phase 2: Multi-Agent Analysis (parallel, one thread per stock) ─────
    display.set_phase(1, "in_progress", f"0/{len(ranked)}")
    config = _runtime_config()

    actionable_plans = _run_parallel_analysis(
        display=display,
        ranked=ranked,
        config=config,
        date=date,
        stats_handler=stats_handler,
        insert_trade_plan=insert_trade_plan,
        insert_agent_report=insert_agent_report,
        insert_debate=insert_debate,
        extract_trade_plan=extract_trade_plan,
    )

    display.set_phase(1, "done", f"{len(actionable_plans)} actionable")

    # ─── Phase 3: Execution ──────────────────────────────────────────────────
    if analyze_only:
        display.set_phase(2, "skipped")
        display.set_phase(3, "skipped")
        display.set_phase(4, "skipped")
        display.add_activity("--analyze-only: skipping execution and monitoring.")
        return

    if not actionable_plans:
        display.set_phase(2, "skipped")
        display.set_phase(3, "skipped")
        display.set_phase(4, "done", "no trades")
        display.add_activity("No actionable plans (no Buy/Overweight).")
        return

    display.set_phase(2, "in_progress", f"{len(actionable_plans)} orders")
    # Capital persists across days: today's starting balance is yesterday's
    # end-of-day capital from the daily_metrics table. First-ever run falls
    # back to DEFAULT_CONFIG["initial_capital"].
    starting_capital = get_latest_capital(
        default=_runtime_config()["initial_capital"],
        before_date=date,
    )
    display.add_activity(f"Starting capital: ₹{starting_capital:.2f}")
    paper_trader = PaperTrader(initial_capital=starting_capital)

    order_ids = []
    for plan in actionable_plans:
        oid = paper_trader.place_trade_plan(plan)
        if oid:
            order_ids.append(oid)
            display.add_activity(
                f"Order {oid}: {plan['ticker']} entry={plan.get('entry_zone_low', 0):.0f}-{plan.get('entry_zone_high', 0):.0f}"
            )

    display.set_phase(2, "done", f"{len(order_ids)} placed")

    if not order_ids:
        display.set_phase(3, "skipped")
        display.set_phase(4, "done")
        display.add_activity("No orders placed.")
        return

    # ─── Phase 4: Monitoring ─────────────────────────────────────────────────
    from tradingagents.dataflows.indian_market import IST as ist_tz
    import pytz

    now = datetime.now(ist_tz)
    if not is_execution_window(now):
        display.set_phase(3, "skipped", f"outside window")
        display.add_activity(f"Outside execution window ({now.strftime('%H:%M')}). Skipping monitoring.")
    else:
        display.set_phase(3, "in_progress", "polling")
        display.add_activity(f"Market monitor started (poll every {poll_interval}s)")

        # Risk thresholds come from config so the user can tune without code edits.
        from tradingagents.execution.risk_manager import RiskThresholds
        risk_thresholds = RiskThresholds(
            breakeven_trigger_pct=config.get("breakeven_trigger_pct", 0.5),
            trail_trigger_pct=config.get("trail_trigger_pct", 1.0),
            trail_lock_pct=config.get("trail_lock_pct", 0.3),
        )

        # Optional: news-event monitor with fast K2.5 (thinking-disabled) classifier.
        # Falls back to no-op if disabled in config or if MOONSHOT_API_KEY missing.
        news_monitor_obj = None
        if config.get("news_check_enabled", True):
            try:
                from tradingagents.llm_clients.fast_classifier import FastClassifier
                from tradingagents.pipeline.news_monitor import NewsMonitor
                news_monitor_obj = NewsMonitor(
                    classifier=FastClassifier(),
                    lookback_min=config.get("news_check_lookback_min", 60),
                )
                display.add_activity(
                    f"News monitor enabled (lookback {config.get('news_check_lookback_min', 60)} min)"
                )
            except Exception as e:
                display.add_activity(f"[yellow]News monitor disabled: {e}[/yellow]")

        monitor = MarketMonitor(
            paper_trader,
            poll_interval_sec=poll_interval,
            risk_thresholds=risk_thresholds,
            news_monitor=news_monitor_obj,
        )
        # Run monitoring with display updates
        monitor._running = True
        while monitor._running:
            now = datetime.now(ist_tz)
            if not is_execution_window(now):
                monitor._hard_exit_all(now)
                display.add_activity("Execution window closed. Hard exit complete.")
                break

            monitor._poll_cycle(now)

            # Surface risk-manager SL raises in the activity log.
            for a in monitor._last_risk_actions:
                display.add_activity(
                    f"[bold yellow]Risk[/bold yellow] {a.ticker}: "
                    f"SL ₹{a.old_sl:.2f} → ₹{a.new_sl:.2f} "
                    f"({a.reason}, unrealized {a.unrealized_pct:+.2f}%)"
                )

            # Surface news-event monitor decisions. EXIT actions are flagged
            # in red since the position has already been force-closed by then.
            for n in monitor._last_news_actions:
                tag = (
                    "[bold red]News-EXIT[/bold red]"
                    if n.decision == "EXIT"
                    else "[bold cyan]News[/bold cyan]"
                )
                display.add_activity(f"{tag} {n.ticker}: {n.reason}")

            # Per-ticker mark-to-market line for the activity log.
            mtm_lines = []
            for ticker, pos in paper_trader.position_tracker.open_positions.items():
                price = monitor._last_prices.get(ticker)
                if price:
                    pct = ((price - pos.entry_price) / pos.entry_price) * 100
                    mtm_lines.append(
                        f"{ticker} @ ₹{price:.2f} ({pct:+.2f}%, SL=₹{pos.stop_loss:.2f})"
                    )
            if mtm_lines:
                display.add_activity("Poll: " + " | ".join(mtm_lines))

            # Mark-to-market portfolio value for the footer.
            metrics_now = paper_trader.position_tracker.get_metrics(monitor._last_prices)
            display.set_monitoring(
                list(monitor._get_tracked_tickers()),
                metrics_now["current_capital"],
                len(paper_trader.position_tracker.open_positions),
            )
            s = stats_handler.get_stats()
            display.update_stats(s["llm_calls"], s["tool_calls"], s["tokens_in"], s["tokens_out"])

            time.sleep(poll_interval)

        display.set_phase(3, "done")

    # ─── Phase 5: Reporting ──────────────────────────────────────────────────
    display.set_phase(4, "in_progress")
    metrics = paper_trader.get_state()["metrics"]

    insert_daily_metrics({
        "date": date,
        "capital": metrics["current_capital"],
        "daily_pnl": metrics["daily_pnl"],
        "daily_return_pct": metrics["total_return_pct"],
        "total_trades": metrics["total_trades"],
        "win_rate": metrics["win_rate"],
        "max_drawdown_pct": metrics["max_drawdown_pct"],
        "notes": f"Pipeline run: {metrics['total_trades']} trades, {metrics['winning_trades']} wins",
    })

    display.add_activity(
        f"Capital: ₹{metrics['current_capital']:.0f} | "
        f"P&L: ₹{metrics['daily_pnl']:.2f} | "
        f"Win rate: {metrics['win_rate']:.1f}%"
    )
    display.set_phase(4, "done")

    # ─── Phase 5.5: EOD reflection sweep ──────────────────────────────────
    if config.get("eod_reflection_enabled", True):
        try:
            from tradingagents.agents.utils.memory import TradingMemoryLog
            from tradingagents.llm_clients.fast_classifier import FastClassifier
            from tradingagents.pipeline.eod_reflection import run_eod_reflection_sweep

            display.add_activity("EOD reflection sweep starting...")
            memory_log = TradingMemoryLog(config)
            classifier = FastClassifier()
            results = run_eod_reflection_sweep(
                paper_trader=paper_trader,
                memory_log=memory_log,
                classifier=classifier,
                date=date,
                window_start=config.get("eod_news_window_start", "09:00"),
                window_end=config.get("eod_news_window_end", "15:30"),
            )
            for r in results:
                display.add_activity(
                    f"[bold magenta]Reflect[/bold magenta] {r.ticker}: "
                    f"{r.plan_adherence}"
                )
            display.add_activity(f"EOD reflection: {len(results)} trade(s) reflected.")
        except Exception as e:
            display.add_activity(f"[yellow]EOD reflection skipped: {e}[/yellow]")


def _run_parallel_analysis(
    display,
    ranked,
    config,
    date,
    stats_handler,
    insert_trade_plan,
    insert_agent_report,
    insert_debate,
    extract_trade_plan,
):
    """Phase 2: run all stocks concurrently in a ThreadPoolExecutor.

    One worker thread per stock, each owning its own TradingAgentsGraph
    instance. The display shows per-ticker progress in parallel-mode panel.
    SQLite writes are safe via WAL mode (see init_db).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading

    n = len(ranked)
    actionable_plans: list = []

    # Per-ticker live state shown in the parallel-mode agents panel.
    ticker_states: dict = {
        s["ticker"]: {"agent": "Market Analyst", "chunk_count": 0}
        for s in ranked
    }
    state_lock = threading.Lock()

    # Switch the agents panel to per-ticker rows; show all tickers as in-progress.
    display.set_parallel_mode(ticker_states)
    for stock in ranked:
        display.update_stock_status(stock["ticker"], "in_progress")
    display.add_activity(f"Spawning {n} parallel analysis threads...")

    def analyze_one(stock: dict):
        ticker = stock["ticker"]
        graph = TradingAgentsGraph(debug=True, config=config, callbacks=[stats_handler])
        past_context = graph.memory_log.get_past_context(ticker)
        init_state = graph.propagator.create_initial_state(ticker, date, past_context=past_context)
        args = graph.propagator.get_graph_args(callbacks=[stats_handler])

        trace = []
        for chunk in graph.graph.stream(init_state, **args):
            trace.append(chunk)
            with state_lock:
                ticker_states[ticker]["chunk_count"] += 1
                active = _infer_active_agent(chunk)
                if active:
                    ticker_states[ticker]["agent"] = active
                # Push the most recent debate/report snippet into the live
                # content panel, tagged with the ticker so the user can tell
                # which stock it's from.
                _route_chunk_content(display, ticker, chunk)
                s = stats_handler.get_stats()
                display.update_stats(s["llm_calls"], s["tool_calls"], s["tokens_in"], s["tokens_out"])
                display.update_parallel_progress(ticker_states)

        final_state = trace[-1]
        rating = graph.process_signal(final_state["final_trade_decision"])
        graph.memory_log.store_decision(
            ticker=ticker, trade_date=date,
            final_trade_decision=final_state["final_trade_decision"],
        )
        plan = extract_trade_plan(ticker, date, final_state, rating)

        # Persist the complete agent-team analysis as markdown — one folder
        # per ticker per day, regardless of Buy/Skip outcome.
        try:
            from tradingagents.pipeline.report_writer import save_daily_analysis
            save_daily_analysis(
                final_state=final_state,
                ticker=ticker,
                date=date,
                reports_dir=config.get("reports_dir"),
            )
        except Exception as e:
            # Display the failure but don't crash the analysis — DB save still
            # succeeds even if the markdown write hits an FS issue.
            display.add_activity(
                f"[yellow]Report save failed for {ticker}: {e}[/yellow]"
            )

        # Save to DB (thread-safe via WAL).
        insert_trade_plan(plan)
        for agent_type, key in (
            ("Market Analyst", "market_report"),
            ("Retail Sentiment", "sentiment_retail_report"),
            ("Institutional Sentiment", "sentiment_institutional_report"),
            ("Contrarian Sentiment", "sentiment_contrarian_report"),
            ("News Analyst", "news_report"),
            ("Fundamentals Analyst", "fundamentals_report"),
            ("Portfolio Manager", "final_trade_decision"),
        ):
            report = final_state.get(key, "")
            if report:
                insert_agent_report({
                    "date": date, "ticker": ticker,
                    "agent_type": agent_type, "report": report,
                })
        debate_state = final_state.get("investment_debate_state", {})
        if debate_state.get("bull_history") or debate_state.get("bear_history"):
            insert_debate({
                "date": date, "ticker": ticker, "round_num": 1,
                "bull_argument": debate_state.get("bull_history", ""),
                "bear_argument": debate_state.get("bear_history", ""),
                "verdict": debate_state.get("judge_decision", ""),
                "confidence": plan.get("confidence_score"),
            })
        return ticker, plan, rating

    completed = 0
    all_plans: list = []
    failed_tickers: list = []
    with ThreadPoolExecutor(max_workers=max(n, 1)) as ex:
        futures = {ex.submit(analyze_one, s): s["ticker"] for s in ranked}
        for fut in as_completed(futures):
            ticker = futures[fut]
            completed += 1
            try:
                t, plan, rating = fut.result()
                all_plans.append(plan)
                display.update_stock_status(t, "done")
            except Exception as e:
                failed_tickers.append(ticker)
                display.update_stock_status(ticker, "failed")
                display.add_result(ticker, f"[red]FAILED: {e}[/red]")
            display.set_phase(1, "in_progress", f"{completed}/{n}")

    # ─── Phase 2.5: Cross-stock allocator ─────────────────────────────────
    # Rank every analyzed plan by confidence × R:R, take top K, distribute
    # capital weighted by score. Force-promote the best Skip if everything
    # came back Skip — the desk always trades the best of the day.
    from tradingagents.pipeline.allocator import rank_and_allocate
    top_k = config.get("top_k_positions", 3)
    deploy_pct = config.get("deploy_pct_top_k", 70.0)
    cap = config.get("max_capital_per_stock_pct", 25.0)

    result = rank_and_allocate(
        all_plans, top_k=top_k, deploy_pct=deploy_pct, max_per_stock_pct=cap,
    )
    actionable_plans = result.traded

    # Render results: top-K traded with rank prefix, rest as saved-only.
    if result.promoted_from_skip:
        display.add_activity(
            "[yellow]Force-best-of-N: best Skip promoted to half-size Buy[/yellow]"
        )
    for p in result.traded:
        rank = p.get("rank_position", "?")
        size = p.get("position_size_pct", 0)
        conf = p.get("confidence_score") or 0
        score = p.get("rank_score", 0)
        eL = p.get("entry_zone_low") or 0
        eH = p.get("entry_zone_high") or 0
        sl = p.get("stop_loss") or 0
        promoted = " [yellow]promoted[/yellow]" if p.get("promoted_from_skip") else ""
        display.add_result(
            p["ticker"],
            f"[bold green]#{rank}[/bold green] Buy size={size:.1f}% "
            f"(entry={eL:.0f}-{eH:.0f}, SL={sl:.0f}, conf={conf}/10, score={score:.1f}){promoted}",
        )
    for p in result.saved_only:
        rank = p.get("rank_position", "?")
        conf = p.get("confidence_score") or 0
        display.add_result(
            p["ticker"],
            f"[dim]#{rank} ranked — saved, not traded (conf={conf}/10)[/dim]",
        )

    display.set_phase(1, "done", f"{len(actionable_plans)} traded of {len(all_plans)}")
    display.add_activity(f"[bold]Allocator: {result.summary}[/bold]")
    return actionable_plans


_AGENT_KEY_TO_NAME = (
    ("market_report", "Market Analyst"),
    ("sentiment_retail_report", "Retail Sentiment"),
    ("sentiment_institutional_report", "Institutional Sentiment"),
    ("sentiment_contrarian_report", "Contrarian Sentiment"),
    ("news_report", "News Analyst"),
    ("fundamentals_report", "Fundamentals Analyst"),
)


def _infer_active_agent(chunk: dict):
    """Return the agent that most recently produced output in this chunk.

    Used in parallel mode to show which stage each ticker is in. Order matters:
    later stages override earlier ones because chunks arrive incrementally and
    later state keys appear when the pipeline progresses.
    """
    # Risk debate / final decision overrides everything.
    if chunk.get("final_trade_decision"):
        return "Portfolio Manager"
    risk = chunk.get("risk_debate_state") or {}
    if risk.get("judge_decision"):
        return "Portfolio Manager"
    if risk.get("neutral_history"):
        return "Neutral Analyst"
    if risk.get("conservative_history"):
        return "Conservative Analyst"
    if risk.get("aggressive_history"):
        return "Aggressive Analyst"
    # Trader stage.
    if chunk.get("trader_investment_plan"):
        return "Trader"
    # Investment debate.
    debate = chunk.get("investment_debate_state") or {}
    if debate.get("judge_decision"):
        return "Research Manager"
    if debate.get("bear_history"):
        return "Bear Researcher"
    if debate.get("bull_history"):
        return "Bull Researcher"
    # Analyst phase: pick the latest report key present.
    latest = None
    for key, name in _AGENT_KEY_TO_NAME:
        if chunk.get(key):
            latest = name
    return latest


def _route_chunk_content(display, ticker: str, chunk: dict):
    """In parallel mode, emit a short tagged snippet to the Live Output panel
    when a chunk produces meaningful agent output. Tagged with the ticker so
    the user can tell which stock it's from."""
    debate = chunk.get("investment_debate_state") or {}
    risk = chunk.get("risk_debate_state") or {}

    if debate.get("judge_decision"):
        display.set_content_section(
            f"[{ticker}] Research Manager — Verdict",
            debate["judge_decision"].strip(),
        )
        return
    if debate.get("bear_history"):
        display.set_content_section(
            f"[{ticker}] Bear Researcher",
            debate["bear_history"].strip(),
        )
        return
    if debate.get("bull_history"):
        display.set_content_section(
            f"[{ticker}] Bull Researcher",
            debate["bull_history"].strip(),
        )
        return
    if chunk.get("trader_investment_plan"):
        display.set_content_section(
            f"[{ticker}] Trader — Plan",
            chunk["trader_investment_plan"].strip(),
        )
        return
    if risk.get("judge_decision"):
        display.set_content_section(
            f"[{ticker}] Portfolio Manager — Final",
            risk["judge_decision"].strip(),
        )
        return
    for key, name in _AGENT_KEY_TO_NAME:
        if chunk.get(key):
            lines = chunk[key].strip().split("\n")
            preview = "\n".join(l for l in lines if l.strip())[:400]
            display.set_content_section(
                f"[{ticker}] {name} — Report",
                preview,
            )
            return


def _update_agents_from_chunk(display, chunk: dict):
    """Update agent statuses and live content based on a graph stream chunk."""
    from langchain_core.messages import AIMessage, ToolMessage

    # --- Show tool calls and agent messages in live content ---
    for message in chunk.get("messages", []):
        # Tool calls from AI messages
        if isinstance(message, AIMessage) and hasattr(message, "tool_calls") and message.tool_calls:
            for tc in message.tool_calls:
                name = tc["name"] if isinstance(tc, dict) else tc.name
                args = tc["args"] if isinstance(tc, dict) else tc.args
                args_str = ", ".join(f"{k}={v}" for k, v in args.items()) if args else ""
                display.add_content(f"[yellow]Tool:[/yellow] {name}({args_str})")

        # AI agent text (truncated)
        if isinstance(message, AIMessage):
            content = message.content if isinstance(message.content, str) else ""
            if content.strip() and len(content) > 20:
                preview = content.strip().replace("\n", " ")[:100]
                display.add_content(f"[blue]Agent:[/blue] {preview}...")

    # --- Analyst reports: show summary snippet ---
    if chunk.get("market_report"):
        display.update_agent("Market Analyst", "completed")
        display.update_agent("Retail Sentiment", "in_progress")
        _show_report_preview(display, "Market Analyst", chunk["market_report"])

    if chunk.get("sentiment_retail_report"):
        display.update_agent("Retail Sentiment", "completed")
        display.update_agent("Institutional Sentiment", "in_progress")
        _show_report_preview(display, "Retail Sentiment", chunk["sentiment_retail_report"])

    if chunk.get("sentiment_institutional_report"):
        display.update_agent("Institutional Sentiment", "completed")
        display.update_agent("Contrarian Sentiment", "in_progress")
        _show_report_preview(display, "Institutional Sentiment", chunk["sentiment_institutional_report"])

    if chunk.get("sentiment_contrarian_report"):
        display.update_agent("Contrarian Sentiment", "completed")
        display.update_agent("News Analyst", "in_progress")
        _show_report_preview(display, "Contrarian Sentiment", chunk["sentiment_contrarian_report"])

    if chunk.get("news_report"):
        display.update_agent("News Analyst", "completed")
        display.update_agent("Fundamentals Analyst", "in_progress")
        _show_report_preview(display, "News Analyst", chunk["news_report"])

    if chunk.get("fundamentals_report"):
        display.update_agent("Fundamentals Analyst", "completed")
        display.update_agent("Bull Researcher", "in_progress")
        _show_report_preview(display, "Fundamentals Analyst", chunk["fundamentals_report"])

    # --- Investment debate: show arguments ---
    if chunk.get("investment_debate_state"):
        debate = chunk["investment_debate_state"]
        if debate.get("bull_history"):
            display.update_agent("Bull Researcher", "in_progress")
            bull_text = debate["bull_history"].strip()
            display.set_content_section(
                "Bull Researcher (Investment Debate)",
                bull_text,
            )
        if debate.get("bear_history"):
            display.update_agent("Bear Researcher", "in_progress")
            bear_text = debate["bear_history"].strip()
            display.set_content_section(
                "Bear Researcher (Investment Debate)",
                bear_text,
            )
        if debate.get("judge_decision"):
            display.update_agent("Bull Researcher", "completed")
            display.update_agent("Bear Researcher", "completed")
            display.update_agent("Research Manager", "completed")
            display.update_agent("Trader", "in_progress")
            judge_text = debate["judge_decision"].strip()
            display.set_content_section(
                "Research Manager — Verdict",
                judge_text,
            )

    # --- Trader ---
    if chunk.get("trader_investment_plan"):
        display.update_agent("Trader", "completed")
        display.update_agent("Aggressive Analyst", "in_progress")
        display.set_content_section(
            "Trader — Investment Plan",
            chunk["trader_investment_plan"].strip(),
        )

    # --- Risk debate: show each debater's argument ---
    if chunk.get("risk_debate_state"):
        risk = chunk["risk_debate_state"]
        if risk.get("aggressive_history"):
            display.update_agent("Aggressive Analyst", "in_progress")
            display.set_content_section(
                "Aggressive Analyst (Risk Debate)",
                risk["aggressive_history"].strip(),
            )
        if risk.get("conservative_history"):
            display.update_agent("Conservative Analyst", "in_progress")
            display.set_content_section(
                "Conservative Analyst (Risk Debate)",
                risk["conservative_history"].strip(),
            )
        if risk.get("neutral_history"):
            display.update_agent("Neutral Analyst", "in_progress")
            display.set_content_section(
                "Neutral Analyst (Risk Debate)",
                risk["neutral_history"].strip(),
            )
        if risk.get("judge_decision"):
            display.update_agent("Aggressive Analyst", "completed")
            display.update_agent("Conservative Analyst", "completed")
            display.update_agent("Neutral Analyst", "completed")
            display.update_agent("Portfolio Manager", "completed")
            display.set_content_section(
                "Portfolio Manager — Final Decision",
                risk["judge_decision"].strip(),
            )

    # --- Final decision ---
    if chunk.get("final_trade_decision"):
        display.update_agent("Portfolio Manager", "completed")
        display.set_content_section(
            "Portfolio Manager — Final Trade Decision",
            chunk["final_trade_decision"].strip(),
        )


def _show_report_preview(display, agent_name: str, report: str):
    """Show a brief preview of an analyst report in the live content panel."""
    lines = report.strip().split("\n")
    # Take first 3 non-empty lines as preview
    preview_lines = [l for l in lines if l.strip()][:3]
    preview = "\n".join(preview_lines)
    if len(lines) > 3:
        preview += f"\n[dim]... ({len(lines)} total lines)[/dim]"
    display.set_content_section(f"{agent_name} — Report Complete", preview)


if __name__ == "__main__":
    app()
