//! Inner agent loop + the six team patterns + the ralph/goal loop.
//! Patterns are wiring between instances of the same inner loop; fanout uses
//! real OS threads (the Rust advantage over the Python runtime's thread pool).

use anyhow::Result;
use regex::Regex;
use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::time::Instant;

use crate::providers::{resolve, ToolSchema};
use crate::runtime::{assemble_context, execute, maybe_consolidate, schema_for,
                     EpisodicMemory, SemanticMemory, ToolCtx, Trace};
use crate::spec::{AgentSpec, HarnessSpec};

// ─────────────────────────────────────────────────────── inner agent loop
pub struct RunOutcome {
    pub reply: String,
    pub input_tokens: u64,
    pub output_tokens: u64,
}

/// delegate hook: Some(fn(agent_name, subtask) -> result) for supervisor patterns
type Delegate<'a> = Option<&'a (dyn Fn(&str, &str) -> String + Sync)>;

pub fn run_agent(agent: &AgentSpec, system: &str, task: &str, ctx: &ToolCtx,
                 spec: &HarnessSpec, trace: &Trace, tokens_used: u64,
                 delegate: Delegate) -> Result<RunOutcome> {
    let provider = resolve(&agent.model)?;
    let mut tools: Vec<ToolSchema> =
        agent.tools.iter().filter_map(|t| schema_for(t)).collect();
    if let Some(_) = delegate {
        tools.push(ToolSchema {
            name: "delegate".into(),
            description: "Delegate a subtask to a team member by name.".into(),
            parameters: json!({"type": "object", "properties": {
                "agent_name": {"type": "string"}, "subtask": {"type": "string"}},
                "required": ["agent_name", "subtask"]}),
        });
    }

    let mut messages = vec![json!({"role": "user", "content": task})];
    let (mut in_tok, mut out_tok) = (0u64, 0u64);
    let started = Instant::now();

    for turn in 0..agent.max_turns {
        // guardrails in code, checked before every model call
        if tokens_used + in_tok + out_tok > spec.guardrails.max_total_tokens {
            return Ok(RunOutcome { reply: "(stopped: token_budget)".into(),
                                   input_tokens: in_tok, output_tokens: out_tok });
        }
        if started.elapsed().as_secs() > spec.guardrails.max_wall_seconds {
            return Ok(RunOutcome { reply: "(stopped: wall_clock)".into(),
                                   input_tokens: in_tok, output_tokens: out_tok });
        }

        // context management: prune all but the newest 4 tool results
        prune_tool_results(&mut messages, 4);
        let resp = provider.chat(system, &messages, &tools, 4096)?;
        in_tok += resp.input_tokens;
        out_tok += resp.output_tokens;
        trace.log("model_turn", json!({"agent": agent.name, "turn": turn,
            "model": agent.model, "in_tokens": resp.input_tokens,
            "out_tokens": resp.output_tokens}));
        messages.push(resp.raw_assistant_message.clone());

        if resp.tool_calls.is_empty() {
            return Ok(RunOutcome { reply: resp.text,
                                   input_tokens: in_tok, output_tokens: out_tok });
        }

        let mut results = vec![];
        for call in &resp.tool_calls {
            trace.log("tool_call", json!({"agent": agent.name,
                "tool": call.name, "input": call.input}));
            let (out, is_err) = if call.name == "delegate" {
                match delegate {
                    Some(d) => {
                        let a = call.input["agent_name"].as_str().unwrap_or("");
                        let s = call.input["subtask"].as_str().unwrap_or("");
                        (d(a, s), false)
                    }
                    None => ("delegate unavailable here".into(), true),
                }
            } else {
                execute(ctx, &call.name, &call.input)
            };
            trace.log("tool_result", json!({"agent": agent.name,
                "tool": call.name, "is_error": is_err,
                "result": out.chars().take(400).collect::<String>()}));
            results.push((call.id.clone(), out, is_err));
        }
        messages.extend(provider.tool_result_messages(&results));
    }
    Ok(RunOutcome { reply: "(stopped: max_turns)".into(),
                    input_tokens: in_tok, output_tokens: out_tok })
}

const PRUNED: &str = "[old tool result pruned to save context]";

fn prune_tool_results(messages: &mut [Value], keep_last: usize) {
    // collect (msg_idx, Some(block_idx)|None) for both dialects
    let mut locs: Vec<(usize, Option<usize>)> = vec![];
    for (mi, m) in messages.iter().enumerate() {
        if m["role"] == "tool" {
            locs.push((mi, None));                       // openai dialect
        } else if m["role"] == "user" {
            if let Some(arr) = m["content"].as_array() {
                for (bi, b) in arr.iter().enumerate() {
                    if b["type"] == "tool_result" {
                        locs.push((mi, Some(bi)));       // anthropic dialect
                    }
                }
            }
        }
    }
    let cut = locs.len().saturating_sub(keep_last);
    for (mi, bi) in locs.into_iter().take(cut) {
        match bi {
            None => messages[mi]["content"] = json!(PRUNED),
            Some(bi) => messages[mi]["content"][bi]["content"] = json!(PRUNED),
        }
    }
}

// ───────────────────────────────────────────────────────── orchestrator
pub struct Orchestrator<'a> {
    pub spec: &'a HarnessSpec,
    pub harness_dir: PathBuf,
    pub workspace: PathBuf,
    pub trace: Trace,
    pub semantic: Option<SemanticMemory>,
    tokens_used: std::sync::atomic::AtomicU64,
}

impl<'a> Orchestrator<'a> {
    pub fn new(spec: &'a HarnessSpec, harness_dir: &Path,
               workspace: Option<PathBuf>) -> Result<Self> {
        Ok(Self {
            spec,
            harness_dir: harness_dir.to_path_buf(),
            workspace: workspace.unwrap_or_else(|| PathBuf::from("workspace")
                .join(&spec.name)),
            trace: Trace::new(&spec.name)?,
            semantic: spec.memory.semantic
                .then(|| SemanticMemory::open(harness_dir)),
            tokens_used: 0.into(),
        })
    }

    fn run_one(&self, name: &str, task: &str) -> Result<String> {
        self.run_one_with(name, task, None)
    }

    fn run_one_with(&self, name: &str, task: &str, delegate: Delegate) -> Result<String> {
        use std::sync::atomic::Ordering;
        let agent = self.spec.agent(name)?;
        let system = assemble_context(&self.harness_dir, agent, task,
            &self.spec.memory, &self.workspace, &self.spec.name,
            &self.spec.pattern, &self.spec.guardrails);
        println!("  ▶ {} [{}]", name, agent.model);
        let ctx = ToolCtx::new(self.workspace.clone(), &self.spec.guardrails,
                               self.semantic.as_ref(), self.harness_dir.clone());
        let out = run_agent(agent, &system, task, &ctx, self.spec, &self.trace,
                            self.tokens_used.load(Ordering::Relaxed), delegate)?;
        self.tokens_used.fetch_add(out.input_tokens + out.output_tokens,
                                   Ordering::Relaxed);
        println!("  ✔ {}", name);
        Ok(out.reply)
    }

    pub fn run(&self, task: &str) -> Result<String> {
        self.trace.log("run_start", json!({"harness": self.spec.name,
            "pattern": self.spec.pattern, "task": task}));
        let reply = match self.spec.pattern.as_str() {
            "pipeline" => self.pipeline(task),
            "fanout" => self.fanout(task),
            "expert_pool" => self.expert_pool(task),
            "producer_reviewer" => self.producer_reviewer(task),
            "supervisor" => self.supervised(task, 1),
            "hierarchical" => self.supervised(task, 3),
            _ => unreachable!("validated"),
        }?;
        self.trace.log("reply",
            json!({"text": reply.chars().take(2000).collect::<String>()}));
        if self.spec.memory.episodic {
            EpisodicMemory::open(&self.harness_dir)
                .add(task, &reply.chars().take(1500).collect::<String>());
            maybe_consolidate(&self.harness_dir, &self.spec.memory, &self.trace);
        }
        self.trace.finish();
        Ok(reply)
    }

    fn pipeline(&self, task: &str) -> Result<String> {
        let mut payload = String::new();
        for name in self.spec.flow_or_all() {
            let input = if payload.is_empty() { task.to_string() } else {
                format!("OVERALL TASK:\n{}\n\nINPUT FROM PREVIOUS STAGE:\n{}",
                        task, payload)
            };
            payload = self.run_one(&name, &input)?;
        }
        Ok(payload)
    }

    fn fanout(&self, task: &str) -> Result<String> {
        let order = self.spec.flow_or_all();
        let (workers, merger) = order.split_at(order.len() - 1);
        // real parallelism: one OS thread per worker (scoped — no 'static needed)
        let results: Vec<(String, Result<String>)> = std::thread::scope(|s| {
            workers.iter().map(|n| {
                let n = n.clone();
                let task = task.to_string();
                (n.clone(), s.spawn(move || self.run_one(&n, &task)))
            }).collect::<Vec<_>>().into_iter()
              .map(|(n, h)| (n, h.join().expect("worker thread panicked")))
              .collect()
        });
        let merged: Vec<String> = results.into_iter().map(|(n, r)| {
            format!("=== FINDINGS FROM {} ===\n{}", n.to_uppercase(),
                    r.unwrap_or_else(|e| format!("(worker failed: {})", e)))
        }).collect();
        self.run_one(&merger[0], &format!(
            "OVERALL TASK:\n{}\n\nMERGE THESE PARALLEL FINDINGS INTO ONE \
             COHERENT DELIVERABLE:\n{}", task, merged.join("\n\n")))
    }

    fn expert_pool(&self, task: &str) -> Result<String> {
        let roster: String = self.spec.agents.iter()
            .map(|a| format!("- {}: {}", a.name, a.role))
            .collect::<Vec<_>>().join("\n");
        let provider = resolve(&self.spec.agents[0].model)?;
        let resp = provider.chat(
            "You are a router. Given a task and an expert roster, reply ONLY \
             with a comma-separated list of 1-3 expert names best suited. No prose.",
            &[json!({"role": "user",
                     "content": format!("TASK: {}\n\nROSTER:\n{}", task, roster)})],
            &[], 50)?;
        let names: Vec<&str> = self.spec.agents.iter()
            .map(|a| a.name.as_str()).collect();
        let mut chosen: Vec<String> = resp.text.split(',')
            .map(|s| s.trim().to_string())
            .filter(|s| names.contains(&s.as_str())).collect();
        if chosen.is_empty() { chosen.push(names[0].into()); }
        self.trace.log("router", json!({"chosen": chosen}));
        println!("  [router] -> {:?}", chosen);
        let outputs: Vec<String> = chosen.iter()
            .map(|n| self.run_one(n, task)).collect::<Result<_>>()?;
        Ok(if outputs.len() == 1 { outputs.into_iter().next().unwrap() } else {
            chosen.iter().zip(outputs)
                .map(|(n, o)| format!("=== {} ===\n{}", n, o))
                .collect::<Vec<_>>().join("\n\n")
        })
    }

    fn producer_reviewer(&self, task: &str) -> Result<String> {
        let order = self.spec.flow_or_all();
        let (producer, reviewer) = (&order[0], &order[1]);
        let mut draft = self.run_one(producer, task)?;
        for round in 0..3 {
            let review = self.run_one(reviewer, &format!(
                "TASK:\n{}\n\nDRAFT TO REVIEW:\n{}\n\nCritique against the task \
                 and your skills. If genuinely ready, reply with exactly \
                 'APPROVED' and nothing else. Otherwise list specific fixes.",
                task, draft))?;
            if review.trim().to_uppercase().starts_with("APPROVED") {
                self.trace.log("review_approved", json!({"round": round + 1}));
                println!("  [review] approved on round {}", round + 1);
                break;
            }
            draft = self.run_one(producer, &format!(
                "TASK:\n{}\n\nYOUR PREVIOUS DRAFT:\n{}\n\nREVIEWER FEEDBACK \
                 (address every point):\n{}", task, draft, review))?;
        }
        Ok(draft)
    }

    fn supervised(&self, task: &str, max_depth: u32) -> Result<String> {
        let sup = self.spec.supervisor.as_ref().unwrap().clone();
        self.supervised_run(&sup, task, 0, max_depth)
    }

    fn supervised_run(&self, name: &str, task: &str, depth: u32,
                      max_depth: u32) -> Result<String> {
        // subtasks flow down the hierarchy, never back up
        let excluded_sup = if depth > 0 { self.spec.supervisor.clone() } else { None };
        let team: Vec<&AgentSpec> = self.spec.agents.iter()
            .filter(|a| a.name != name
                        && Some(&a.name) != excluded_sup.as_ref()).collect();
        let roster: String = team.iter()
            .map(|a| format!("- {}: {}", a.name, a.role))
            .collect::<Vec<_>>().join("\n");
        let team_names: Vec<String> = team.iter().map(|a| a.name.clone()).collect();

        let delegate = move |agent_name: &str, subtask: &str| -> String {
            if !team_names.contains(&agent_name.to_string()) {
                return format!("unknown team member '{}'. Roster:\n{}",
                               agent_name, roster);
            }
            let result = if depth + 1 <= max_depth {
                self.supervised_run(agent_name, subtask, depth + 1, max_depth)
            } else {
                self.run_one(agent_name, subtask)
            };
            result.unwrap_or_else(|e| format!("(delegate failed: {})", e))
        };

        let agent = self.spec.agent(name)?;
        let mut system = assemble_context(&self.harness_dir, agent, task,
            &self.spec.memory, &self.workspace, &self.spec.name,
            &self.spec.pattern, &self.spec.guardrails);
        let roster2: String = team.iter()
            .map(|a| format!("- {}: {}", a.name, a.role))
            .collect::<Vec<_>>().join("\n");
        system += &format!("\n\nYou are the coordinator. Break the task down, \
            use `delegate` for specialist work, then integrate results into the \
            final deliverable yourself.\nTeam:\n{}", roster2);
        println!("  ▶ {} [{}] (supervisor, depth {})", name, agent.model, depth);
        let ctx = ToolCtx::new(self.workspace.clone(), &self.spec.guardrails,
                               self.semantic.as_ref(), self.harness_dir.clone());
        use std::sync::atomic::Ordering;
        let out = run_agent(agent, &system, task, &ctx, self.spec, &self.trace,
                            self.tokens_used.load(Ordering::Relaxed),
                            Some(&delegate))?;
        self.tokens_used.fetch_add(out.input_tokens + out.output_tokens,
                                   Ordering::Relaxed);
        Ok(out.reply)
    }
}

// ─────────────────────────────────────────────────────── ralph/goal loop
pub fn judge(spec: &HarnessSpec, task: &str, output: &str) -> (f64, String) {
    let default_criteria = vec!["fully addresses the task".to_string(),
        "factually careful".into(), "clear and well-structured".into()];
    let criteria = if spec.eval_spec.quality_criteria.is_empty() {
        &default_criteria } else { &spec.eval_spec.quality_criteria };
    let prompt = format!(
        "TASK:\n{}\n\nQUALITY CRITERIA:\n{}\n\nOUTPUT TO JUDGE:\n{}",
        task, criteria.iter().map(|c| format!("- {}", c))
            .collect::<Vec<_>>().join("\n"),
        output.chars().take(20000).collect::<String>());
    let system = "You are a strict quality judge for AI agent output. Score \
        the OUTPUT against the TASK and each CRITERION. Be harsh: 9-10 means \
        genuinely excellent. Respond ONLY with JSON: {\"score\": <float 0-10>, \
        \"diagnosis\": \"<specific fix instructions for the next attempt>\"}";
    let Ok(provider) = resolve(&spec.eval_spec.judge_model) else {
        return (0.0, "judge model unresolvable".into());
    };
    match provider.chat(system,
        &[json!({"role": "user", "content": prompt})], &[], 1500) {
        Ok(r) => {
            let clean = Regex::new(r"```(json)?|```").unwrap()
                .replace_all(&r.text, "").trim().to_string();
            match serde_json::from_str::<Value>(&clean) {
                Ok(v) => (v["score"].as_f64().unwrap_or(0.0),
                          v["diagnosis"].as_str().unwrap_or("").to_string()),
                Err(_) => (0.0, format!("judge output unparseable: {}",
                    r.text.chars().take(800).collect::<String>())),
            }
        }
        Err(e) => (0.0, format!("judge call failed: {}", e)),
    }
}

pub fn run_goal_loop<F>(spec: &HarnessSpec, task: &str, max_iterations: u32,
                        verify_cmd: Option<&str>, workspace: &Path,
                        mut run_fn: F) -> Result<(bool, u32, String)>
where F: FnMut(&str) -> Result<String> {
    let mut feedback = String::new();
    let mut output = String::new();
    for i in 1..=max_iterations {
        println!("\n═══ GOAL LOOP iteration {}/{} ═══", i, max_iterations);
        let iter_task = if feedback.is_empty() { task.to_string() } else {
            format!("{}\n\n--- PREVIOUS ATTEMPT FAILED THE QUALITY GATE ---\n\
                     Verifier diagnosis (fix these specifically):\n{}",
                    task, feedback)
        };
        output = run_fn(&iter_task)?;

        let (passed, diagnosis) = match verify_cmd {
            Some(cmd) => {
                let out = std::process::Command::new("sh").arg("-c").arg(cmd)
                    .current_dir(workspace).output()?;
                let text = format!("{}{}",
                    String::from_utf8_lossy(&out.stdout),
                    String::from_utf8_lossy(&out.stderr));
                (out.status.success(),
                 text.chars().rev().take(2000).collect::<String>()
                     .chars().rev().collect())
            }
            None => {
                let (score, diag) = judge(spec, task, &output);
                println!("  [gate] score={}/10 (threshold {})",
                         score, spec.eval_spec.pass_threshold);
                (score >= spec.eval_spec.pass_threshold, diag)
            }
        };
        if passed {
            println!("  [gate] PASSED -> release");
            return Ok((true, i, output));
        }
        println!("  [gate] failed -> diagnosing and re-running");
        feedback = diagnosis;
    }
    Ok((false, max_iterations, output))
}
