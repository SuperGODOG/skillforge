import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import readline from "node:readline";
import { Agent, type AgentTool, type AgentToolResult, type AgentMessage } from "@earendil-works/pi-agent-core";
import {
  fauxAssistantMessage,
  fauxProvider,
  fauxText,
  fauxThinking,
  fauxToolCall,
  Type,
  type TSchema,
  type TextContent,
  type ThinkingContent,
  type ToolCall,
  type StopReason,
} from "@earendil-works/pi-ai";
import { getModel, streamSimple, getApiProvider } from "@earendil-works/pi-ai/compat";

const PROTOCOL_VERSION = "pi_bridge_v1";

export interface BridgeTextContent {
  type: "text";
  text: string;
}

export interface BridgeThinkingContent {
  type: "thinking";
  thinking: string;
}

export interface BridgeToolCall {
  type: "toolCall";
  id: string;
  name: string;
  arguments: Record<string, unknown>;
}

export type BridgeContentBlock = BridgeTextContent | BridgeThinkingContent | BridgeToolCall;

export interface BridgeToolCallItem {
  id: string;
  type?: string;
  name?: string;
  function?: { name?: string; arguments?: string };
  arguments?: Record<string, unknown>;
}

export interface BridgeUserMessage {
  role: "user";
  content: string | BridgeTextContent[];
  timestamp?: number;
}

export interface BridgeAssistantMessage {
  role: "assistant";
  content: BridgeContentBlock[];
  tool_calls?: BridgeToolCallItem[];
  api?: string;
  provider?: string;
  model?: string;
  stopReason?: string;
  timestamp?: number;
}

export interface BridgeToolResultMessage {
  role: "tool" | "toolResult";
  toolCallId?: string;
  tool_call_id?: string;
  toolName?: string;
  tool_name?: string;
  content: string | BridgeTextContent[];
  details?: Record<string, unknown>;
  isError?: boolean;
  is_error?: boolean;
  timestamp?: number;
}

export type BridgeMessage = BridgeUserMessage | BridgeAssistantMessage | BridgeToolResultMessage;

export interface BridgeContextResponseFrame {
  protocol: string;
  type: "context_response";
  run_id: string;
  request_id: string;
  intent_version?: string;
  system_prompt?: string;
  messages?: BridgeMessage[];
  report?: Record<string, unknown>;
  allowed?: boolean;
  action?: string;
  paused?: boolean;
  error_type?: string;
  error?: string;
}

function serializeAgentMessages(messages: AgentMessage[]): BridgeMessage[] {
  const result: BridgeMessage[] = [];
  for (const msg of messages) {
    if (msg.role === "user") {
      if (typeof msg.content === "string") {
        result.push({ role: "user", content: msg.content, timestamp: msg.timestamp });
      } else if (Array.isArray(msg.content)) {
        const blocks: BridgeTextContent[] = [];
        for (const b of msg.content) {
          if (b.type === "text") {
            blocks.push({ type: "text", text: b.text });
          } else {
            throw new Error(`Unsupported content block type in user message: ${(b as { type: string }).type}`);
          }
        }
        result.push({ role: "user", content: blocks, timestamp: msg.timestamp });
      } else {
        result.push({ role: "user", content: String(msg.content ?? ""), timestamp: msg.timestamp });
      }
    } else if (msg.role === "assistant") {
      const contentBlocks: BridgeContentBlock[] = [];
      const toolCalls: BridgeToolCallItem[] = [];
      for (const block of msg.content) {
        if (block.type === "text") {
          contentBlocks.push({ type: "text", text: block.text });
        } else if (block.type === "thinking") {
          contentBlocks.push({ type: "thinking", thinking: block.thinking });
        } else if (block.type === "toolCall") {
          contentBlocks.push({
            type: "toolCall",
            id: block.id,
            name: block.name,
            arguments: (block.arguments as Record<string, unknown>) || {},
          });
          toolCalls.push({
            id: block.id,
            type: "function",
            name: block.name,
            function: {
              name: block.name,
              arguments: JSON.stringify(block.arguments || {}),
            },
            arguments: (block.arguments as Record<string, unknown>) || {},
          });
        } else {
          throw new Error(`Unsupported content block in assistant message: ${(block as { type: string }).type}`);
        }
      }
      result.push({
        role: "assistant",
        content: contentBlocks,
        tool_calls: toolCalls,
        api: msg.api,
        provider: msg.provider,
        model: msg.model,
        stopReason: msg.stopReason,
        timestamp: msg.timestamp,
      });
    } else if (msg.role === "toolResult") {
      let text = "";
      if (Array.isArray(msg.content)) {
        for (const b of msg.content) {
          if (b.type === "text") {
            text += (text ? "\n" : "") + b.text;
          } else {
            throw new Error(`Unsupported content block in toolResult: ${(b as { type: string }).type}`);
          }
        }
      }
      result.push({
        role: "tool",
        tool_call_id: msg.toolCallId,
        tool_name: msg.toolName,
        content: text,
        details: (msg.details as Record<string, unknown>) || {},
        is_error: msg.isError,
        timestamp: msg.timestamp,
      });
    } else {
      throw new Error(`Unsupported message role: ${(msg as { role: string }).role}`);
    }
  }
  return result;
}

function deserializeAgentMessages(messages: BridgeMessage[], defaultModel: any): AgentMessage[] {
  const result: AgentMessage[] = [];
  for (const msg of messages) {
    if (msg.role === "user") {
      let content: string | TextContent[];
      if (typeof msg.content === "string") {
        content = msg.content;
      } else if (Array.isArray(msg.content)) {
        content = msg.content.map((b) => ({ type: "text" as const, text: b.text }));
      } else {
        content = String(msg.content ?? "");
      }
      result.push({
        role: "user",
        content,
        timestamp: msg.timestamp || Date.now(),
      });
    } else if (msg.role === "assistant") {
      const blocks: (TextContent | ThinkingContent | ToolCall)[] = [];
      let hasToolCall = false;
      if (Array.isArray(msg.content)) {
        for (const b of msg.content) {
          if (b.type === "text") {
            blocks.push({ type: "text", text: b.text });
          } else if (b.type === "thinking") {
            blocks.push({ type: "thinking", thinking: b.thinking });
          } else if (b.type === "toolCall") {
            blocks.push({
              type: "toolCall",
              id: b.id,
              name: b.name,
              arguments: b.arguments || {},
            });
            hasToolCall = true;
          }
        }
      } else if (typeof msg.content === "string" && msg.content) {
        blocks.push({ type: "text", text: msg.content });
      }

      if (msg.tool_calls && Array.isArray(msg.tool_calls)) {
        for (const tc of msg.tool_calls) {
          const tcId = tc.id;
          if (!blocks.some((b) => b.type === "toolCall" && b.id === tcId)) {
            let tcArgs = tc.arguments;
            if (!tcArgs && tc.function?.arguments) {
              try {
                tcArgs = JSON.parse(tc.function.arguments);
              } catch {
                tcArgs = {};
              }
            }
            blocks.push({
              type: "toolCall",
              id: tcId,
              name: tc.name || tc.function?.name || "tool",
              arguments: tcArgs || {},
            });
            hasToolCall = true;
          }
        }
      }

      const stopReason: StopReason = (msg.stopReason || (hasToolCall ? "toolUse" : "stop")) as StopReason;
      result.push({
        role: "assistant",
        content: blocks,
        api: (msg.api || defaultModel?.api || "chat") as any,
        provider: (msg.provider || defaultModel?.provider || "faux") as any,
        model: msg.model || defaultModel?.id || "faux-1",
        usage: {
          input: 0,
          output: 0,
          totalTokens: 0,
          cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
          cacheRead: 0,
          cacheWrite: 0,
        },
        stopReason,
        timestamp: msg.timestamp || Date.now(),
      });
    } else if (msg.role === "tool" || msg.role === "toolResult") {
      const text = typeof msg.content === "string" ? msg.content : JSON.stringify(msg.content);
      result.push({
        role: "toolResult",
        toolCallId: msg.tool_call_id || msg.toolCallId || "",
        toolName: msg.tool_name || msg.toolName || "",
        content: [{ type: "text", text }],
        details: msg.details || {},
        isError: Boolean(msg.is_error || msg.isError),
        timestamp: msg.timestamp || Date.now(),
      });
    }
  }
  return result;
}

function sendFrame(frame: any): void {
  process.stdout.write(JSON.stringify(frame) + "\n");
}

function logDebug(msg: string): void {
  process.stderr.write(`[pi_runner] ${msg}\n`);
}

function jsonSchemaToTypeBox(schema: any): TSchema {
  if (!schema || schema.type !== "object") {
    return Type.Object({});
  }
  const properties: Record<string, TSchema> = {};
  const props = schema.properties || {};
  for (const [key, prop] of Object.entries<any>(props)) {
    let t: TSchema;
    switch (prop.type) {
      case "string":
        t = Type.String({ description: prop.description });
        break;
      case "number":
        t = Type.Number({ description: prop.description });
        break;
      case "integer":
        t = Type.Integer({ description: prop.description });
        break;
      case "boolean":
        t = Type.Boolean({ description: prop.description });
        break;
      case "array":
        t = Type.Array(Type.Any(), { description: prop.description });
        break;
      case "object":
        t = Type.Record(Type.String(), Type.Any(), { description: prop.description });
        break;
      default:
        throw new Error(`Unsupported schema property type '${prop.type}' for property '${key}'`);
    }
    properties[key] = t;
  }
  return Type.Object(properties);
}

function parseFauxResponses(responses: any[]): any[] {
  return responses.map((r) => {
    if (typeof r === "string") {
      const msg: any = fauxAssistantMessage(r);
      msg._missingUsage = true;
      return msg;
    }
    const rawContent = r.content;
    const blocks = Array.isArray(rawContent) ? rawContent : [rawContent];
    const fauxBlocks = blocks.map((b: any) => {
      if (typeof b === "string") return fauxText(b);
      if (b.type === "toolCall") {
        return fauxToolCall(b.name, b.arguments || {}, { id: b.id });
      }
      if (b.type === "thinking") {
        return fauxThinking(b.thinking || "");
      }
      return fauxText(b.text || "");
    });
    const hasToolCall = fauxBlocks.some((b: any) => b.type === "toolCall");
    const stopReason = r.stopReason || (hasToolCall ? "toolUse" : "stop");
    const msg: any = fauxAssistantMessage(fauxBlocks, { stopReason, errorMessage: r.errorMessage });
    if (r.usage === undefined || r.usage === null) {
      msg._missingUsage = true;
      msg.usage = null;
    } else {
      msg._missingUsage = false;
      msg.usage = r.usage;
    }
    return msg;
  });
}

class PiRunnerSession {
  public activeRunId: string | null = null;
  public agent: Agent | null = null;
  public turnCount: number = 0;
  public activeSystemPrompt: string = "";
  public currentRequestId: string = "req_default";
  public currentIntentVersion: string = "v1";
  public isPaused: boolean = false;
  public pendingTurnDecision: {
    resolve: () => void;
    reject: (err: any) => void;
  } | null = null;
  public pendingContextDecision: {
    resolve: (res: BridgeContextResponseFrame) => void;
    reject: (err: any) => void;
  } | null = null;
  public capturedProviderInputs: Array<{
    systemPrompt: string;
    messages: unknown[];
    turnIndex: number;
  }> = [];
  public pendingToolCalls = new Map<
    string,
    {
      resolve: (res: any) => void;
      reject: (err: any) => void;
    }
  >();
  private fauxHandle: ReturnType<typeof fauxProvider> | null = null;

  public handleInit(msg: any): void {
    const runId = msg.run_id;
    if (!runId) {
      throw new Error("Missing run_id in init frame");
    }
    this.activeRunId = runId;
    this.turnCount = 0;
    this.isPaused = false;
    this.activeSystemPrompt = msg.system_prompt || "";
    this.currentIntentVersion = msg.intent_version || "v1";
    this.currentRequestId = msg.request_id || "req_default";
    this.capturedProviderInputs = [];
    this.pendingTurnDecision = null;
    this.pendingContextDecision = null;
    this.pendingToolCalls.clear();

    const declaredTools = Array.isArray(msg.tools) ? msg.tools : [];
    const agentTools: AgentTool[] = declaredTools.map((t: any) => {
      const paramSchema = jsonSchemaToTypeBox(t.parameters);
      return {
        name: t.name,
        label: t.name,
        description: t.description || "",
        parameters: paramSchema,
        execute: async (toolCallId: string, params: any, signal?: AbortSignal) => {
          if (signal?.aborted) {
            throw new Error(`Tool call ${toolCallId} aborted before dispatch`);
          }
          sendFrame({
            protocol: PROTOCOL_VERSION,
            type: "tool_call",
            run_id: runId,
            call_id: toolCallId,
            tool_name: t.name,
            parameters: params,
          });

          return new Promise<AgentToolResult<any>>((resolve, reject) => {
            const onAbort = () => {
              this.pendingToolCalls.delete(toolCallId);
              reject(new Error(`Tool call ${toolCallId} aborted by agent signal`));
            };
            if (signal) {
              signal.addEventListener("abort", onAbort, { once: true });
            }
            this.pendingToolCalls.set(toolCallId, {
              resolve: (res: any) => {
                if (signal) signal.removeEventListener("abort", onAbort);
                if (res.status === "COMPLETED") {
                  resolve({
                    content: [{ type: "text", text: String(res.output || "") }],
                    details: res.data || {},
                  });
                } else {
                  const errText = res.error || res.error_message || "Tool execution failed";
                  resolve({
                    content: [{ type: "text", text: `Error [${res.error_type || res.status}]: ${errText}` }],
                    details: {
                      error: errText,
                      status: res.status,
                      error_type: res.error_type,
                    },
                    isError: true,
                  });
                }
              },
              reject: (err: any) => {
                if (signal) signal.removeEventListener("abort", onAbort);
                reject(err);
              },
            });
          });
        },
      };
    });

    const modelConfig = msg.model_config || { provider: "anthropic", model_id: "claude-haiku-4-5" };
    const isTestMode = Boolean(msg.is_test || modelConfig.is_test || modelConfig.test_mode);

    let baseStreamFn: any;
    let targetModel: any;
    let resolvedApiKey: string | undefined;
    const customHeaders: Record<string, string> = {};

    if (modelConfig.provider === "faux") {
      if (!isTestMode) {
        throw new Error("Faux provider is restricted to explicit test mode; production requires a valid Pi provider and model.");
      }
      if (modelConfig.responses && !isTestMode) {
        throw new Error("Pre-written response scripts are restricted to explicit test mode.");
      }
      this.fauxHandle = fauxProvider({
        models: [{ id: modelConfig.model_id || "faux-1" }],
      });
      if (modelConfig.responses && Array.isArray(modelConfig.responses)) {
        this.fauxHandle.setResponses(parseFauxResponses(modelConfig.responses));
      }
      targetModel = this.fauxHandle.getModel();
      baseStreamFn = this.fauxHandle.provider.streamSimple;
    } else {
      // Normal native Pi provider/model configuration entry point
      const m = getModel(modelConfig.provider, modelConfig.model_id);
      if (m) {
        targetModel = m;
        baseStreamFn = streamSimple;
      } else {
        // Local endpoint/auth settings are not part of the persisted Runtime binding yet.
        if (Array.isArray(msg.initial_messages) && msg.initial_messages.length > 0) {
          throw new Error("Local Pi model resume requires a frozen configuration binding; refusing to reuse a mutable models.json checkpoint");
        }
        // Built-in not found -> inspect models.json
        const agentDir = process.env.PI_AGENT_DIR || path.join(os.homedir(), ".pi", "agent");
        const modelsPath = path.join(agentDir, "models.json");
        if (!fs.existsSync(modelsPath)) {
          throw new Error(`Model not found in native Pi catalog and models.json not found at ${modelsPath}: provider='${modelConfig.provider}', model='${modelConfig.model_id}'`);
        }
        let data: any;
        try {
          data = JSON.parse(fs.readFileSync(modelsPath, "utf-8"));
        } catch (err: any) {
          throw new Error(`Failed to parse models.json at ${modelsPath}`);
        }

        const providerConfig = data?.providers?.[modelConfig.provider];
        if (!providerConfig) {
          throw new Error(`Provider '${modelConfig.provider}' not found in models.json`);
        }
        if (!providerConfig.api || typeof providerConfig.api !== "string") {
          throw new Error(`Missing or invalid 'api' for provider '${modelConfig.provider}' in models.json`);
        }
        const apiProvider = getApiProvider(providerConfig.api as any);
        if (!apiProvider) {
          throw new Error(`Unsupported or unknown API protocol '${providerConfig.api}' for provider '${modelConfig.provider}'. Cannot masquerade as other protocols.`);
        }
        if (!providerConfig.baseUrl || typeof providerConfig.baseUrl !== "string") {
          throw new Error(`Missing or invalid 'baseUrl' for provider '${modelConfig.provider}' in models.json`);
        }
        const endpoint = new URL(providerConfig.baseUrl);
        if (!["http:", "https:"].includes(endpoint.protocol) || endpoint.username || endpoint.password || endpoint.search || endpoint.hash) {
          throw new Error("Local model baseUrl must be an HTTP(S) endpoint without embedded credentials, query, or fragment");
        }

        const modelsList = providerConfig.models;
        if (!Array.isArray(modelsList)) {
          throw new Error(`Invalid 'models' list for provider '${modelConfig.provider}' in models.json`);
        }
        const matched = modelsList.find((item: any) => item && item.id === modelConfig.model_id);
        if (!matched) {
          throw new Error(`Model '${modelConfig.model_id}' not found under provider '${modelConfig.provider}' in models.json`);
        }

        // Authentication validation
        let rawApiKey = providerConfig.apiKey;
        if (rawApiKey === undefined || rawApiKey === null || (typeof rawApiKey === "string" && !rawApiKey.trim())) {
          throw new Error(`Missing authentication / apiKey for provider '${modelConfig.provider}' in models.json`);
        }
        if (typeof rawApiKey !== "string") {
          throw new Error(`Invalid apiKey format for provider '${modelConfig.provider}' in models.json`);
        }
        rawApiKey = rawApiKey.trim();
        if (rawApiKey.startsWith("!")) {
          throw new Error(`Shell command references ('!') in apiKey are rejected for provider '${modelConfig.provider}'`);
        }
        if (rawApiKey.startsWith("$")) {
          const envName = rawApiKey.slice(1);
          const envVal = process.env[envName];
          if (!envVal) {
            throw new Error(`Environment variable '${envName}' referenced by apiKey for provider '${modelConfig.provider}' is not set`);
          }
          resolvedApiKey = envVal;
        } else {
          resolvedApiKey = rawApiKey;
        }

        // Extract custom headers (keep secrets out of model object)
        if (providerConfig.headers && typeof providerConfig.headers === "object") {
          for (const [k, v] of Object.entries(providerConfig.headers)) {
            if (typeof v === "string") {
              customHeaders[k] = v;
            }
          }
        }

        targetModel = {
          id: matched.id,
          name: matched.name || matched.id,
          api: providerConfig.api,
          provider: modelConfig.provider,
          baseUrl: providerConfig.baseUrl,
          reasoning: Boolean(matched.reasoning),
          thinkingLevelMap: matched.thinkingLevelMap || undefined,
          input: matched.input || ["text"],
          cost: matched.cost || { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
          contextWindow: matched.contextWindow || 128000,
          maxTokens: matched.maxTokens || 4096,
          compat: matched.compat || undefined,
        };
        baseStreamFn = streamSimple;
      }
    }

    // Parameter extraction and strict pre-network validation
    let validatedMaxTokens: number | undefined;
    const rawMaxTokens = modelConfig.max_tokens ?? modelConfig.maxTokens;
    if (rawMaxTokens !== undefined && rawMaxTokens !== null) {
      const num = Number(rawMaxTokens);
      if (typeof rawMaxTokens !== "number" || !Number.isFinite(num) || num <= 0 || !Number.isInteger(num)) {
        throw new Error(`Invalid max_tokens '${rawMaxTokens}': must be a positive integer`);
      }
      validatedMaxTokens = num;
    }

    let validatedTemperature: number | undefined = undefined;
    if (modelConfig.temperature !== undefined && modelConfig.temperature !== null) {
      const num = Number(modelConfig.temperature);
      if (typeof modelConfig.temperature !== "number" || !Number.isFinite(num) || num < 0) {
        throw new Error(`Invalid temperature '${modelConfig.temperature}': must be a non-negative number`);
      }
      validatedTemperature = num;
    }

    let validatedMaxRetries = 0;
    const rawMaxRetries = modelConfig.max_retries ?? modelConfig.maxRetries;
    if (rawMaxRetries !== undefined && rawMaxRetries !== null) {
      const num = Number(rawMaxRetries);
      if (typeof rawMaxRetries !== "number" || !Number.isFinite(num) || num < 0 || !Number.isInteger(num)) {
        throw new Error(`Invalid max_retries '${rawMaxRetries}': must be a non-negative integer`);
      }
      validatedMaxRetries = num;
    }

    let validatedTimeoutMs = 120000;
    const rawTimeout = modelConfig.timeout_ms ?? modelConfig.timeoutMs ?? (modelConfig.timeout ? modelConfig.timeout * 1000 : undefined);
    if (rawTimeout !== undefined && rawTimeout !== null) {
      const num = Number(rawTimeout);
      if (typeof rawTimeout !== "number" || !Number.isFinite(num) || num <= 0) {
        throw new Error(`Invalid timeout '${rawTimeout}': must be a positive number`);
      }
      validatedTimeoutMs = num;
    }

    // Reasoning level selection: minimal or off based on model support
    let selectedReasoning: "minimal" | "off" = "off";
    if (modelConfig.reasoning === "off") {
      selectedReasoning = "off";
    } else if (modelConfig.reasoning === "minimal") {
      if (targetModel.reasoning && targetModel.thinkingLevelMap && targetModel.thinkingLevelMap.minimal === null) {
        selectedReasoning = "off";
      } else if (targetModel.reasoning) {
        selectedReasoning = "minimal";
      } else {
        selectedReasoning = "off";
      }
    } else {
      if (targetModel.reasoning && targetModel.thinkingLevelMap && targetModel.thinkingLevelMap.minimal === null) {
        selectedReasoning = "off";
      } else if (targetModel.reasoning) {
        selectedReasoning = "minimal";
      } else {
        selectedReasoning = "off";
      }
    }

    // Budget-aware stream function that requests authoritative Python turn permission
    const budgetedStreamFn = async (m: any, ctx: any, opts: any) => {
      this.turnCount++;
      if (this.activeSystemPrompt) {
        ctx.systemPrompt = this.activeSystemPrompt;
      }
      sendFrame({
        protocol: PROTOCOL_VERSION,
        type: "turn_request",
        run_id: runId,
        turn_index: this.turnCount,
        provider_input: {
          systemPrompt: ctx.systemPrompt,
          messages: ctx.messages,
        },
      });

      await new Promise<void>((resolve, reject) => {
        this.pendingTurnDecision = { resolve, reject };
      });

      // Only count requests admitted by Python and actually sent to the provider.
      this.capturedProviderInputs.push({
        systemPrompt: ctx.systemPrompt,
        messages: JSON.parse(JSON.stringify(ctx.messages)),
        turnIndex: this.turnCount,
      });

      const effectiveOpts: any = {
        ...opts,
        maxRetries: validatedMaxRetries,
        timeoutMs: validatedTimeoutMs,
      };
      if (validatedMaxTokens !== undefined) {
        effectiveOpts.maxTokens = validatedMaxTokens;
      }
      if (validatedTemperature !== undefined) {
        effectiveOpts.temperature = validatedTemperature;
      }
      if (selectedReasoning && selectedReasoning !== "off") {
        effectiveOpts.reasoning = selectedReasoning;
      } else {
        delete effectiveOpts.reasoning;
      }
      if (resolvedApiKey) {
        effectiveOpts.apiKey = resolvedApiKey;
      }
      if (Object.keys(customHeaders).length > 0) {
        effectiveOpts.headers = { ...(effectiveOpts.headers || {}), ...customHeaders };
      }

      return baseStreamFn(m, ctx, effectiveOpts);
    };

    const initialMessages: AgentMessage[] =
      Array.isArray(msg.initial_messages) && msg.initial_messages.length > 0
        ? deserializeAgentMessages(msg.initial_messages, targetModel)
        : [];

    this.agent = new Agent({
      streamFn: budgetedStreamFn,
      getApiKey: resolvedApiKey ? () => resolvedApiKey : undefined,
      transformContext: async (messages: AgentMessage[], signal?: AbortSignal) => {
        return await this.requestContextTransform(messages, targetModel, signal);
      },
      initialState: {
        systemPrompt: msg.system_prompt || "",
        model: targetModel,
        tools: agentTools,
        messages: initialMessages,
        thinkingLevel: selectedReasoning,
      },
    });

    this.agent.subscribe((event: any) => {
      if (event.type === "message_end" && event.message?.role === "assistant") {
        const m = event.message;
        let effUsage: any = null;
        if (m.usage && !m._missingUsage) {
          effUsage = {
            input: m.usage.input ?? null,
            output: m.usage.output ?? null,
            cacheRead: m.usage.cacheRead ?? null,
            cacheWrite: m.usage.cacheWrite ?? null,
            totalTokens: m.usage.totalTokens ?? null,
            cost: m.usage.cost ?? null,
          };
        }
        const isErr = m.stopReason === "error" || m.stopReason === "aborted" || Boolean(m.errorMessage);
        const status = isErr ? "error" : "success";
        const errMsg = m.errorMessage || (isErr ? "Assistant message error" : null);

        sendFrame({
          protocol: PROTOCOL_VERSION,
          type: "turn_receipt",
          run_id: runId,
          turn_index: this.turnCount,
          receipt_id: `rcpt_${runId}_${this.turnCount}`,
          usage: effUsage,
          status: status,
          error: errMsg,
          stop_reason: m.stopReason || "stop",
        });
      }
    });

    sendFrame({
      protocol: PROTOCOL_VERSION,
      type: "ready",
      run_id: runId,
    });
  }

  public async requestContextTransform(
    messages: AgentMessage[],
    targetModel: any,
    signal?: AbortSignal
  ): Promise<AgentMessage[]> {
    if (!this.activeRunId) {
      return messages;
    }
    if (signal?.aborted) {
      throw new Error(`Context transform aborted for run ${this.activeRunId}`);
    }

    const serialized = serializeAgentMessages(messages);

    sendFrame({
      protocol: PROTOCOL_VERSION,
      type: "context_request",
      run_id: this.activeRunId,
      request_id: this.currentRequestId,
      intent_version: this.currentIntentVersion,
      turn_index: this.turnCount + 1,
      messages: serialized,
    });

    const resp = await new Promise<BridgeContextResponseFrame>((resolve, reject) => {
      const onAbort = () => {
        this.pendingContextDecision = null;
        reject(new Error(`Context transform aborted for run ${this.activeRunId}`));
      };
      if (signal) {
        signal.addEventListener("abort", onAbort, { once: true });
      }
      this.pendingContextDecision = {
        resolve: (res) => {
          if (signal) signal.removeEventListener("abort", onAbort);
          resolve(res);
        },
        reject: (err) => {
          if (signal) signal.removeEventListener("abort", onAbort);
          reject(err);
        },
      };
    });

    if (resp.action === "pause" || resp.paused || resp.error_type === "PAUSED") {
      this.isPaused = true;
      sendFrame({
        protocol: PROTOCOL_VERSION,
        type: "paused",
        run_id: this.activeRunId,
        request_id: this.currentRequestId,
        stop_reason: "paused",
        messages: serialized,
        captured_provider_inputs: this.capturedProviderInputs,
      });
      if (this.agent) {
        this.agent.abort();
      }
      throw new Error(`Execution paused cooperatively for run ${this.activeRunId}`);
    }

    if (resp.allowed === false) {
      const errType = resp.error_type || "CONTEXT_ERROR";
      const errMsg = resp.error || "Context transform rejected";
      throw new Error(`Context transform rejected [${errType}]: ${errMsg}`);
    }

    if (resp.system_prompt !== undefined && resp.system_prompt !== null) {
      this.activeSystemPrompt = resp.system_prompt;
      if (this.agent) {
        this.agent.state.systemPrompt = resp.system_prompt;
      }
    }

    if (resp.messages && Array.isArray(resp.messages)) {
      return deserializeAgentMessages(resp.messages, targetModel);
    }
    return messages;
  }

  public handleContextResponse(msg: BridgeContextResponseFrame): void {
    if (msg.run_id !== this.activeRunId) {
      logDebug(`Context response run_id mismatch: expected ${this.activeRunId}, got ${msg.run_id}`);
      return;
    }
    const pending = this.pendingContextDecision;
    this.pendingContextDecision = null;
    if (!pending) {
      logDebug("No pending context decision");
      return;
    }
    pending.resolve(msg);
  }

  public handleTurnDecision(msg: any): void {
    if (msg.run_id !== this.activeRunId) {
      logDebug(`Turn decision run_id mismatch: expected ${this.activeRunId}, got ${msg.run_id}`);
      return;
    }
    const pending = this.pendingTurnDecision;
    this.pendingTurnDecision = null;
    if (!pending) {
      logDebug("No pending turn decision");
      return;
    }
    if (msg.allowed) {
      pending.resolve();
    } else {
      const errType = msg.error_type || "BUDGET_EXHAUSTED";
      const errMsg = msg.error || "Turn budget exceeded";
      pending.reject(new Error(`Turn budget exhausted [${errType}]: ${errMsg}`));
    }
  }

  public handleToolResult(msg: any): void {
    if (msg.run_id !== this.activeRunId) {
      logDebug(`Tool result run_id mismatch: expected ${this.activeRunId}, got ${msg.run_id}`);
      return;
    }
    const callId = msg.call_id;
    const pending = this.pendingToolCalls.get(callId);
    if (!pending) {
      logDebug(`No pending tool call for call_id: ${callId}`);
      return;
    }
    this.pendingToolCalls.delete(callId);
    pending.resolve(msg);
  }

  public async handlePrompt(msg: any): Promise<void> {
    const runId = msg.run_id;
    if (runId !== this.activeRunId || !this.agent) {
      throw new Error(`Prompt rejected: runner active run is ${this.activeRunId}, requested ${runId}`);
    }
    const requestId = msg.request_id || "req_default";
    this.currentRequestId = requestId;
    if (msg.intent_version) {
      this.currentIntentVersion = msg.intent_version;
    }
    try {
      await this.agent.prompt(msg.text || "");
      const messages = this.agent.state.messages;
      let lastAssistant: any = null;
      for (let i = messages.length - 1; i >= 0; i--) {
        if (messages[i].role === "assistant") {
          lastAssistant = messages[i];
          break;
        }
      }

      let responseText = "";
      let stopReason = "stop";
      let usage: any = null;
      if (lastAssistant) {
        stopReason = lastAssistant.stopReason || "stop";
        const textBlocks = (lastAssistant.content || [])
          .filter((c: any) => c.type === "text")
          .map((c: any) => c.text);
        responseText = textBlocks.join("\n");
        if (lastAssistant.usage && !lastAssistant._missingUsage) {
          usage = {
            input: lastAssistant.usage.input ?? null,
            output: lastAssistant.usage.output ?? null,
            cacheRead: lastAssistant.usage.cacheRead ?? null,
            cacheWrite: lastAssistant.usage.cacheWrite ?? null,
            totalTokens: lastAssistant.usage.totalTokens ?? null,
            cost: lastAssistant.usage.cost ?? null,
          };
        }
      }

      sendFrame({
        protocol: PROTOCOL_VERSION,
        type: "response",
        run_id: runId,
        request_id: requestId,
        text: responseText,
        stop_reason: stopReason,
        messages,
        usage: usage,
        captured_provider_inputs: this.capturedProviderInputs,
      });
    } catch (err: any) {
      if (this.isPaused) {
        return;
      }
      sendFrame({
        protocol: PROTOCOL_VERSION,
        type: "error",
        run_id: runId,
        request_id: requestId,
        error: err?.message || String(err),
        fatal: false,
      });
    }
  }

  public async handleContinue(msg: any): Promise<void> {
    const runId = msg.run_id;
    if (runId !== this.activeRunId || !this.agent) {
      throw new Error(`Continue rejected: runner active run is ${this.activeRunId}, requested ${runId}`);
    }
    const requestId = msg.request_id || "req_cont";
    this.currentRequestId = requestId;
    if (msg.intent_version) {
      this.currentIntentVersion = msg.intent_version;
    }
    try {
      await this.agent.continue();
      const messages = this.agent.state.messages;
      let lastAssistant: any = null;
      for (let i = messages.length - 1; i >= 0; i--) {
        if (messages[i].role === "assistant") {
          lastAssistant = messages[i];
          break;
        }
      }

      let responseText = "";
      let stopReason = "stop";
      let usage: any = null;
      if (lastAssistant) {
        stopReason = lastAssistant.stopReason || "stop";
        const textBlocks = (lastAssistant.content || [])
          .filter((c: any) => c.type === "text")
          .map((c: any) => c.text);
        responseText = textBlocks.join("\n");
        if (lastAssistant.usage && !lastAssistant._missingUsage) {
          usage = {
            input: lastAssistant.usage.input ?? null,
            output: lastAssistant.usage.output ?? null,
            cacheRead: lastAssistant.usage.cacheRead ?? null,
            cacheWrite: lastAssistant.usage.cacheWrite ?? null,
            totalTokens: lastAssistant.usage.totalTokens ?? null,
            cost: lastAssistant.usage.cost ?? null,
          };
        }
      }

      sendFrame({
        protocol: PROTOCOL_VERSION,
        type: "response",
        run_id: runId,
        request_id: requestId,
        text: responseText,
        stop_reason: stopReason,
        messages,
        usage: usage,
        captured_provider_inputs: this.capturedProviderInputs,
      });
    } catch (err: any) {
      if (this.isPaused) {
        return;
      }
      sendFrame({
        protocol: PROTOCOL_VERSION,
        type: "error",
        run_id: runId,
        request_id: requestId,
        error: err?.message || String(err),
        fatal: false,
      });
    }
  }

  public handleAbort(msg: any): void {
    if (msg.run_id !== this.activeRunId) {
      logDebug(`Abort run_id mismatch: expected ${this.activeRunId}, got ${msg.run_id}`);
      return;
    }
    logDebug(`Aborting run ${msg.run_id}: ${msg.reason || "user cancelled"}`);
    if (this.agent) {
      this.agent.abort();
    }
    if (this.pendingTurnDecision) {
      this.pendingTurnDecision.reject(new Error(`Turn cancelled by abort: ${msg.reason || "aborted"}`));
      this.pendingTurnDecision = null;
    }
    if (this.pendingContextDecision) {
      this.pendingContextDecision.reject(new Error(`Context transform cancelled by abort: ${msg.reason || "aborted"}`));
      this.pendingContextDecision = null;
    }
    for (const [callId, pending] of this.pendingToolCalls.entries()) {
      pending.reject(new Error(`Tool call ${callId} cancelled by abort`));
    }
    this.pendingToolCalls.clear();
    sendFrame({
      protocol: PROTOCOL_VERSION,
      type: "aborted",
      run_id: msg.run_id,
      reason: msg.reason || "aborted",
    });
  }

  public handlePause(msg: any): void {
    if (msg.run_id !== this.activeRunId) {
      logDebug(`Pause run_id mismatch: expected ${this.activeRunId}, got ${msg.run_id}`);
      return;
    }
    logDebug(`Pausing run ${msg.run_id}`);
    this.isPaused = true;
    sendFrame({
      protocol: PROTOCOL_VERSION,
      type: "paused",
      run_id: this.activeRunId,
      request_id: this.currentRequestId,
      stop_reason: "paused",
      messages: this.agent ? serializeAgentMessages(this.agent.state.messages) : [],
      captured_provider_inputs: this.capturedProviderInputs,
    });
    if (this.agent) {
      this.agent.abort();
    }
    if (this.pendingTurnDecision) {
      this.pendingTurnDecision.reject(new Error("Turn cancelled by pause"));
      this.pendingTurnDecision = null;
    }
    if (this.pendingContextDecision) {
      this.pendingContextDecision.reject(new Error("Context transform cancelled by pause"));
      this.pendingContextDecision = null;
    }
    for (const [callId, pending] of this.pendingToolCalls.entries()) {
      pending.reject(new Error(`Tool call ${callId} cancelled by pause`));
    }
    this.pendingToolCalls.clear();
  }
}

async function main(): Promise<void> {
  const session = new PiRunnerSession();
  const rl = readline.createInterface({
    input: process.stdin,
    output: process.stdout,
    terminal: false,
  });

  rl.on("line", async (line: string) => {
    const trimmed = line.trim();
    if (!trimmed) return;

    let frame: any;
    try {
      frame = JSON.parse(trimmed);
    } catch (parseErr: any) {
      logDebug(`Malformed JSON frame: ${trimmed}`);
      sendFrame({
        protocol: PROTOCOL_VERSION,
        type: "error",
        error: `Malformed JSON: ${parseErr.message}`,
        fatal: false,
      });
      return;
    }

    if (frame.protocol !== PROTOCOL_VERSION) {
      sendFrame({
        protocol: PROTOCOL_VERSION,
        type: "error",
        run_id: frame.run_id,
        error: `Protocol version mismatch: expected ${PROTOCOL_VERSION}, got ${frame.protocol}`,
        fatal: true,
      });
      return;
    }

    try {
      switch (frame.type) {
        case "init":
          session.handleInit(frame);
          break;
        case "context_response":
          session.handleContextResponse(frame);
          break;
        case "turn_decision":
          session.handleTurnDecision(frame);
          break;
        case "tool_result":
          session.handleToolResult(frame);
          break;
        case "prompt":
          await session.handlePrompt(frame);
          break;
        case "continue":
          await session.handleContinue(frame);
          break;
        case "abort":
          session.handleAbort(frame);
          break;
        case "pause":
          session.handlePause(frame);
          break;
        case "ping":
          sendFrame({ protocol: PROTOCOL_VERSION, type: "pong", run_id: frame.run_id });
          break;
        default:
          sendFrame({
            protocol: PROTOCOL_VERSION,
            type: "error",
            run_id: frame.run_id,
            error: `Unknown frame type: ${frame.type}`,
            fatal: false,
          });
      }
    } catch (err: any) {
      logDebug(`Error processing frame ${frame.type}: ${err.message}`);
      sendFrame({
        protocol: PROTOCOL_VERSION,
        type: "error",
        run_id: frame.run_id,
        error: err?.message || String(err),
        fatal: false,
      });
    }
  });

  rl.on("close", () => {
    logDebug("Stdin closed, exiting runner cleanly.");
    process.exit(0);
  });
}

main().catch((err) => {
  process.stderr.write(`[pi_runner fatal] ${err?.stack || err}\n`);
  sendFrame({
    protocol: PROTOCOL_VERSION,
    type: "error",
    error: err?.message || String(err),
    fatal: true,
  });
  process.exit(1);
});
