"""
harness — CLI for the meta-factory.

  harness build "Build a harness for deep research. I need ..."
  harness run harnesses/deep_research --task "Investigate solid-state batteries"
  harness run harnesses/deep_research --task "..." --loop --max-iterations 3
  harness run harnesses/code_review   --task "review ./src" --loop --verify-cmd "pytest -q"
  harness templates
  harness use deep_research            # copy a bundled template into ./harnesses
  harness inspect harnesses/deep_research
  harness export harnesses/deep_research --to claude-code
  harness                              # no args: launch the interactive TUI
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


def cmd_build(args):
    from .builder.architect import build_harness
    build_harness(args.prompt, output_root=args.output,
                  architect_model=args.architect_model,
                  default_model=args.default_model)


def cmd_run(args):
    from .core.spec import HarnessSpec
    from .runtime.orchestrator import Orchestrator

    spec = HarnessSpec.load(args.harness)
    harness_dir = Path(args.harness) if Path(args.harness).is_dir() \
        else Path(args.harness).parent
    if args.model_override:
        for a in spec.agents:
            a.model = args.model_override
        spec.eval.judge_model = args.model_override
        spec.memory.summarizer_model = args.model_override

    def one_run(task_text: str) -> str:
        orch = Orchestrator(spec, harness_dir, workspace=args.workspace)
        return orch.run(task_text)

    if args.loop:
        from .core.ralph import run_goal_loop
        result = run_goal_loop(
            run_harness_fn=one_run, spec=spec, task=args.task,
            max_iterations=args.max_iterations, verify_cmd=args.verify_cmd,
            workspace=args.workspace or Path("workspace") / spec.name)
        print("\n" + "═" * 70)
        print(f"GOAL LOOP {'PASSED' if result.passed else 'DID NOT PASS'} "
              f"after {result.iterations} iteration(s)")
        print("═" * 70 + "\n")
        print(result.final_output)
    else:
        print("\n" + "═" * 70 + "\nFINAL REPLY\n" + "═" * 70 + "\n")
        print(one_run(args.task))


def cmd_templates(_args):
    print("Bundled domain harness templates (harness use <name>):\n")
    for d in sorted(TEMPLATES_DIR.iterdir()):
        if (d / "harness.yaml").exists():
            import yaml
            meta = yaml.safe_load((d / "harness.yaml").read_text())
            print(f"  {d.name:<22} [{meta['pattern']:<18}] {meta['description'][:70]}")


def cmd_use(args):
    src = TEMPLATES_DIR / args.name
    if not (src / "harness.yaml").exists():
        sys.exit(f"no template '{args.name}'. Run: harness templates")
    dst = Path("harnesses") / args.name
    if dst.exists():
        sys.exit(f"{dst} already exists")
    shutil.copytree(src, dst)
    (dst / "memory").mkdir(exist_ok=True)
    print(f"copied -> {dst}\nRun it:\n  harness run {dst} --task \"...\"")


def cmd_export(args):
    from .core.spec import HarnessSpec
    from .export import export
    spec = HarnessSpec.load(args.harness)
    hdir = Path(args.harness) if Path(args.harness).is_dir() else Path(args.harness).parent
    out = export(spec, hdir, args.to, args.output)
    print(f"exported for {args.to} -> {out}/")


def cmd_inspect(args):
    from .core.spec import HarnessSpec
    spec = HarnessSpec.load(args.harness)
    print(f"name:        {spec.name}\npattern:     {spec.pattern}"
          + (f" (supervisor: {spec.supervisor})" if spec.supervisor else ""))
    print(f"description: {spec.description}\nflow:        {spec.flow or '(all agents)'}")
    print(f"eval gate:   score >= {spec.eval.pass_threshold} on:")
    for c in spec.eval.quality_criteria:
        print(f"               - {c}")
    print("agents:")
    for a in spec.agents:
        print(f"  {a.name} [{a.model}]\n    role:  {a.role}\n    tools: {a.tools}")


def main():
    p = argparse.ArgumentParser(prog="harness",
                                description="Prompt -> domain-specific AI agent harness")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="generate a new harness from a prompt")
    b.add_argument("prompt")
    b.add_argument("--output", default="harnesses")
    b.add_argument("--architect-model", default="anthropic/claude-sonnet-4-6")
    b.add_argument("--default-model", default="anthropic/claude-sonnet-4-6",
                   help="default model for generated agents (any provider/model)")
    b.set_defaults(fn=cmd_build)

    r = sub.add_parser("run", help="run a harness on a task")
    r.add_argument("harness", help="path to harness dir or harness.yaml")
    r.add_argument("--task", required=True)
    r.add_argument("--workspace", default=None)
    r.add_argument("--loop", action="store_true",
                   help="wrap the run in a ralph/goal loop with an eval gate")
    r.add_argument("--max-iterations", type=int, default=3)
    r.add_argument("--verify-cmd", default=None,
                   help="deterministic verifier shell command (exit 0 = pass); "
                        "overrides LLM-as-judge")
    r.add_argument("--model-override", default=None,
                   help="force every agent onto one provider/model, e.g. ollama/llama3.1")
    r.set_defaults(fn=cmd_run)

    t = sub.add_parser("templates", help="list bundled domain templates")
    t.set_defaults(fn=cmd_templates)

    u = sub.add_parser("use", help="copy a bundled template into ./harnesses")
    u.add_argument("name")
    u.set_defaults(fn=cmd_use)

    i = sub.add_parser("inspect", help="show a harness's team and gate")
    i.add_argument("harness")
    i.set_defaults(fn=cmd_inspect)

    e = sub.add_parser("export", help="compile a harness into a host agent's plugin format")
    e.add_argument("harness")
    e.add_argument("--to", required=True, choices=["claude-code", "codex", "opencode"])
    e.add_argument("--output", default="exports")
    e.set_defaults(fn=cmd_export)

    if len(sys.argv) == 1:          # bare `harness` -> interactive TUI
        from .tui import main as tui_main
        tui_main()
        return

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
