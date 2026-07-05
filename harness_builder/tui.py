"""
harness TUI — opencode-style interactive shell. Launch with bare `harness`.

Chat-first UX like opencode: type plain text and it runs against the active
harness; slash commands manage everything else. Rich rendering, tab
completion, persistent history, live agent progress.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import NestedCompleter
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from .core.spec import HarnessSpec
from .export import EXPORTERS, export

console = Console(highlight=False)

BANNER = r"""[bold red]
  ██╗  ██╗ █████╗ ██████╗ ███╗   ██╗███████╗███████╗███████╗
  ██║  ██║██╔══██╗██╔══██╗████╗  ██║██╔════╝██╔════╝██╔════╝
  ███████║███████║██████╔╝██╔██╗ ██║█████╗  ███████╗███████╗
  ██╔══██║██╔══██║██╔══██╗██║╚██╗██║██╔══╝  ╚════██║╚════██║
  ██║  ██║██║  ██║██║  ██║██║ ╚████║███████╗███████║███████║
  ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═══╝╚══════╝╚══════╝╚══════╝[/]
[dim]        the team-architecture factory · type /help[/]
"""

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
  /templates                list bundled domain templates
  /use <name>               copy a template into ./harnesses and activate it
  /open <path>              activate an existing harness directory
  /list                     list harnesses in ./harnesses
  /inspect                  show the active harness's team, pattern, gate

[bold]Config[/]
  /model <provider/model>   override every agent's model for this session
  /verify <shell cmd>       set a deterministic verifier for /loop (blank clears)
  /iterations <n>           max goal-loop iterations (default 3)

[bold]Interop[/]
  /export <target>          compile active harness for: claude-code | codex | opencode

[bold]Misc[/]
  /traces                   list recent run traces
  /help    /quit
"""


class Shell:
    def __init__(self):
        self.harness_dir: Path | None = None
        self.spec: HarnessSpec | None = None
        self.model_override: str | None = None
        self.verify_cmd: str | None = None
        self.max_iterations = 3
        histfile = Path.home() / ".harness_history"
        self.session = PromptSession(
            history=FileHistory(str(histfile)),
            completer=self._completer(),
            style=Style.from_dict({"prompt": "bold ansired"}),
        )

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
            "/build": None, "/templates": None, "/use": templates,
            "/open": local or None, "/list": None, "/inspect": None,
            "/loop": None, "/model": {
                "anthropic/claude-sonnet-4-6": None,
                "anthropic/claude-haiku-4-5-20251001": None,
                "openai/gpt-4o": None, "groq/llama-3.3-70b-versatile": None,
                "ollama/llama3.1": None},
            "/goal": None, "/verify": None, "/iterations": None,
            "/export": {t: None for t in EXPORTERS},
            "/traces": None, "/help": None, "/quit": None,
        })

    def _refresh_completer(self):
        if getattr(self, "session", None):
            self.session.completer = self._completer()

    def _status(self) -> str:
        h = self.spec.name if self.spec else "none"
        m = self.model_override or "per-agent"
        bits = [f"harness:{h}", f"model:{m}"]
        if self.verify_cmd:
            bits.append("verify:cmd")
        return "  ".join(bits)

    def _need_harness(self) -> bool:
        if self.spec:
            return True
        console.print("[yellow]no active harness — /use <template>, /open <path>, "
                      "or /build <prompt> first[/]")
        return False

    def _activate(self, path: Path):
        self.spec = HarnessSpec.load(path)
        self.harness_dir = path if path.is_dir() else path.parent
        if self.model_override:
            for a in self.spec.agents:
                a.model = self.model_override
        console.print(f"[green]✓ active:[/] [bold]{self.spec.name}[/] "
                      f"({self.spec.pattern}, {len(self.spec.agents)} agents)")

    # ----------------------------------------------------------- actions
    def do_build(self, prompt: str):
        if not prompt:
            console.print("[yellow]usage: /build <domain description>[/]")
            return
        from .builder.architect import build_harness
        with console.status("[bold red]architect designing your harness…[/]"):
            harness_dir = build_harness(prompt)
        self._activate(harness_dir)
        self._refresh_completer()
        console.print(Panel(
            f"[bold]{harness_dir.name}[/] is a standalone app — it runs "
            f"without harness-builder:\n\n"
            f"  cd {harness_dir} && ./{harness_dir.name}\n\n"
            f"[dim]prompts/ANATOMY.md maps every {{{{slot}}}} · edit "
            f"prompts/*.md + skills/*.md freely · /prompts inside its TUI "
            f"shows the anatomy[/]",
            title="standalone harness created", border_style="green"))

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
        found = sorted(Path("harnesses").glob("*/harness.yaml")) \
            if Path("harnesses").exists() else []
        if not found:
            console.print("[dim]no harnesses in ./harnesses yet — /build or /use one[/]")
        for f in found:
            console.print(f"  {f.parent}")

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

        def one_run(t: str) -> str:
            return Orchestrator(self.spec, self.harness_dir).run(t)

        console.rule(f"[bold]{self.spec.name}[/] · {self.spec.pattern}"
                     + (" · goal loop" if loop else ""))
        try:
            if loop:
                from .core.ralph import run_goal_loop
                res = run_goal_loop(
                    run_harness_fn=one_run, spec=self.spec, task=task,
                    max_iterations=self.max_iterations,
                    verify_cmd=self.verify_cmd,
                    workspace=Path("workspace") / self.spec.name)
                verdict = "[green]PASSED[/]" if res.passed else "[red]DID NOT PASS[/]"
                console.rule(f"goal loop {verdict} · {res.iterations} iteration(s)")
                reply = res.final_output
            else:
                reply = one_run(task)
        except Exception as e:
            console.print(f"[red]run failed:[/] {type(e).__name__}: {e}")
            return
        console.print(Panel(Markdown(reply), title="reply", border_style="red"))

    def run_goal(self, goal: str):
        if not self._need_harness():
            return
        from .core.external_loop import run_external_loop
        from .runtime.orchestrator import Orchestrator

        def one_run(t: str) -> str:
            return Orchestrator(self.spec, self.harness_dir).run(t)

        console.rule(f"[bold]{self.spec.name}[/] · external loop")
        try:
            state = run_external_loop(spec=self.spec, harness_dir=self.harness_dir,
                                      goal=goal, run_harness_fn=one_run)
        except Exception as e:
            console.print(f"[red]loop failed:[/] {type(e).__name__}: {e}")
            return
        done = sum(s.status == "done" for s in state.steps)
        console.print(Panel(state.render(),
                            title=f"external loop · {done}/{len(state.steps)} done",
                            border_style="magenta"))

    # -------------------------------------------------------------- loop
    def repl(self):
        console.print(BANNER)
        if not any(os.environ.get(k) for k in
                   ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GROQ_API_KEY",
                    "OPENROUTER_API_KEY")):
            console.print("[yellow]⚠ no provider API keys in env — /build and "
                          "task runs will fail until you set one (.env)[/]\n")
        while True:
            try:
                line = self.session.prompt(
                    [("class:prompt", "❯ ")],
                    bottom_toolbar=lambda: f" {self._status()} ").strip()
            except (KeyboardInterrupt, EOFError):
                console.print("[dim]bye[/]")
                return
            if not line:
                continue
            if line.startswith("/"):
                parts = line.split(maxsplit=1)
                cmd, arg = parts[0][1:], (parts[1] if len(parts) > 1 else "")
                if cmd in ("quit", "exit", "q"):
                    console.print("[dim]bye[/]")
                    return
                if cmd == "help":
                    console.print(Panel(HELP, border_style="dim"))
                    continue
                if cmd == "loop":
                    self.run_task(arg, loop=True)
                    continue
                if cmd == "goal":
                    self.run_goal(arg)
                    continue
                fn = getattr(self, f"do_{cmd}", None)
                if fn:
                    fn(arg)
                else:
                    harness_dir = Path("harnesses") / cmd
                    if (harness_dir / "harness.yaml").exists():
                        self._activate(harness_dir)
                        self.run_task(f"/{cmd}" + (f" {arg}" if arg else ""),
                                      loop=False)
                    else:
                        console.print(f"[yellow]unknown command /{cmd} — /help[/]")
            else:
                self.run_task(line, loop=False)


def main():
    Shell().repl()


if __name__ == "__main__":
    main()
