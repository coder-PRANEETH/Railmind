import Anthropic from "@anthropic-ai/sdk";
import { FunctionCallingConfigMode, GoogleGenAI, Type } from "@google/genai";
import cors from "cors";
import dotenv from "dotenv";
import express from "express";

dotenv.config();

const app = express();
const PORT = process.env.PORT || 3001;
const ANTHROPIC_MODEL = process.env.ANTHROPIC_MODEL || "claude-sonnet-4-20250514";
const GEMINI_MODEL = process.env.GEMINI_MODEL || "gemini-2.5-flash";
const hasGeminiKey = Boolean(process.env.GEMINI_API_KEY);
const hasAnthropicKey = Boolean(process.env.ANTHROPIC_API_KEY);
const LLM_PROVIDER = process.env.DEMO_MODE === "true"
  ? "demo"
  : hasGeminiKey
    ? "gemini"
    : hasAnthropicKey
      ? "anthropic"
      : "demo";
const DEMO_MODE = LLM_PROVIDER === "demo";

app.use(cors({ origin: process.env.CLIENT_ORIGIN || "http://localhost:5173" }));
app.use(express.json({ limit: "1mb" }));

const anthropic = process.env.ANTHROPIC_API_KEY
  ? new Anthropic({ apiKey: process.env.ANTHROPIC_API_KEY })
  : null;
const gemini = hasGeminiKey
  ? new GoogleGenAI({ apiKey: process.env.GEMINI_API_KEY })
  : null;

const systemPrompt = `
You are RailMind, an AI operations assistant for Indian Railways.

Rules:
- ALWAYS use one available tool before answering. Never guess from memory.
- Give the final answer in 1-3 sentences, plain English, no jargon.
- Use Indian railway context when explaining station, track, train, and weather data.
- If get_agent_recommendation is called, mention the confidence level.
- If any tool result includes urgency: "critical", clearly flag it as urgent.
- If the user asks a broad question, choose the most relevant tool and use values like "all" or "active" when an exact ID is not provided.
`;

const tools = [
  {
    name: "get_train_status",
    description:
      "Get live status for one train. Use train_id='all' for a network-wide train delay summary.",
    input_schema: {
      type: "object",
      properties: {
        train_id: {
          type: "string",
          description: "Train number or alias, for example MS-221, CBE-102, or all."
        }
      },
      required: ["train_id"]
    }
  },
  {
    name: "get_track_health",
    description:
      "Get track health and alerts. If no track is named, use track_id='7' for the high-risk demo section.",
    input_schema: {
      type: "object",
      properties: {
        track_id: {
          type: "string",
          description: "Track identifier, for example 7, 14A, or MAS-TBM-UP."
        }
      },
      required: ["track_id"]
    }
  },
  {
    name: "get_signal_status",
    description:
      "Get railway signal status. If no signal is named, use signal_id='SG-42'.",
    input_schema: {
      type: "object",
      properties: {
        signal_id: {
          type: "string",
          description: "Signal identifier, for example SG-42."
        }
      },
      required: ["signal_id"]
    }
  },
  {
    name: "get_agent_recommendation",
    description:
      "Get a recommendation from a railway operations agent. Use this for weather impact, maintenance schedule, station load, or general operational advice.",
    input_schema: {
      type: "object",
      properties: {
        agent: {
          type: "string",
          enum: ["weather", "track", "signal", "train", "station", "maintenance"],
          description: "The specialist agent to consult."
        }
      },
      required: ["agent"]
    }
  },
  {
    name: "get_simulation_scenario",
    description:
      "Get a future timeline scenario. Use scenario='A' for safest default, 'B' for fastest recovery, and 'C' for passenger-priority routing.",
    input_schema: {
      type: "object",
      properties: {
        scenario: {
          type: "string",
          enum: ["A", "B", "C"],
          description: "Scenario branch to inspect."
        }
      },
      required: ["scenario"]
    }
  },
  {
    name: "get_incident_report",
    description:
      "Get incident root cause and resolution status. Use incident_id='active' for the current active incident.",
    input_schema: {
      type: "object",
      properties: {
        incident_id: {
          type: "string",
          description: "Incident ID, for example INC-2047, or active."
        }
      },
      required: ["incident_id"]
    }
  }
];

const geminiTools = tools.map((tool) => ({
  name: tool.name,
  description: tool.description,
  parameters: toGeminiSchema(tool.input_schema)
}));

app.get("/api/health", (_req, res) => {
  res.json({
    ok: true,
    service: "RailMind LLM Interface",
    provider: LLM_PROVIDER,
    model:
      LLM_PROVIDER === "gemini"
        ? GEMINI_MODEL
        : LLM_PROVIDER === "anthropic"
          ? ANTHROPIC_MODEL
          : "demo-mode"
  });
});

app.post("/api/chat", async (req, res) => {
  try {
    const messages = normalizeMessages(req.body.messages);
    if (!messages.length) {
      return res.status(400).json({ error: "At least one user message is required." });
    }

    if (DEMO_MODE) {
      const demo = await runDemoTurn(messages);
      return res.json(demo);
    }

    if (LLM_PROVIDER === "gemini") {
      const geminiResponse = await runGeminiTurn(messages);
      return res.json(geminiResponse);
    }

    const firstPass = await anthropic.messages.create({
      model: ANTHROPIC_MODEL,
      max_tokens: 700,
      system: systemPrompt,
      messages,
      tools,
      tool_choice: { type: "any" }
    });

    const toolUse = firstPass.content.find((block) => block.type === "tool_use");
    if (!toolUse) {
      const fallbackTool = selectToolForPrompt(getLastUserText(messages));
      const result = await runRailTool(fallbackTool.name, fallbackTool.input);
      return res.json({
        reply: createDemoAnswer(fallbackTool.name, result),
        tool: {
          name: fallbackTool.name,
          endpoint: getToolEndpoint(fallbackTool.name),
          input: fallbackTool.input,
          result
        },
        provider: "anthropic",
        model: ANTHROPIC_MODEL
      });
    }

    const toolResult = await runRailTool(toolUse.name, toolUse.input || {});

    const finalPass = await anthropic.messages.create({
      model: ANTHROPIC_MODEL,
      max_tokens: 500,
      system: systemPrompt,
      messages: [
        ...messages,
        {
          role: "assistant",
          content: firstPass.content
        },
        {
          role: "user",
          content: [
            {
              type: "tool_result",
              tool_use_id: toolUse.id,
              content: JSON.stringify(toolResult)
            }
          ]
        }
      ],
      tools
    });

    res.json({
      reply: extractText(finalPass.content) || createDemoAnswer(toolUse.name, toolResult),
      tool: {
        name: toolUse.name,
        endpoint: getToolEndpoint(toolUse.name),
        input: toolUse.input,
        result: toolResult
      },
      provider: "anthropic",
      model: ANTHROPIC_MODEL
    });
  } catch (error) {
    const status = error.status || 500;
    res.status(status).json({
      error:
        LLM_PROVIDER === "gemini" && [400, 401, 403].includes(status)
          ? "Gemini API key rejected or model unavailable. Check GEMINI_API_KEY and GEMINI_MODEL in .env."
          : LLM_PROVIDER === "anthropic" && status === 401
          ? "Anthropic API key rejected. Check ANTHROPIC_API_KEY in .env."
          : "RailMind could not complete this request. Please try again."
    });
  }
});

function toGeminiSchema(schema) {
  const typeMap = {
    array: Type.ARRAY,
    boolean: Type.BOOLEAN,
    integer: Type.INTEGER,
    number: Type.NUMBER,
    object: Type.OBJECT,
    string: Type.STRING
  };

  const converted = {
    ...schema,
    type: typeMap[schema.type] || schema.type
  };

  if (schema.properties) {
    converted.properties = Object.fromEntries(
      Object.entries(schema.properties).map(([key, value]) => [key, toGeminiSchema(value)])
    );
  }

  if (schema.items) {
    converted.items = toGeminiSchema(schema.items);
  }

  return converted;
}

function normalizeMessages(rawMessages = []) {
  const messages = rawMessages
    .filter((message) => ["user", "assistant"].includes(message.role))
    .map((message) => ({
      role: message.role,
      content: String(message.content || "").slice(0, 4000)
    }))
    .filter((message) => message.content.trim().length > 0)
    .slice(-12);

  while (messages[0]?.role === "assistant") {
    messages.shift();
  }

  return messages;
}

function extractText(contentBlocks = []) {
  return contentBlocks
    .filter((block) => block.type === "text")
    .map((block) => block.text)
    .join("\n")
    .trim();
}

async function runDemoTurn(messages) {
  const selected = selectToolForPrompt(getLastUserText(messages));
  const result = await runRailTool(selected.name, selected.input);

  return {
    reply: createDemoAnswer(selected.name, result),
    tool: {
      name: selected.name,
      endpoint: getToolEndpoint(selected.name),
      input: selected.input,
      result
    },
    demoMode: true
  };
}

async function runGeminiTurn(messages) {
  const contents = messages.map((message) => ({
    role: message.role === "assistant" ? "model" : "user",
    parts: [{ text: message.content }]
  }));

  const firstPass = await gemini.models.generateContent({
    model: GEMINI_MODEL,
    contents,
    config: {
      systemInstruction: systemPrompt,
      tools: [{ functionDeclarations: geminiTools }],
      toolConfig: {
        functionCallingConfig: {
          mode: FunctionCallingConfigMode.ANY
        }
      }
    }
  });

  const functionCall = firstPass.functionCalls?.[0];
  if (!functionCall) {
    const fallbackTool = selectToolForPrompt(getLastUserText(messages));
    const result = await runRailTool(fallbackTool.name, fallbackTool.input);

    return {
      reply: createDemoAnswer(fallbackTool.name, result),
      tool: {
        name: fallbackTool.name,
        endpoint: getToolEndpoint(fallbackTool.name),
        input: fallbackTool.input,
        result
      },
      provider: "gemini",
      model: GEMINI_MODEL
    };
  }

  const toolResult = await runRailTool(functionCall.name, functionCall.args || {});
  const finalPass = await gemini.models.generateContent({
    model: GEMINI_MODEL,
    contents: [
      ...contents,
      firstPass.candidates?.[0]?.content,
      {
        role: "user",
        parts: [
          {
            functionResponse: {
              name: functionCall.name,
              response: { result: toolResult },
              id: functionCall.id
            }
          }
        ]
      }
    ].filter(Boolean),
    config: {
      systemInstruction: systemPrompt
    }
  });

  return {
    reply: finalPass.text || createDemoAnswer(functionCall.name, toolResult),
    tool: {
      name: functionCall.name,
      endpoint: getToolEndpoint(functionCall.name),
      input: functionCall.args || {},
      result: toolResult
    },
    provider: "gemini",
    model: GEMINI_MODEL
  };
}

function getLastUserText(messages) {
  return [...messages].reverse().find((message) => message.role === "user")?.content || "";
}

function selectToolForPrompt(prompt) {
  const text = prompt.toLowerCase();

  if (text.includes("track") || text.includes("rail health")) {
    return {
      name: "get_track_health",
      input: { track_id: extractLabeledId(prompt, "track", "7") }
    };
  }

  if (text.includes("signal")) {
    return {
      name: "get_signal_status",
      input: { signal_id: extractLabeledId(prompt, "signal", "SG-42") }
    };
  }

  if (text.includes("scenario") || text.includes("strategy") || text.includes("best")) {
    return {
      name: "get_simulation_scenario",
      input: { scenario: text.includes("fast") ? "B" : text.includes("passenger") ? "C" : "A" }
    };
  }

  if (text.includes("incident") || text.includes("root cause") || text.includes("active")) {
    return {
      name: "get_incident_report",
      input: { incident_id: text.includes("active") ? "active" : extractLabeledId(prompt, "incident", "active") }
    };
  }

  if (text.includes("weather")) {
    return { name: "get_agent_recommendation", input: { agent: "weather" } };
  }

  if (text.includes("maintenance") || text.includes("repair")) {
    return { name: "get_agent_recommendation", input: { agent: "maintenance" } };
  }

  if (text.includes("station") || text.includes("crowd")) {
    return { name: "get_agent_recommendation", input: { agent: "station" } };
  }

  return {
    name: "get_train_status",
    input: { train_id: extractLabeledId(prompt, "train", "all") }
  };
}

function extractLabeledId(prompt, label, fallback) {
  const pattern = new RegExp(`\\b${label}\\s+(?:id\\s+|number\\s+)?([a-z0-9-]+)`, "i");
  const match = prompt.match(pattern);
  const candidate = match?.[1]?.replace(/[?.!,;:]$/, "").toLowerCase();
  const genericWords = new Set([
    "delay",
    "delays",
    "health",
    "impact",
    "incident",
    "incidents",
    "report",
    "schedule",
    "status",
    "today"
  ]);

  return candidate && !genericWords.has(candidate) ? candidate : fallback;
}

async function runRailTool(name, input) {
  switch (name) {
    case "get_train_status":
      return getTrainStatus(input.train_id);
    case "get_track_health":
      return getTrackHealth(input.track_id);
    case "get_signal_status":
      return getSignalStatus(input.signal_id);
    case "get_agent_recommendation":
      return getAgentRecommendation(input.agent);
    case "get_simulation_scenario":
      return getSimulationScenario(input.scenario);
    case "get_incident_report":
      return getIncidentReport(input.incident_id);
    default:
      throw new Error(`Unknown tool: ${name}`);
  }
}

function getToolEndpoint(name) {
  const endpoints = {
    get_train_status: "/tools/train-status",
    get_track_health: "/tools/track-health",
    get_signal_status: "/tools/signal-status",
    get_agent_recommendation: "/tools/agent-recommendation",
    get_simulation_scenario: "/tools/simulation-scenario",
    get_incident_report: "/tools/incident-report"
  };

  return endpoints[name] || "/tools/unknown";
}

function getTrainStatus(trainId = "all") {
  if (String(trainId).toLowerCase() === "all") {
    return {
      scope: "Chennai suburban corridor",
      delayed_trains: 7,
      average_delay_minutes: 11,
      worst_delay: {
        train_id: "MS-221",
        route: "Chennai Central to Tambaram",
        delay_minutes: 24,
        current_position: "between Guindy and St Thomas Mount"
      },
      normal_trains: 38,
      passenger_impact: "moderate",
      last_updated: new Date().toISOString()
    };
  }

  return {
    train_id: trainId.toUpperCase(),
    route: "Chennai Central to Tambaram",
    current_position: "approaching Guindy",
    next_station: "St Thomas Mount",
    speed_kmph: 46,
    delay_minutes: 12,
    passenger_load: "high",
    cause: "temporary speed restriction near Track 7",
    last_updated: new Date().toISOString()
  };
}

function getTrackHealth(trackId = "7") {
  const isHotTrack = String(trackId).toLowerCase() === "7";

  return {
    track_id: trackId,
    section: isHotTrack ? "Guindy to St Thomas Mount UP line" : "Tambaram yard loop",
    health_score: isHotTrack ? 71 : 88,
    status: isHotTrack ? "watchlist" : "stable",
    speed_restriction_kmph: isHotTrack ? 45 : 70,
    alerts: isHotTrack
      ? ["Ballast vibration above baseline", "Heat stress risk rising", "Inspection due in 35 minutes"]
      : ["No active defects", "Next routine inspection tonight"],
    maintenance_window: isHotTrack ? "22:40-23:15 IST" : "02:00-02:20 IST",
    last_updated: new Date().toISOString()
  };
}

function getSignalStatus(signalId = "SG-42") {
  return {
    signal_id: signalId.toUpperCase(),
    section: "Chennai Egmore approach",
    color: "amber",
    aspect: "caution",
    controlled_section: "Platform 5 inbound",
    failure_probability: 0.08,
    last_updated: new Date().toISOString(),
    operator_note: "Amber is intentional due to congestion at Basin Bridge junction."
  };
}

function getAgentRecommendation(agent = "train") {
  const recommendations = {
    weather: {
      recommendation:
        "Keep a 45 kmph cap between Guindy and Tambaram until the rain cell clears.",
      confidence: 0.87,
      urgency: "medium",
      reason: "IMD radar indicates heavy rain moving across the southern suburban section."
    },
    track: {
      recommendation: "Dispatch inspection crew to Track 7 and hold heavy freight for 20 minutes.",
      confidence: 0.91,
      urgency: "critical",
      reason: "Track health trend shows vibration and heat stress rising together."
    },
    signal: {
      recommendation: "Keep SG-42 in caution mode and route express traffic through Platform 3.",
      confidence: 0.82,
      urgency: "medium",
      reason: "Signal is healthy, but congestion requires controlled approach speed."
    },
    train: {
      recommendation: "Prioritize MS-221 and CBE-102 for platform clearance at Tambaram.",
      confidence: 0.79,
      urgency: "medium",
      reason: "Both trains carry high passenger loads and are delay multipliers."
    },
    station: {
      recommendation: "Open two extra gates at Chennai Central and shift crowd flow to Gate 4.",
      confidence: 0.84,
      urgency: "low",
      reason: "Passenger density is rising near the main concourse."
    },
    maintenance: {
      recommendation: "Schedule Track 7 tamping at 22:40 IST and pre-stage crew at Guindy.",
      confidence: 0.89,
      urgency: "high",
      reason: "Short maintenance window avoids peak traffic and prevents morning slowdown."
    }
  };

  return {
    agent,
    ...recommendations[agent],
    generated_at: new Date().toISOString()
  };
}

function getSimulationScenario(scenario = "A") {
  const scenarios = {
    A: {
      strategy: "Slow affected section, reroute two express services, keep suburban trains moving.",
      score: 91,
      predicted_delay_minutes: 8,
      safety_rating: "excellent",
      passenger_impact: "low",
      tradeoff: "Slight express delay, strongest safety margin."
    },
    B: {
      strategy: "Clear express backlog first and compress suburban headways after Tambaram.",
      score: 84,
      predicted_delay_minutes: 6,
      safety_rating: "good",
      passenger_impact: "medium",
      tradeoff: "Fastest recovery, but more crowding at intermediate stations."
    },
    C: {
      strategy: "Prioritize passenger-heavy suburban trains and hold low-load freight outside the zone.",
      score: 88,
      predicted_delay_minutes: 9,
      safety_rating: "very good",
      passenger_impact: "lowest",
      tradeoff: "Best passenger outcome, slower freight recovery."
    }
  };

  return {
    scenario,
    ...scenarios[scenario],
    generated_at: new Date().toISOString()
  };
}

function getIncidentReport(incidentId = "active") {
  return {
    incident_id: incidentId === "active" ? "INC-2047" : incidentId.toUpperCase(),
    severity: "medium",
    root_cause_chain: [
      "Heavy rain cell crossed Guindy at 18:35 IST",
      "Track 7 vibration rose above baseline",
      "Signal SG-42 switched to caution to protect spacing",
      "MS-221 and TBM-408 accumulated platform approach delay"
    ],
    affected_trains: ["MS-221", "TBM-408", "CBE-102"],
    resolution_status: "crew dispatched, speed restriction active",
    eta_to_clear_minutes: 38,
    last_updated: new Date().toISOString()
  };
}

function createDemoAnswer(toolName, result) {
  if (toolName === "get_train_status") {
    if (result.scope) {
      return `${result.scope} has ${result.delayed_trains} delayed trains with an average delay of ${result.average_delay_minutes} minutes. The biggest issue is ${result.worst_delay.train_id}, delayed by ${result.worst_delay.delay_minutes} minutes ${result.worst_delay.current_position}.`;
    }

    return `${result.train_id} is ${result.delay_minutes} minutes late near ${result.current_position}, mainly due to ${result.cause}. It should next reach ${result.next_station}.`;
  }

  if (toolName === "get_track_health") {
    return `Track ${result.track_id} is on ${result.status} with a health score of ${result.health_score}/100. Keep speed near ${result.speed_restriction_kmph} kmph and inspect during the ${result.maintenance_window} window.`;
  }

  if (toolName === "get_signal_status") {
    return `${result.signal_id} is showing ${result.color} (${result.aspect}) for ${result.controlled_section}. This is intentional because ${result.operator_note}`;
  }

  if (toolName === "get_agent_recommendation") {
    const urgent = result.urgency === "critical" ? "Urgent: " : "";
    return `${urgent}${result.recommendation} Confidence is ${Math.round(result.confidence * 100)}%, with ${result.urgency} urgency.`;
  }

  if (toolName === "get_simulation_scenario") {
    return `Scenario ${result.scenario} is the best safe default here: ${result.strategy} It scores ${result.score}/100 with about ${result.predicted_delay_minutes} minutes predicted delay.`;
  }

  if (toolName === "get_incident_report") {
    return `Incident ${result.incident_id} is ${result.resolution_status}, affecting ${result.affected_trains.join(", ")}. The likely root cause is rain-driven Track 7 vibration, and clearance is estimated in ${result.eta_to_clear_minutes} minutes.`;
  }

  return "RailMind checked the relevant operations tool and found no critical action needed right now.";
}

app.listen(PORT, () => {
  const mode =
    LLM_PROVIDER === "gemini"
      ? `Gemini ${GEMINI_MODEL}`
      : LLM_PROVIDER === "anthropic"
        ? `Anthropic ${ANTHROPIC_MODEL}`
        : "demo fallback";
  console.log(`RailMind API running on http://localhost:${PORT} (${mode})`);
});
