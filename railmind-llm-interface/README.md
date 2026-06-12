# RailMind LLM Interface

RailMind is the natural-language interface module for a railway intelligence hackathon project. The React frontend lets an operator ask plain-English questions, while the Express backend keeps the API key server-side and uses Gemini or Anthropic tool calling to query railway operations tools.

## What It Does

- Calls one railway tool before every answer.
- Shows the exact tool name, input, and raw JSON in a collapsible badge.
- Includes realistic Indian Railways mock data for trains, tracks, signals, agents, scenarios, and incidents.
- Runs in demo mode without an API key, then switches to Gemini when `GEMINI_API_KEY` is present.
- Falls back to Anthropic only if `ANTHROPIC_API_KEY` is present and no Gemini key is configured.
- Is ready to connect to teammates' real endpoints by replacing the mock functions in `server.js`.

## API Key Choice

Anthropic Claude API keys are generally for paid/credit-based API usage. Gemini API is better for this hackathon setup because Google currently offers a Free Tier for supported Gemini API models, subject to rate limits.

Use Gemini first:

```bash
GEMINI_API_KEY=your-gemini-api-key
GEMINI_MODEL=gemini-2.5-flash
```

Keep Anthropic only as an optional fallback:

```bash
# ANTHROPIC_API_KEY=sk-ant-your-key
# ANTHROPIC_MODEL=claude-sonnet-4-20250514
```

## Install

Download or prepare these first:

- Node.js 20 or newer
- npm, which comes with Node.js
- Gemini API key for real LLM calls
- This project folder

```bash
npm install
```

`npm install` downloads the app dependencies:

- `@google/genai` for Gemini tool calling
- `@anthropic-ai/sdk` for optional Claude fallback tool calling
- `express`, `cors`, and `dotenv` for the private backend API
- `react`, `react-dom`, `vite`, and `@vitejs/plugin-react` for the frontend
- `tailwindcss` and `@tailwindcss/vite` for styling
- `lucide-react` for icons
- `axios` for swapping mock tools to real teammate endpoints later

Create your environment file:

```bash
copy .env.example .env
```

Then edit `.env` and add:

```bash
GEMINI_API_KEY=your-gemini-api-key
```

## Run

Open two terminals:

```bash
npm run server
```

```bash
npm run dev
```

Open the Vite URL, usually `http://localhost:5173`.

## Demo Queries

- `Train delays today`
- `What is the health of Track 7?`
- `What is the weather impact right now?`
- `Which future scenario is safest?`
- `Show active incidents`
- `What maintenance should we schedule?`

## Connect Real Backend Later

When the simulation and multi-agent teams are ready, replace these mock functions in `server.js` with real API calls:

- `getTrainStatus`
- `getTrackHealth`
- `getSignalStatus`
- `getAgentRecommendation`
- `getSimulationScenario`
- `getIncidentReport`

The frontend does not need to change as long as `/api/chat` keeps returning:

```json
{
  "reply": "Plain-English answer",
  "tool": {
    "name": "get_track_health",
    "endpoint": "/tools/track-health",
    "input": {},
    "result": {}
  }
}
```
