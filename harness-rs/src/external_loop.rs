//! The EXTERNAL LOOP — the layer outside the harness (whiteboard's pink box):
//! goal → planner → numbered checklist → one fresh harness run per item →
//! per-item gate → check off → replan on repeated failure → done when the
//! list is done. State lives in a file, so the loop is resumable and its
//! scope exceeds any single context window.

use anyhow::{Context, Result};
use regex::Regex;
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::path::{Path, PathBuf};

use crate::providers::resolve;
use crate::spec::HarnessSpec;

const PLANNER_SYSTEM: &str = "You are a planner for an autonomous agent loop.\n\
Decompose the GOAL into 3-8 concrete, sequential steps a specialist agent team \
will execute ONE PER RUN. Each step must be independently executable given only \
the goal + notes from completed steps, and independently checkable.\n\
Respond ONLY with JSON:\n\
{\"steps\": [{\"title\": \"<imperative, one line>\", \"details\": \"<what exactly \
to do, 1-3 sentences>\", \"done_when\": \"<concrete, checkable completion \
criterion>\"}]}";

const REPLAN_SYSTEM: &str = "You are revising the remaining plan of an \
autonomous agent loop after a step kept failing. Given the GOAL, COMPLETED \
steps, the FAILED step and its failure diagnosis, produce a revised list of \
remaining steps that still achieves the goal. Respond ONLY with JSON:\n\
{\"steps\": [{\"title\": \"...\", \"details\": \"...\", \"done_when\": \"...\"}]}";

const STEP_JUDGE_SYSTEM: &str = "You are a strict per-step gate in an agent \
loop. Given one STEP (with its done_when criterion) and the agent team's \
OUTPUT for it, decide if the step is genuinely complete. Respond ONLY with \
JSON: {\"passed\": true|false, \"note\": \"<if passed: 1-2 sentence factual \
summary for downstream steps>\", \"diagnosis\": \"<if failed: specific fix \
instructions>\"}";

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Step {
    pub title: String,
    #[serde(default)]
    pub details: String,
    #[serde(default)]
    pub done_when: String,
    #[serde(default = "pending")]
    pub status: String, // pending | done | failed
    #[serde(default)]
    pub attempts: u32,
    #[serde(default)]
    pub note: String,
}
fn pending() -> String { "pending".into() }

#[derive(Debug, Serialize, Deserialize)]
pub struct LoopState {
    pub goal: String,
    pub steps: Vec<Step>,
    #[serde(default)]
    pub cycles: u32,
}

impl LoopState {
    fn path_for(harness_dir: &Path, goal: &str) -> PathBuf {
        // tiny stable hash (FNV-1a) — no extra crate needed
        let mut h: u64 = 0xcbf29ce484222325;
        for b in goal.bytes() {
            h ^= b as u64;
            h = h.wrapping_mul(0x100000001b3);
        }
        harness_dir.join("loop_state").join(format!("{:010x}.json", h & 0xffffffffff))
    }

    fn save(&self, path: &Path) -> Result<()> {
        std::fs::create_dir_all(path.parent().unwrap())?;
        std::fs::write(path, serde_json::to_string_pretty(self)?)?;
        Ok(())
    }

    pub fn render(&self) -> String {
        self.steps.iter().enumerate().map(|(i, s)| {
            let icon = match s.status.as_str() {
                "done" => "✓", "failed" => "✗", _ => "·" };
            let attempts = if s.attempts > 1 {
                format!("  [{} attempts]", s.attempts) } else { String::new() };
            format!("  {} {}. {}{}", icon, i + 1, s.title, attempts)
        }).collect::<Vec<_>>().join("\n")
    }

    pub fn done_count(&self) -> usize {
        self.steps.iter().filter(|s| s.status == "done").count()
    }
}

fn llm_json(model: &str, system: &str, prompt: &str,
            max_tokens: u32) -> Result<serde_json::Value> {
    let provider = resolve(model)?;
    let resp = provider.chat(system,
        &[json!({"role": "user", "content": prompt})], &[], max_tokens)?;
    let clean = Regex::new(r"```(json)?|```").unwrap()
        .replace_all(&resp.text, "").trim().to_string();
    serde_json::from_str(&clean)
        .with_context(|| format!("model returned non-JSON: {}",
            resp.text.chars().take(300).collect::<String>()))
}

fn parse_steps(v: &serde_json::Value) -> Result<Vec<Step>> {
    let steps: Vec<Step> = serde_json::from_value(v["steps"].clone())
        .context("plan JSON missing valid 'steps' array")?;
    anyhow::ensure!(!steps.is_empty(), "planner produced an empty plan");
    Ok(steps)
}

pub fn run_external_loop<F>(spec: &HarnessSpec, harness_dir: &Path, goal: &str,
                            fresh: bool, mut run_harness: F) -> Result<LoopState>
where F: FnMut(&str) -> Result<String> {
    let cfg = &spec.loop_spec;
    let state_path = LoopState::path_for(harness_dir, goal);

    // ── plan or resume ──────────────────────────────────────────────────
    let mut state = if !fresh && state_path.exists() {
        let st: LoopState = serde_json::from_str(
            &std::fs::read_to_string(&state_path)?)?;
        println!("[loop] resuming from {} ({}/{} done)",
                 state_path.file_name().unwrap().to_string_lossy(),
                 st.done_count(), st.steps.len());
        st
    } else {
        println!("[planner] decomposing goal with {} ...", cfg.planner_model);
        let plan = llm_json(&cfg.planner_model, PLANNER_SYSTEM,
                            &format!("GOAL:\n{}", goal), 4000)?;
        let st = LoopState { goal: goal.into(), steps: parse_steps(&plan)?,
                             cycles: 0 };
        st.save(&state_path)?;
        st
    };
    println!("\nPLAN:\n{}\n", state.render());

    // ── the loop ────────────────────────────────────────────────────────
    while state.cycles < cfg.max_cycles {
        let Some(i) = state.steps.iter().position(|s| s.status == "pending")
        else {
            println!("\n[loop] all steps checked off — goal complete\n{}",
                     state.render());
            return Ok(state);
        };
        state.cycles += 1;
        state.steps[i].attempts += 1;
        let (title, details, done_when, attempts) = {
            let s = &state.steps[i];
            (s.title.clone(), s.details.clone(), s.done_when.clone(), s.attempts)
        };
        println!("═══ CYCLE {}/{} · step {}: {} (attempt {}) ═══",
                 state.cycles, cfg.max_cycles, i + 1, title, attempts);

        let completed: String = state.steps.iter().enumerate()
            .filter(|(_, s)| s.status == "done")
            .map(|(j, s)| format!("- ({}) {}: {}", j + 1, s.title,
                 if s.note.is_empty() { "done" } else { &s.note }))
            .collect::<Vec<_>>().join("\n");
        let task = format!(
            "OVERALL GOAL (for context — do NOT do it all now):\n{}\n\n{}\
             YOUR CURRENT STEP — do exactly this and only this:\n{}. {}\n{}\n\
             Definition of done: {}",
            state.goal,
            if completed.is_empty() { String::new() } else {
                format!("COMPLETED STEPS (build on these):\n{}\n\n", completed) },
            i + 1, title, details, done_when);

        let output = run_harness(&task)?;   // fresh harness run

        // ── per-item gate ───────────────────────────────────────────────
        let (passed, note, diagnosis) = if cfg.step_verify == "judge" {
            match llm_json(&spec.eval_spec.judge_model, STEP_JUDGE_SYSTEM,
                &format!("STEP:\n{}\n{}\ndone_when: {}\n\nOUTPUT:\n{}",
                    title, details, done_when,
                    output.chars().take(20000).collect::<String>()), 1000) {
                Ok(v) => (v["passed"].as_bool().unwrap_or(false),
                          v["note"].as_str().unwrap_or("").to_string(),
                          v["diagnosis"].as_str().unwrap_or("").to_string()),
                Err(e) => (false, String::new(), format!("judge failed: {}", e)),
            }
        } else {
            (true, output.chars().take(300).collect(), String::new())
        };

        if passed {
            state.steps[i].status = "done".into();
            state.steps[i].note = note.chars().take(500).collect();
            println!("  ✓ step {} checked off", i + 1);
        } else {
            println!("  ✗ step {} failed gate: {}", i + 1,
                     diagnosis.chars().take(120).collect::<String>());
            if attempts < cfg.max_attempts_per_step {
                state.steps[i].details.push_str(
                    &format!("\nPREVIOUS ATTEMPT FAILED — fix: {}",
                             diagnosis.chars().take(800).collect::<String>()));
            } else if cfg.replan_on_failure {
                println!("  [replan] revising remaining plan around the failure");
                let done_notes: String = state.steps.iter()
                    .filter(|s| s.status == "done")
                    .map(|s| format!("- {}: {}", s.title, s.note))
                    .collect::<Vec<_>>().join("\n");
                match llm_json(&cfg.planner_model, REPLAN_SYSTEM,
                    &format!("GOAL:\n{}\n\nCOMPLETED:\n{}\n\nFAILED STEP:\n{}\n\n\
                              DIAGNOSIS:\n{}", state.goal, done_notes, title,
                             diagnosis), 4000)
                    .and_then(|v| parse_steps(&v)) {
                    Ok(new_steps) => {
                        state.steps[i].status = "failed".into();
                        let mut kept: Vec<Step> = state.steps.iter()
                            .filter(|s| s.status != "pending").cloned().collect();
                        kept.extend(new_steps);
                        state.steps = kept;
                    }
                    Err(e) => {
                        println!("  [replan] failed ({}); marking step failed", e);
                        state.steps[i].status = "failed".into();
                    }
                }
            } else {
                state.steps[i].status = "failed".into();
            }
        }
        state.save(&state_path)?;          // resumability
        println!("\n{}\n", state.render());
    }
    println!("[loop] stopped: max_cycles ({}) reached", cfg.max_cycles);
    Ok(state)
}
