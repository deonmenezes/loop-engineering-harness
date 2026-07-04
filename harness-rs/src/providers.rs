//! Unified multi-provider LLM API — same routing strings as the Python side:
//! anthropic/…, openai/…, groq/…, openrouter/…, ollama/…
//! One normalized ChatResponse; messages stay in provider dialect as JSON.

use anyhow::{bail, Context, Result};
use serde_json::{json, Value};

#[derive(Debug, Clone)]
pub struct ToolCall {
    pub id: String,
    pub name: String,
    pub input: Value,
}

#[derive(Debug)]
pub struct ChatResponse {
    pub text: String,
    pub tool_calls: Vec<ToolCall>,
    pub input_tokens: u64,
    pub output_tokens: u64,
    pub raw_assistant_message: Value,
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Dialect { Anthropic, OpenAI }

#[derive(Debug, Clone)]
pub struct Provider {
    pub dialect: Dialect,
    pub base_url: String,
    pub api_key: String,
    pub model: String,
}

/// tool schema in our neutral form
#[derive(Debug, Clone)]
pub struct ToolSchema {
    pub name: String,
    pub description: String,
    pub parameters: Value,
}

pub fn resolve(model_string: &str) -> Result<Provider> {
    let (prefix, model) = model_string.split_once('/')
        .with_context(|| format!("model must be 'provider/model', got '{}'", model_string))?;
    let env = |k: &str| std::env::var(k).unwrap_or_default();
    let p = match prefix {
        "anthropic" => Provider { dialect: Dialect::Anthropic,
            base_url: "https://api.anthropic.com/v1/messages".into(),
            api_key: env("ANTHROPIC_API_KEY"), model: model.into() },
        "openai" => Provider { dialect: Dialect::OpenAI,
            base_url: "https://api.openai.com/v1/chat/completions".into(),
            api_key: env("OPENAI_API_KEY"), model: model.into() },
        "groq" => Provider { dialect: Dialect::OpenAI,
            base_url: "https://api.groq.com/openai/v1/chat/completions".into(),
            api_key: env("GROQ_API_KEY"), model: model.into() },
        "openrouter" => Provider { dialect: Dialect::OpenAI,
            base_url: "https://openrouter.ai/api/v1/chat/completions".into(),
            api_key: env("OPENROUTER_API_KEY"), model: model.into() },
        "ollama" => Provider { dialect: Dialect::OpenAI,
            base_url: format!("{}/chat/completions",
                std::env::var("OLLAMA_BASE_URL")
                    .unwrap_or("http://localhost:11434/v1".into())),
            api_key: "ollama".into(), model: model.into() },
        other => bail!("unknown provider '{}'. Known: anthropic, openai, groq, \
                        openrouter, ollama", other),
    };
    Ok(p)
}

impl Provider {
    pub fn chat(&self, system: &str, messages: &[Value],
                tools: &[ToolSchema], max_tokens: u32) -> Result<ChatResponse> {
        match self.dialect {
            Dialect::Anthropic => self.chat_anthropic(system, messages, tools, max_tokens),
            Dialect::OpenAI => self.chat_openai(system, messages, tools, max_tokens),
        }
    }

    fn post(&self, body: Value, auth_anthropic: bool) -> Result<Value> {
        let req = ureq::post(&self.base_url)
            .set("content-type", "application/json");
        let req = if auth_anthropic {
            req.set("x-api-key", &self.api_key)
               .set("anthropic-version", "2023-06-01")
        } else {
            req.set("authorization", &format!("Bearer {}", self.api_key))
        };
        let resp = req.send_json(body);
        match resp {
            Ok(r) => Ok(r.into_json()?),
            Err(ureq::Error::Status(code, r)) => {
                let text = r.into_string().unwrap_or_default();
                bail!("API {} from {}: {}", code, self.base_url,
                      &text[..text.len().min(600)]);
            }
            Err(e) => bail!("request to {} failed: {}", self.base_url, e),
        }
    }

    // ---------------------------------------------------------- anthropic
    fn chat_anthropic(&self, system: &str, messages: &[Value],
                      tools: &[ToolSchema], max_tokens: u32) -> Result<ChatResponse> {
        let mut body = json!({
            "model": self.model, "max_tokens": max_tokens,
            "system": system, "messages": messages,
        });
        if !tools.is_empty() {
            body["tools"] = Value::Array(tools.iter().map(|t| json!({
                "name": t.name, "description": t.description,
                "input_schema": t.parameters })).collect());
        }
        let data = self.post(body, true)?;
        let content = data["content"].as_array().cloned().unwrap_or_default();
        let mut text = String::new();
        let mut calls = vec![];
        for block in &content {
            match block["type"].as_str() {
                Some("text") => text.push_str(block["text"].as_str().unwrap_or("")),
                Some("tool_use") => calls.push(ToolCall {
                    id: block["id"].as_str().unwrap_or("").into(),
                    name: block["name"].as_str().unwrap_or("").into(),
                    input: block["input"].clone() }),
                _ => {}
            }
        }
        Ok(ChatResponse {
            text, tool_calls: calls,
            input_tokens: data["usage"]["input_tokens"].as_u64().unwrap_or(0),
            output_tokens: data["usage"]["output_tokens"].as_u64().unwrap_or(0),
            raw_assistant_message: json!({"role": "assistant", "content": content}),
        })
    }

    // ------------------------------------------------------------- openai
    fn chat_openai(&self, system: &str, messages: &[Value],
                   tools: &[ToolSchema], max_tokens: u32) -> Result<ChatResponse> {
        let mut full = vec![json!({"role": "system", "content": system})];
        full.extend_from_slice(messages);
        let mut body = json!({
            "model": self.model, "max_tokens": max_tokens, "messages": full,
        });
        if !tools.is_empty() {
            body["tools"] = Value::Array(tools.iter().map(|t| json!({
                "type": "function",
                "function": {"name": t.name, "description": t.description,
                             "parameters": t.parameters}})).collect());
        }
        let data = self.post(body, false)?;
        let msg = &data["choices"][0]["message"];
        let text = msg["content"].as_str().unwrap_or("").to_string();
        let mut calls = vec![];
        if let Some(tcs) = msg["tool_calls"].as_array() {
            for tc in tcs {
                let args = tc["function"]["arguments"].as_str().unwrap_or("{}");
                calls.push(ToolCall {
                    id: tc["id"].as_str().unwrap_or("").into(),
                    name: tc["function"]["name"].as_str().unwrap_or("").into(),
                    input: serde_json::from_str(args).unwrap_or(json!({})),
                });
            }
        }
        Ok(ChatResponse {
            text, tool_calls: calls,
            input_tokens: data["usage"]["prompt_tokens"].as_u64().unwrap_or(0),
            output_tokens: data["usage"]["completion_tokens"].as_u64().unwrap_or(0),
            raw_assistant_message: msg.clone(),
        })
    }

    /// wrap executed tool results in this provider's dialect
    pub fn tool_result_messages(&self, results: &[(String, String, bool)]) -> Vec<Value> {
        match self.dialect {
            Dialect::Anthropic => vec![json!({
                "role": "user",
                "content": results.iter().map(|(id, out, err)| json!({
                    "type": "tool_result", "tool_use_id": id,
                    "content": out, "is_error": err })).collect::<Vec<_>>()
            })],
            Dialect::OpenAI => results.iter().map(|(id, out, _)| json!({
                "role": "tool", "tool_call_id": id, "content": out })).collect(),
        }
    }
}
