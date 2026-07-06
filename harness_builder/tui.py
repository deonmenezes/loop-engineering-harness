"""
harness TUI — Claude-Code-style interactive shell. Launch with bare `harness`.

Chat-first UX: type plain text and it runs against the active harness; slash
commands manage everything else. The input sits inside a tight double-rule
frame (rule above, rule below, status line under that), redrawn from the live
terminal width so it survives resizes. /prd compiles a pasted PRD into a
build brief the architect consumes.
"""

from __future__ import annotations

import random
import shutil
import time
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.application.current import get_app
from prompt_toolkit.completion import NestedCompleter
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from .core.spec import HarnessSpec
from .export import EXPORTERS, export

console = Console(highlight=False)

ACCENT = "#D97757"          # Claude terracotta
RULE_GREY = "#585858"

BANNER = rf"""[bold {ACCENT}]
  ██╗  ██╗ █████╗ ██████╗ ███╗   ██╗███████╗███████╗███████╗
  ██║  ██║██╔══██╗██╔══██╗████╗  ██║██╔════╝██╔════╝██╔════╝
  ███████║███████║██████╔╝██╔██╗ ██║█████╗  ███████╗███████╗
  ██╔══██║██╔══██║██╔══██╗██║╚██╗██║██╔══╝  ╚════██║╚════██║
  ██║  ██║██║  ██║██║  ██║██║ ╚████║███████╗███████║╚══════╝ [/]
"""

SPINNER_WORDS = [
    "Thinking", "Scheming", "Percolating", "Brewing", "Composing",
    "Orchestrating", "Wrangling", "Assembling", "Conjuring", "Marinating",
    "Plotting", "Architecting",
]

HELP = """\
[bold]Chat[/]
  <plain text>              run it as a task on the active harness
  /<harness> <task>         activate and run a local harness by name
  /loop <task>              same, wrapped in the retry goal loop (eval gate)
  /goal <goal>              EXTERNAL loop: plan -> checklist -> one harness run
                            per step -> check off -> replan on failure (resumable)

[bold]Harnesses[/]
  /build <prompt>           architect a NEW standalone harness: own app + TUI,
                            zero deps, {{slot}} prompt anatomy in prompts/
  /prd \\[file]             paste (or load) a PRD — compiles it into a build
                            brief (architecture, agents, all four loops) and
                            builds the harness from it
  /templates                list bundled domain templates
  /use <name>               copy a template into ./harnesses and activate it
  /open <path>              activate an existing harness directory
  /list                     every harness in ./harnesses (launcher, pattern)
  /install <name>           put a harness's command on your PATH (~/.local/bin)
  /inspect                  show the active harness's team, pattern, gate

[bold]Config[/]
  /model <provider/model>   override every agent's model for this session
  /verify <shell cmd>       set a deterministic verifier for /loop (blank clears)
  /iterations <n>           max goal-loop iterations (default 3)

[bold]Interop[/]
  /export <target>          compile active harness for: claude-code | codex | opencode

[bold]Quality[/]
  /certify                  re-run the birth certificate on the active harness
                            (every /build ends with one automatically)
  /scorecard                pass-rate + gate scores per harness, over time

[bold]Misc[/]
  /traces                   list recent run traces
  /help    /quit

[dim]keys: enter send · tab command menu · ctrl+c clear input · ctrl+d quit[/]
"""

STYLE = Style.from_dict({
    "prompt": f"bold fg:{ACCENT}",
    "rule": f"fg:{RULE_GREY}",
    "placeholder": "fg:#6c6c6c italic",
    # the frame's bottom rule + status live in the toolbar: kill the default
    # reverse-video bar so it reads as part of the frame, not a widget
    "bottom-toolbar": "noreverse bg:default fg:#8a8a8a",
    "completion-menu": "bg:#303030 fg:#d7d7d7",
    "completion-menu.completion.current": f"bg:{ACCENT} fg:#1c1c1c bold",
    "completion-menu.meta.completion": "bg:#262626 fg:#8a8a8a",
    "completion-menu.meta.completion.current": "bg:#3a3a3a fg:#d7d7d7",
    "scrollbar.background": "bg:#303030",
    "scrollbar.button": "bg:#585858",
})


def _width() -> int:
    try:
        return get_app().output.get_size().columns
    except Exception:
        return shutil.get_terminal_size((80, 24)).columns


def _keybindings() -> KeyBindings:
    kb = KeyBindings()

    @kb.add("c-c")
    def _(event):
        """Claude-style ctrl+c: clear the input first; exit only when empty."""
        buf = event.app.current_buffer
        if buf.text:
            buf.reset()
        else:
            event.app.exit(exception=KeyboardInterrupt)

    @kb.add("/")
    def _(event):
        """Typing / on an empty line pops the command menu, like Claude Code."""
        buf = event.app.current_buffer
        buf.insert_text("/")
        if buf.text == "/":
            buf.start_completion(select_first=False)

    return kb


class Shell:
    def __init__(self):
        self.harness_dir: Path | None = None
        self.spec: HarnessSpec | None = None
        self.model_override: str | None = None
        self.verify_cmd: str | None = None
        self.max_iterations = 3
        histfile = Path.home() / ".harness_history"
        self.session = PromptSession(
            message=self._prompt_fragments,
            history=FileHistory(str(histfile)),
            completer=self._completer(),
            complete_while_typing=False,   # keeps the frame tight; menu on tab or /
            reserve_space_for_menu=6,
            key_bindings=_keybindings(),
            placeholder=FormattedText([(
                "class:placeholder",
                'a task for the active harness · /build <idea> · /prd — /help')]),
            prompt_continuation=lambda width, ln, wrap: [("class:rule", "│ ")],
            bottom_toolbar=self._toolbar,
            style=STYLE,
        )
        self.prd_session: PromptSession | None = None

    # ----------------------------------------------------------- the frame
    def _prompt_fragments(self):
        """Top rule + prompt char. Re-evaluated every repaint => resize-safe."""
        return [("class:rule", "─" * _width() + "\n"), ("class:prompt", "❯ ")]

    def _toolbar(self):
        """Bottom rule + status line, directly under the input."""
        w = _width()
        left = " " + self._status()
        right = "/help · tab menu · ctrl+d quit "
        pad = w - len(left) - len(right)
        frags = [("class:rule", "─" * w), ("", "\n"), ("", left)]
        if pad > 1:
            frags += [("", " " * pad), ("class:rule", right)]
        return frags

    def _status(self) -> str:
        h = self.spec.name if self.spec else "no harness"
        bits = [f"⛭ {h}", self.model_override or "per-agent models"]
        if self.verify_cmd:
            bits.append("verify ✓")
        return " · ".join(bits)

    def _working(self, label: str | None = None):
        word = label or random.choice(SPINNER_WORDS)
        return console.status(
            f"[bold {ACCENT}]✻[/] [bold]{word}…[/] [dim](ctrl+c to interrupt)[/]",
            spinner="dots", spinner_style=ACCENT)

    def _reply(self, body, title: str = "reply"):
        console.print()
        console.print(Panel(body, title=f"[{ACCENT}]⏺[/] {title}",
                            title_align="left", border_style="grey37",
                            padding=(0, 1)))

    def _stream_run(self, run_fn, title: str = "reply") -> str:
        """Run run_fn(on_token) inside a Live panel that streams the current
        agent's tokens; the panel becomes the final reply. Agent progress
        lines print above it (Live redirects stdout)."""
        state = {"agent": "", "text": ""}
        live = Live(console=console, refresh_per_second=8, transient=False)

        def on_token(agent: str, delta: str):
            if agent != state["agent"]:
                state["agent"], state["text"] = agent, ""
            state["text"] += delta
            live.update(Panel(
                Markdown(state["text"][-3000:]),
                title=f"[{ACCENT}]⏺[/] {agent} [dim]streaming…[/]",
                title_align="left", border_style="grey37", padding=(0, 1)))

        with live:
            reply = run_fn(on_token)
            live.update(Panel(Markdown(reply), title=f"[{ACCENT}]⏺[/] {title}",
                              title_align="left", border_style="grey37",
                              padding=(0, 1)))
        return reply

    # ------------------------------------------------------------- infra
    def _completer(self):
        tdir = Path(__file__).resolve().parent.parent / "templates"
        templates = {d.name: None for d in tdir.iterdir()
                     if (d / "harness.yaml").exists()} if tdir.exists() else {}
        local = {str(p.parent): None for p in Path("harnesses").glob("*/harness.yaml")} \
            if Path("harnesses").exists() else {}
        local_commands = {f"/{Path(path).name}": None for path in local}
        return NestedCompleter.from_nested_dict({
            **local_commands,
            "/build": None, "/prd": None, "/templates": None, "/use": templates,
            "/open": local or None, "/list": None, "/inspect": None,
            "/install": {Path(p).name: None for p in local} or None,
            "/loop": None, "/model": {
                "anthropic/claude-sonnet-4-6": None,
                "anthropic/claude-haiku-4-5-20251001": None,
                "codex/gpt-5.5": None,
                "openai/gpt-4o": None, "groq/llama-3.3-70b-versatile": None,
                "ollama/llama3.1": None},
            "/goal": None, "/verify": None, "/iterations": None,
            "/certify": None, "/scorecard": None,
            "/export": {t: None for t in EXPORTERS},
            "/traces": None, "/help": None, "/quit": None,
        })

    def _refresh_completer(self):
        if getattr(self, "session", None):
            self.session.completer = self._completer()

    def _need_harness(self) -> bool:
        if self.spec:
            return True
        console.print("[yellow]no active harness — /use <template>, /open <path>, "
                      "/build <prompt>, or /prd first[/]")
        return False

    def _activate(self, path: Path):
        self.spec = HarnessSpec.load(path)
        self.harness_dir = path if path.is_dir() else path.parent
        if self.model_override:
            for a in self.spec.agents:
                a.model = self.model_override
        try:
            from .core.registry import register
            register(self.spec.name, self.harness_dir,
                     command=self.spec.command, pattern=self.spec.pattern,
                     description=self.spec.description)
        except Exception:
            pass
        console.print(f"[green]✓ active:[/] [bold]{self.spec.name}[/] "
                      f"({self.spec.pattern}, {len(self.spec.agents)} agents)")

    # ----------------------------------------------------------- actions
    def do_build(self, prompt: str):
        if not prompt:
            console.print("[yellow]usage: /build <domain description> "
                          "(or /prd to start from a full spec)[/]")
            return
        from .builder.architect import build_harness
        kwargs = {}
        if self.model_override:
            kwargs = {"architect_model": self.model_override,
                      "default_model": self.model_override}
        with self._working("Architecting"):
            harness_dir = build_harness(prompt, **kwargs)
        self._activate(harness_dir)
        self._refresh_completer()
        self._show_banner(self.spec.command or harness_dir.name,
                          self.spec.accent)
        console.print(Panel(
            f"[bold]{harness_dir.name}[/] is a standalone app — it runs "
            f"without harness-builder:\n\n"
            f"  cd {harness_dir} && ./{self.spec.command or harness_dir.name}\n\n"
            f"[dim]prompts/ANATOMY.md maps every {{{{slot}}}} · edit "
            f"prompts/*.md + skills/*.md freely · /prompts inside its TUI "
            f"shows the anatomy[/]",
            title="standalone harness created", border_style="green"))
        self.do_certify("")
        return harness_dir

    def _show_banner(self, text: str, accent: str = ""):
        from .builder.banner import big_banner
        rows = big_banner(text)
        if not rows:
            return
        color = accent if accent and accent.startswith("#") else ACCENT
        console.print()
        for r in rows:
            console.print(f"  [bold {color}]{r}[/]")
        console.print()

    def do_certify(self, _):
        """Birth certificate: one smoke run + judge against the harness's
        own quality gate; verdict stamped into BIRTH.md."""
        if not self._need_harness():
            return
        from .builder.verify import birth_certificate
        console.rule("birth certificate — one smoke run through the new team",
                     style=RULE_GREY)
        out: dict = {}
        try:
            def run(on_token):
                out.update(birth_certificate(self.spec, self.harness_dir,
                                             on_token=on_token))
                return out["reply"]
            self._stream_run(run, title="smoke deliverable")
        except Exception as e:
            console.print(f"[yellow]⚠ verification could not run:[/] "
                          f"{type(e).__name__}: {e}")
            console.print("[dim]the harness is built — verify later "
                          "with /certify[/]")
            return
        mark = "[green]PASSED[/]" if out["passed"] else "[red]DID NOT PASS[/]"
        console.print(f"[{ACCENT}]⏺[/] birth certificate: "
                      f"[bold]{out['score']:.1f}/10[/] {mark} "
                      f"[dim](gate {self.spec.eval.pass_threshold} · "
                      f"stamped into BIRTH.md)[/]")
        if not out["passed"] and out.get("diagnosis"):
            console.print(f"[dim]judge: {out['diagnosis'][:300]}[/]")

    def do_prd(self, arg: str):
        """PRD -> build brief (architecture + loops) -> confirm -> build."""
        text = ""
        arg = arg.strip()
        if arg and Path(arg).expanduser().is_file():
            text = Path(arg).expanduser().read_text()
            console.print(f"[dim]loaded PRD from {arg} "
                          f"({len(text.splitlines())} lines)[/]")
        elif arg:
            text = arg
        else:
            console.print(Panel(
                "Paste your PRD below — rough notes, user stories, a full "
                "spec, anything. It gets compiled into a [bold]build brief[/]: "
                "the architecture (pattern + agent seats + tools) and all four "
                "loops (design, review, eval-gated retry, goal checklist) in "
                "the exact language the architect consumes.\n\n"
                f"[dim]finish with esc+enter (or alt+enter) · ctrl+c cancels · "
                f"/prd <file.md> loads from disk[/]",
                title=f"[{ACCENT}]⏺[/] /prd", title_align="left",
                border_style="grey37", padding=(0, 1)))
            if self.prd_session is None:
                self.prd_session = PromptSession(
                    multiline=True, style=STYLE,
                    prompt_continuation=lambda w, ln, wrap: [("class:rule", "│ ")],
                    bottom_toolbar=lambda: [
                        ("class:rule", "─" * _width()), ("", "\n"),
                        ("", " esc+enter finish · ctrl+c cancel")])
            try:
                text = self.prd_session.prompt(
                    [("class:rule", "─" * _width() + "\n"),
                     ("class:prompt", "prd ❯ ")])
            except (KeyboardInterrupt, EOFError):
                console.print("[dim]prd cancelled[/]")
                return
        if not text.strip():
            console.print("[dim]empty PRD — nothing to do[/]")
            return

        # the paste is precious — put it on disk BEFORE anything can fail
        briefs = Path("workspace") / "briefs"
        briefs.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        if arg and Path(arg).expanduser().is_file():
            prd_file = Path(arg).expanduser()
        else:
            prd_file = briefs / f"prd_{stamp}.md"
            prd_file.write_text(text)
            console.print(f"[dim]PRD saved -> {prd_file}[/]")

        from .builder.architect import compile_prd
        model = self.model_override or "anthropic/claude-sonnet-4-6"
        try:
            with self._working("Compiling PRD"):
                brief = compile_prd(text, model=model)
        except Exception as e:
            console.print(f"[red]✗ PRD compile failed:[/] {type(e).__name__}: {e}")
            console.print(f"[dim]your PRD is safe — retry with:  /prd {prd_file}[/]")
            return
        self._reply(Markdown(brief), title="build brief")

        brief_file = briefs / f"brief_{stamp}.md"
        brief_file.write_text(f"{brief}\n\n---\n## SOURCE PRD\n\n{text}\n")
        console.print(f"[dim]brief saved -> {brief_file}[/]")

        try:
            ans = console.input(
                f"[bold {ACCENT}]build this harness now?[/] [dim][Y/n/e(dit)][/] "
            ).strip().lower()
        except (KeyboardInterrupt, EOFError):
            ans = "n"
        if ans in ("e", "edit"):
            console.print(f"[dim]edit {brief_file}, then run:  "
                          f"/prd {brief_file}[/]")
            return
        if ans not in ("", "y", "yes"):
            console.print(f"[dim]skipped — build later with:  /prd {brief_file}[/]")
            return
        harness_dir = self.do_build(brief)
        if harness_dir:
            (Path(harness_dir) / "BRIEF.md").write_text(brief_file.read_text())

    def do_templates(self, _):
        tdir = Path(__file__).resolve().parent.parent / "templates"
        table = Table(title="bundled templates", border_style="dim")
        table.add_column("name", style="bold")
        table.add_column("pattern", style="cyan")
        table.add_column("description")
        import yaml
        for d in sorted(tdir.iterdir()):
            f = d / "harness.yaml"
            if f.exists():
                meta = yaml.safe_load(f.read_text())
                table.add_row(d.name, meta["pattern"], meta["description"])
        console.print(table)

    def do_use(self, name: str):
        src = Path(__file__).resolve().parent.parent / "templates" / name
        if not (src / "harness.yaml").exists():
            console.print(f"[red]no template '{name}'[/] — try /templates")
            return
        dst = Path("harnesses") / name
        if not dst.exists():
            shutil.copytree(src, dst)
            (dst / "memory").mkdir(exist_ok=True)
            from .builder.scaffold import scaffold
            scaffold(HarnessSpec.load(dst), dst)
            console.print(f"[dim]copied template -> {dst} "
                          f"(standalone app included: cd {dst} && ./{name})[/]")
        self._activate(dst)
        self._refresh_completer()

    def do_open(self, path: str):
        p = Path(path)
        if not (p / "harness.yaml").exists() and not p.name == "harness.yaml":
            console.print(f"[red]{path} is not a harness directory[/]")
            return
        self._activate(p)

    def do_list(self, _):
        import yaml
        from .core.registry import entries
        rows: dict[str, tuple] = {}          # resolved path -> row
        found = sorted(Path("harnesses").glob("*/harness.yaml")) \
            if Path("harnesses").exists() else []
        for f in found:
            try:
                meta = yaml.safe_load(f.read_text()) or {}
            except Exception:
                meta = {}
            name = f.parent.name
            cmd = meta.get("command") or name
            rows[str(f.parent.resolve())] = (
                f"/{name}", f"./{f.parent}/{cmd}", meta.get("pattern", "?"),
                " ".join((meta.get("description") or "").split())[:110])
        for e in entries():              # fleet-wide, from ~/.harness/registry
            if e["path"] not in rows:
                rows[e["path"]] = (
                    f"[dim]{e['name']}[/]", f"[dim]/open {e['path']}[/]",
                    e.get("pattern", "?"),
                    (e.get("description") or "")[:110])
        if not rows:
            console.print("[dim]no harnesses yet — /build, /prd, or /use one[/]")
            return
        t = Table(title="your harnesses (cwd + global registry)",
                  border_style="dim")
        t.add_column("run", style="bold")
        t.add_column("launch", style="cyan")
        t.add_column("pattern")
        t.add_column("description", style="dim")
        for row in rows.values():
            t.add_row(*row)
        console.print(t)
        console.print("[dim]/<name> <task> runs a local one · /open <path> "
                      "activates a global one · /install <name> puts its "
                      "command on your PATH[/]")

    def do_scorecard(self, _):
        import json as _json
        ledger = Path("traces") / "scores.jsonl"
        if not ledger.exists():
            console.print("[dim]no scores yet — birth certificates and /loop "
                          "runs write traces/scores.jsonl[/]")
            return
        per: dict[str, dict] = {}
        for line in ledger.read_text().splitlines():
            try:
                r = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            d = per.setdefault(r.get("harness", "?"),
                               {"n": 0, "passed": 0, "scores": [],
                                "birth": None, "last": ""})
            d["n"] += 1
            d["passed"] += bool(r.get("passed"))
            if isinstance(r.get("score"), (int, float)):
                d["scores"].append(float(r["score"]))
            if r.get("kind") == "birth":
                d["birth"] = r.get("score")
            d["last"] = str(r.get("ts", ""))[:16].replace("T", " ")
        t = Table(title="scorecard — quality-gate verdicts over time",
                  border_style="dim")
        for col in ("harness", "runs", "pass rate", "avg score", "birth",
                    "last gated run"):
            t.add_column(col, style="bold" if col == "harness" else None)
        for name, d in sorted(per.items()):
            rate = f"{100 * d['passed'] // d['n']}%"
            avg = (f"{sum(d['scores']) / len(d['scores']):.1f}"
                   if d["scores"] else "—")
            birth = f"{d['birth']:.1f}" if isinstance(
                d["birth"], (int, float)) else "—"
            t.add_row(name, str(d["n"]), rate, avg, birth, d["last"])
        console.print(t)
        console.print("[dim]improving a harness? /uploop inside it, then "
                      "watch this table move[/]")

    def do_install(self, name: str):
        if not name:
            console.print("[yellow]usage: /install <harness name> — see /list[/]")
            return
        script = Path("harnesses") / name / "install.sh"
        if not script.exists():
            console.print(f"[red]no install.sh under harnesses/{name}[/] — /list")
            return
        import subprocess
        r = subprocess.run(["bash", str(script)], capture_output=True, text=True)
        out = (r.stdout + r.stderr).strip()
        if out:
            console.print(out)
        if r.returncode == 0:
            console.print("[green]✓ installed — run it from any directory[/]")
        else:
            console.print(f"[red]✗ install failed (exit {r.returncode})[/]")

    def do_inspect(self, _):
        if not self._need_harness():
            return
        s = self.spec
        t = Table(border_style="dim", title=f"{s.name} · {s.pattern}"
                  + (f" · supervisor={s.supervisor}" if s.supervisor else ""))
        t.add_column("agent", style="bold")
        t.add_column("model", style="cyan")
        t.add_column("role")
        t.add_column("tools", style="dim")
        for a in s.agents:
            t.add_row(a.name, a.model, a.role, ",".join(a.tools) or "—")
        console.print(t)
        console.print("[bold]gate[/] score ≥ "
                      f"{s.eval.pass_threshold} on:")
        for c in s.eval.quality_criteria:
            console.print(f"  [dim]-[/] {c}")

    def do_model(self, model: str):
        if not model:
            self.model_override = None
            console.print("[green]✓ cleared — agents use their own models[/]")
            return
        self.model_override = model
        if self.spec:
            for a in self.spec.agents:
                a.model = model
        console.print(f"[green]✓ every agent -> {model}[/]")

    def do_verify(self, cmd: str):
        self.verify_cmd = cmd or None
        console.print(f"[green]✓ verifier:[/] {cmd or '[dim]LLM-as-judge[/]'}")

    def do_iterations(self, n: str):
        try:
            self.max_iterations = max(1, int(n))
            console.print(f"[green]✓ max goal-loop iterations: {self.max_iterations}[/]")
        except ValueError:
            console.print("[yellow]usage: /iterations <n>[/]")

    def do_export(self, target: str):
        if not self._need_harness():
            return
        if target not in EXPORTERS:
            console.print(f"[yellow]usage: /export <{'|'.join(EXPORTERS)}>[/]")
            return
        out = export(self.spec, self.harness_dir, target)
        console.print(f"[green]✓ exported for {target} ->[/] {out}/")
        readme = out / "README.md"
        if readme.exists():
            console.print(Panel(Markdown(readme.read_text()), border_style="dim"))

    def do_traces(self, _):
        traces = sorted(Path("traces").glob("*.jsonl"))[-10:] \
            if Path("traces").exists() else []
        if not traces:
            console.print("[dim]no traces yet[/]")
        for t in traces:
            console.print(f"  {t}")

    def run_task(self, task: str, loop: bool):
        if not self._need_harness():
            return
        from .runtime.orchestrator import Orchestrator

        console.rule(f"[bold]{self.spec.name}[/] · {self.spec.pattern}"
                     + (" · goal loop" if loop else ""), style=RULE_GREY)
        try:
            if loop:
                from .core.ralph import run_goal_loop
                res = None

                def looped(on_token):
                    nonlocal res
                    res = run_goal_loop(
                        run_harness_fn=lambda t: Orchestrator(
                            self.spec, self.harness_dir).run(t, on_token=on_token),
                        spec=self.spec, task=task,
                        max_iterations=self.max_iterations,
                        verify_cmd=self.verify_cmd,
                        workspace=Path("workspace") / self.spec.name)
                    return res.final_output

                self._stream_run(looped)
                verdict = "[green]PASSED[/]" if res.passed \
                    else "[red]DID NOT PASS[/]"
                console.rule(f"goal loop {verdict} · {res.iterations} "
                             "iteration(s)", style=RULE_GREY)
            else:
                self._stream_run(lambda on_token: Orchestrator(
                    self.spec, self.harness_dir).run(task, on_token=on_token))
        except Exception as e:
            console.print(f"[red]✗ run failed:[/] {type(e).__name__}: {e}")

    def run_goal(self, goal: str):
        if not self._need_harness():
            return
        from .core.external_loop import run_external_loop
        from .runtime.orchestrator import Orchestrator

        def one_run(t: str) -> str:
            return Orchestrator(self.spec, self.harness_dir).run(t)

        console.rule(f"[bold]{self.spec.name}[/] · external loop", style=RULE_GREY)
        try:
            with self._working():
                state = run_external_loop(spec=self.spec,
                                          harness_dir=self.harness_dir,
                                          goal=goal, run_harness_fn=one_run)
        except Exception as e:
            console.print(f"[red]✗ loop failed:[/] {type(e).__name__}: {e}")
            return
        done = sum(s.status == "done" for s in state.steps)
        console.print(Panel(state.render(),
                            title=f"external loop · {done}/{len(state.steps)} done",
                            border_style="magenta"))

    # -------------------------------------------------------------- loop
    def _welcome(self):
        console.print(BANNER)
        from .core import auth
        detected = [f"{p} ({src})" for p, (ok, src) in auth.available().items()
                    if ok and p != "ollama"]
        auth_line = ", ".join(detected) if detected else \
            "[yellow]none — set ANTHROPIC_API_KEY, `claude setup-token`, " \
            "or use ollama/… models[/]"
        console.print(Panel(
            f"[bold]✻ Welcome to harness[/] [dim]— the team-architecture factory[/]\n\n"
            f"  [dim]cwd [/]  {Path.cwd()}\n"
            f"  [dim]auth[/]  {auth_line}\n"
            f"  [dim]try [/]  [bold]/prd[/] paste a spec · [bold]/build[/] "
            f"an idea · [bold]/templates[/]",
            border_style="grey37", padding=(0, 1), expand=False))

    def _dispatch(self, line: str):
        if not line.startswith("/"):
            self.run_task(line, loop=False)
            return True
        parts = line.split(maxsplit=1)
        cmd, arg = parts[0][1:], (parts[1] if len(parts) > 1 else "")
        if cmd in ("quit", "exit", "q"):
            return False
        if cmd == "help":
            console.print()
            console.print(HELP)
        elif cmd == "loop":
            self.run_task(arg, loop=True)
        elif cmd == "goal":
            self.run_goal(arg)
        elif fn := getattr(self, f"do_{cmd}", None):
            fn(arg)
        else:
            harness_dir = Path("harnesses") / cmd
            if (harness_dir / "harness.yaml").exists():
                self._activate(harness_dir)
                self.run_task(f"/{cmd}" + (f" {arg}" if arg else ""), loop=False)
            else:
                console.print(f"[yellow]unknown command /{cmd} — /help[/]")
        return True

    def repl(self):
        self._welcome()
        while True:
            try:
                line = self.session.prompt().strip()
            except (KeyboardInterrupt, EOFError):
                console.print("[dim]bye[/]")
                return
            if not line:
                continue
            try:
                if self._dispatch(line) is False:
                    console.print("[dim]bye[/]")
                    return
            except KeyboardInterrupt:
                console.print(f"\n[{ACCENT}]⏹ interrupted[/]")


def main():
    Shell().repl()


if __name__ == "__main__":
    main()
