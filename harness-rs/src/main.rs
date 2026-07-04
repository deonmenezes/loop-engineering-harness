//! CLI + architect. `harness build|run|templates|use|inspect` — full parity
//! with the Python CLI, same harness.yaml artifacts, single static binary.

mod engine;
mod providers;
mod runtime;
mod spec;

use anyhow::{bail, Context, Result};
use clap::{Parser, Subcommand};
use serde_json::{json, Value};
use std::fs;
use std::path::{Path, PathBuf};

use spec::HarnessSpec;

#[derive(Parser)]
#[command(name = "harness", version,
          about = "Prompt -> domain-specific AI agent harness (Rust runtime)")]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Generate a new harness from a domain description
    Build {
        prompt: String,
        #[arg(long, default_value = "harnesses")]
        output: String,
        #[arg(long, default_value = "anthropic/claude-sonnet-4-6")]
        architect_model: String,
        #[arg(long, default_value = "anthropic/claude-sonnet-4-6")]
        default_model: String,
    },
    /// Run a harness on a task
    Run {
        harness: PathBuf,
        #[arg(long)]
        task: String,
        #[arg(long)]
        workspace: Option<PathBuf>,
        /// wrap the run in a goal loop with an eval gate
        #[arg(long)]
        r#loop: bool,
        #[arg(long, default_value_t = 3)]
        max_iterations: u32,
        /// deterministic verifier (exit 0 = pass); overrides LLM-as-judge
        #[arg(long)]
        verify_cmd: Option<String>,
        /// force every agent onto one provider/model
        #[arg(long)]
        model_override: Option<String>,
    },
    /// List bundled domain templates
    Templates,
    /// Copy a bundled template into ./harnesses
    Use { name: String },
    /// Show a harness's team, pattern and quality gate
    Inspect { harness: PathBuf },
}

fn templates_dir() -> Option<PathBuf> {
    if let Ok(d) = std::env::var("HARNESS_TEMPLATES") {
        return Some(PathBuf::from(d));
    }
    let cwd = PathBuf::from("templates");
    if cwd.is_dir() { return Some(cwd); }
    std::env::current_exe().ok()
        .and_then(|e| e.ancestors().nth(3).map(|p| p.join("templates")))
        .filter(|p| p.is_dir())
}

fn main() -> Result<()> {
    // die quietly on closed pipes (e.g. `harness templates | head`)
    unsafe { libc::signal(libc::SIGPIPE, libc::SIG_DFL); }

    // minimal .env loader (no extra crate)
    if let Ok(env) = fs::read_to_string(".env") {
        for line in env.lines() {
            if let Some((k, v)) = line.split_once('=') {
                let (k, v) = (k.trim(), v.trim());
                if !k.is_empty() && !k.starts_with('#')
                   && std::env::var(k).is_err() {
                    std::env::set_var(k, v);
                }
            }
        }
    }

    match Cli::parse().cmd {
        Cmd::Build { prompt, output, architect_model, default_model } =>
            build(&prompt, &output, &architect_model, &default_model),
        Cmd::Run { harness, task, workspace, r#loop, max_iterations,
                   verify_cmd, model_override } =>
            run(&harness, &task, workspace, r#loop, max_iterations,
                verify_cmd.as_deref(), model_override.as_deref()),
        Cmd::Templates => templates(),
        Cmd::Use { name } => use_template(&name),
        Cmd::Inspect { harness } => inspect(&harness),
    }
}

// ────────────────────────────────────────────────────────────────── run
fn run(harness: &Path, task: &str, workspace: Option<PathBuf>, do_loop: bool,
       max_iterations: u32, verify_cmd: Option<&str>,
       model_override: Option<&str>) -> Result<()> {
    let (mut spec, harness_dir) = HarnessSpec::load(harness)?;
    if let Some(m) = model_override {
        for a in &mut spec.agents { a.model = m.to_string(); }
        spec.eval_spec.judge_model = m.to_string();
        spec.memory.summarizer_model = m.to_string();
    }
    let ws = workspace.unwrap_or_else(|| PathBuf::from("workspace")
        .join(&spec.name));

    if do_loop {
        let (passed, iters, output) = engine::run_goal_loop(
            &spec, task, max_iterations, verify_cmd, &ws, |t| {
                let orch = engine::Orchestrator::new(&spec, &harness_dir,
                    Some(ws.clone()))?;
                orch.run(t)
            })?;
        println!("\n{}", "═".repeat(70));
        println!("GOAL LOOP {} after {} iteration(s)",
                 if passed { "PASSED" } else { "DID NOT PASS" }, iters);
        println!("{}\n\n{}", "═".repeat(70), output);
    } else {
        let orch = engine::Orchestrator::new(&spec, &harness_dir, Some(ws))?;
        let reply = orch.run(task)?;
        println!("\n{}\nFINAL REPLY\n{}\n\n{}", "═".repeat(70),
                 "═".repeat(70), reply);
    }
    Ok(())
}

// ──────────────────────────────────────────────────────────── templates
fn templates() -> Result<()> {
    let dir = templates_dir().context("no templates directory found \
        (set HARNESS_TEMPLATES or run from the repo root)")?;
    println!("Bundled domain harness templates (harness use <name>):\n");
    let mut entries: Vec<_> = fs::read_dir(&dir)?.flatten().collect();
    entries.sort_by_key(|e| e.file_name());
    for e in entries {
        let f = e.path().join("harness.yaml");
        if f.exists() {
            let y: Value = serde_yaml::from_str(&fs::read_to_string(f)?)?;
            println!("  {:<22} [{:<18}] {}",
                     e.file_name().to_string_lossy(),
                     y["pattern"].as_str().unwrap_or(""),
                     y["description"].as_str().unwrap_or("")
                        .chars().take(70).collect::<String>());
        }
    }
    Ok(())
}

fn copy_dir(src: &Path, dst: &Path) -> Result<()> {
    fs::create_dir_all(dst)?;
    for e in fs::read_dir(src)?.flatten() {
        let (s, d) = (e.path(), dst.join(e.file_name()));
        if s.is_dir() { copy_dir(&s, &d)?; } else { fs::copy(&s, &d)?; }
    }
    Ok(())
}

fn use_template(name: &str) -> Result<()> {
    let src = templates_dir().context("no templates directory found")?.join(name);
    if !src.join("harness.yaml").exists() {
        bail!("no template '{}'. Run: harness templates", name);
    }
    let dst = PathBuf::from("harnesses").join(name);
    if dst.exists() { bail!("{} already exists", dst.display()); }
    copy_dir(&src, &dst)?;
    fs::create_dir_all(dst.join("memory"))?;
    println!("copied -> {}\nRun it:\n  harness run {} --task \"...\"",
             dst.display(), dst.display());
    Ok(())
}

fn inspect(harness: &Path) -> Result<()> {
    let (spec, _) = HarnessSpec::load(harness)?;
    println!("name:        {}", spec.name);
    println!("pattern:     {}{}", spec.pattern,
             spec.supervisor.as_ref()
                 .map(|s| format!(" (supervisor: {})", s)).unwrap_or_default());
    println!("description: {}", spec.description);
    println!("eval gate:   score >= {} on:", spec.eval_spec.pass_threshold);
    for c in &spec.eval_spec.quality_criteria { println!("               - {}", c); }
    println!("agents:");
    for a in &spec.agents {
        println!("  {} [{}]\n    role:  {}\n    tools: {:?}",
                 a.name, a.model, a.role, a.tools);
    }
    Ok(())
}

// ───────────────────────────────────────────────────────────── architect
const ARCHITECT_SYSTEM: &str = r#"You are a Harness Architect: you design multi-agent AI systems ("harnesses") from a user's domain description.

# Team-architecture patterns (pick exactly one)
- pipeline: sequential dependent stages. flow = ordered agent names.
- fanout: independent parallel angles merged at the end. flow = [worker1, ..., merger]; LAST agent merges.
- expert_pool: heterogeneous tasks routed to the right specialist. flow = [].
- producer_reviewer: quality via critique loops. flow = [producer, reviewer].
- supervisor: one coordinator plans/delegates/integrates. Set supervisor = coordinator name.
- hierarchical: like supervisor but delegates can sub-delegate. Set supervisor.

# Agents
3-6 agents. Each: name (snake_case), role (one line), system_prompt (150-400 words, written like briefing a skilled specialist: identity, objective, method, output format, quality bar, boundaries — domain-specific, never generic), model (default "__DEFAULT__"; use "anthropic/claude-haiku-4-5-20251001" for mechanical/low-judgment roles), tools (least-privilege subset of: read_file, write_file, list_files, run_shell, web_search, fetch_url, save_fact, recall), skills ([name] of ONE skill file you also write).

# Quality criteria
4-6 crisp, checkable, domain-specific criteria an LLM judge scores the output against.

# Output — ONLY this JSON, no markdown fences, no prose:
{"name": "snake_case", "description": "...", "pattern": "...", "supervisor": null, "flow": [], "agents": [{"name": "...", "role": "...", "system_prompt": "...", "model": "...", "tools": [], "skills": ["..."]}], "quality_criteria": ["..."], "skills": {"skill_name": "markdown body: concrete procedures, checklists, heuristics, pitfalls. 200-500 words."}}"#;

fn build(prompt: &str, output_root: &str, architect_model: &str,
         default_model: &str) -> Result<()> {
    let provider = providers::resolve(architect_model)?;
    println!("[architect] designing harness with {} ...", architect_model);
    let system = ARCHITECT_SYSTEM.replace("__DEFAULT__", default_model);
    let resp = provider.chat(&system,
        &[json!({"role": "user", "content": prompt})], &[], 8000)?;
    let clean = resp.text.trim()
        .trim_start_matches("```json").trim_start_matches("```")
        .trim_end_matches("```").trim();
    let mut design: Value = serde_json::from_str(clean)
        .map_err(|e| {
            let _ = fs::write("architect_failed.json", clean);
            anyhow::anyhow!("architect returned invalid JSON ({}); raw saved \
                             to architect_failed.json", e)
        })?;

    let skills = design["skills"].take();
    design.as_object_mut().unwrap().remove("skills");
    let criteria = design["quality_criteria"].take();
    design.as_object_mut().unwrap().remove("quality_criteria");
    design["eval"] = json!({"quality_criteria": criteria});

    // validate through the same deserializer the runtime uses
    let spec: HarnessSpec = serde_json::from_value(design.clone())
        .context("architect design does not match the HarnessSpec schema")?;
    spec.validate()?;

    let dir = PathBuf::from(output_root).join(&spec.name);
    fs::create_dir_all(dir.join("skills"))?;
    fs::create_dir_all(dir.join("memory"))?;
    fs::write(dir.join("harness.yaml"), serde_yaml::to_string(&design)?)?;
    if let Some(map) = skills.as_object() {
        for (name, body) in map {
            fs::write(dir.join("skills").join(format!("{}.md", name)),
                      body.as_str().unwrap_or(""))?;
        }
    }
    println!("[architect] pattern: {}{}", spec.pattern,
             spec.supervisor.as_ref()
                 .map(|s| format!(" (supervisor: {})", s)).unwrap_or_default());
    for a in &spec.agents {
        println!("  - {}: {}  [{}] tools={:?}", a.name, a.role, a.model, a.tools);
    }
    println!("[architect] saved -> {}/", dir.display());
    println!("\nRun it:\n  harness run {} --task \"...\"", dir.display());
    Ok(())
}
