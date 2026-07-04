//! HarnessSpec — deserializes the SAME harness.yaml the Python builder emits.
//! The YAML file is the contract; both runtimes execute identical harnesses.

use anyhow::{bail, Context, Result};
use serde::Deserialize;
use std::path::{Path, PathBuf};

pub const PATTERNS: [&str; 6] = ["pipeline", "fanout", "expert_pool",
    "producer_reviewer", "supervisor", "hierarchical"];

fn d_model() -> String { "anthropic/claude-sonnet-4-6".into() }
fn d_max_turns() -> u32 { 12 }
fn d_true() -> bool { true }
fn d_consolidate() -> u32 { 3 }
fn d_summarizer() -> String { "anthropic/claude-haiku-4-5-20251001".into() }
fn d_tokens() -> u64 { 300_000 }
fn d_wall() -> u64 { 900 }
fn d_deny() -> Vec<String> {
    vec![r"\brm\s+-rf\s+/".into(), r"\bsudo\b".into(), r"\bmkfs\b".into(),
         r"\bssh\b".into(), r">\s*/dev/".into()]
}
fn d_judge() -> String { d_model() }
fn d_threshold() -> f64 { 7.0 }

#[derive(Debug, Clone, Deserialize)]
pub struct AgentSpec {
    pub name: String,
    pub role: String,
    pub system_prompt: String,
    #[serde(default = "d_model")]
    pub model: String,
    #[serde(default)]
    pub tools: Vec<String>,
    #[serde(default)]
    pub skills: Vec<String>,
    #[serde(default = "d_max_turns")]
    pub max_turns: u32,
}

#[derive(Debug, Clone, Deserialize)]
pub struct MemorySpec {
    #[serde(default = "d_true")]
    pub semantic: bool,
    #[serde(default = "d_true")]
    pub episodic: bool,
    #[serde(default = "d_consolidate")]
    pub consolidate_after: u32,
    #[serde(default = "d_summarizer")]
    pub summarizer_model: String,
}
impl Default for MemorySpec {
    fn default() -> Self {
        Self { semantic: true, episodic: true, consolidate_after: 3,
               summarizer_model: d_summarizer() }
    }
}

#[derive(Debug, Clone, Deserialize)]
pub struct GuardrailSpec {
    #[serde(default = "d_tokens")]
    pub max_total_tokens: u64,
    #[serde(default = "d_wall")]
    pub max_wall_seconds: u64,
    #[serde(default = "d_deny")]
    pub shell_deny_patterns: Vec<String>,
}
impl Default for GuardrailSpec {
    fn default() -> Self {
        Self { max_total_tokens: d_tokens(), max_wall_seconds: d_wall(),
               shell_deny_patterns: d_deny() }
    }
}

#[derive(Debug, Clone, Deserialize)]
pub struct EvalSpec {
    #[serde(default)]
    pub quality_criteria: Vec<String>,
    #[serde(default = "d_judge")]
    pub judge_model: String,
    #[serde(default = "d_threshold")]
    pub pass_threshold: f64,
}
impl Default for EvalSpec {
    fn default() -> Self {
        Self { quality_criteria: vec![], judge_model: d_judge(),
               pass_threshold: d_threshold() }
    }
}

/// flow entries may be "name" or ["a","b"] (parallel groups)
#[derive(Debug, Clone, Deserialize)]
#[serde(untagged)]
pub enum FlowItem {
    One(String),
    Group(Vec<String>),
}

#[derive(Debug, Clone, Deserialize)]
pub struct HarnessSpec {
    pub name: String,
    #[serde(default)]
    pub description: String,
    pub pattern: String,
    pub agents: Vec<AgentSpec>,
    #[serde(default)]
    pub flow: Vec<FlowItem>,
    #[serde(default)]
    pub supervisor: Option<String>,
    #[serde(default)]
    pub memory: MemorySpec,
    #[serde(default)]
    pub guardrails: GuardrailSpec,
    #[serde(default, rename = "eval")]
    pub eval_spec: EvalSpec,
}

impl HarnessSpec {
    pub fn load(path: &Path) -> Result<(Self, PathBuf)> {
        let (file, dir) = if path.is_dir() {
            (path.join("harness.yaml"), path.to_path_buf())
        } else {
            (path.to_path_buf(),
             path.parent().unwrap_or(Path::new(".")).to_path_buf())
        };
        let text = std::fs::read_to_string(&file)
            .with_context(|| format!("reading {}", file.display()))?;
        let spec: HarnessSpec = serde_yaml::from_str(&text)
            .with_context(|| format!("parsing {}", file.display()))?;
        spec.validate()?;
        Ok((spec, dir))
    }

    pub fn validate(&self) -> Result<()> {
        if !PATTERNS.contains(&self.pattern.as_str()) {
            bail!("pattern must be one of {:?}, got '{}'", PATTERNS, self.pattern);
        }
        let names: Vec<&str> = self.agents.iter().map(|a| a.name.as_str()).collect();
        let mut dedup = names.clone();
        dedup.sort();
        dedup.dedup();
        if dedup.len() != names.len() {
            bail!("duplicate agent names: {:?}", names);
        }
        if matches!(self.pattern.as_str(), "supervisor" | "hierarchical") {
            match &self.supervisor {
                Some(s) if names.contains(&s.as_str()) => {}
                other => bail!("pattern '{}' needs a valid 'supervisor'; got {:?}",
                               self.pattern, other),
            }
        }
        for item in &self.flow {
            let refs: Vec<&String> = match item {
                FlowItem::One(s) => vec![s],
                FlowItem::Group(g) => g.iter().collect(),
            };
            for r in refs {
                if !names.contains(&r.as_str()) {
                    bail!("flow references unknown agent '{}'", r);
                }
            }
        }
        Ok(())
    }

    pub fn agent(&self, name: &str) -> Result<&AgentSpec> {
        self.agents.iter().find(|a| a.name == name)
            .with_context(|| format!("no agent named '{}'", name))
    }

    /// flattened execution order (flow if given, else all agents)
    pub fn flow_or_all(&self) -> Vec<String> {
        if self.flow.is_empty() {
            return self.agents.iter().map(|a| a.name.clone()).collect();
        }
        self.flow.iter().flat_map(|i| match i {
            FlowItem::One(s) => vec![s.clone()],
            FlowItem::Group(g) => g.clone(),
        }).collect()
    }
}
